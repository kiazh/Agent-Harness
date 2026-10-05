"""ReAct agent loop — Thought → Action → Observation."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncGenerator
from functools import wraps
from typing import Any

from rich.console import Console

from ah.core.assembler import PromptAssembler
from ah.core.config import config
from ah.core.context import context_manager
from ah.core.exceptions import DatabaseError, SessionNotFoundError, UsageBudgetExceededError
from ah.core.metrics import metrics
from ah.core.models import AgentResponse, LLMResponse, StreamEvent, ToolDefinition
from ah.core.provider import LLMProvider, audit_log, get_provider
from ah.core.session import Session, session_manager
from ah.core.usage import usage_store
from ah.memory.consolidator import MemoryConsolidator
from ah.memory.retriever import MemoryRetriever
from ah.memory.store import memory_store
from ah.observability.tracing import span
from ah.plugins.registry import plugin_registry
from ah.rag.pipeline import RAGPipeline
from ah.tools.base import registry

__all__ = [
    "BaseReActAgent",
    "ReActAgent",
    "SYSTEM_PROMPT",
    "MAX_TOKEN_BUDGET",
    "MAX_PENDING_LEARNING_REVIEWS",
    "instrument_run",
    "instrument_stream",
]

logger = logging.getLogger(__name__)

console = Console()


def instrument_run(func):
    @wraps(func)
    async def wrapper(self, session_id, user_message, verbose=True):
        start = time.monotonic()
        with span("agent.run", agent_id=self.agent_id, session_id=str(session_id)):
            try:
                response = await func(self, session_id, user_message, verbose)
            except Exception:
                metrics.record_error("agent.run")
                raise
        metrics.record_latency("agent.run", (time.monotonic() - start) * 1000)
        metrics.increment_counter("agent.run.calls")
        session = await session_manager.get(session_id)
        await plugin_registry.dispatch("post_agent_run", session, response)
        return response

    return wrapper


def instrument_stream(func):
    @wraps(func)
    async def wrapper(self, session_id, user_message, verbose=True):
        start = time.monotonic()
        with span("agent.run_stream", agent_id=self.agent_id, session_id=str(session_id)):
            try:
                async for event in func(self, session_id, user_message, verbose):
                    if event.type == "done":
                        metrics.record_latency("agent.run", (time.monotonic() - start) * 1000)
                        metrics.increment_counter("agent.run.calls")
                        session = await session_manager.get(session_id)
                        await plugin_registry.dispatch("post_agent_run", session, event.response)
                    yield event
            except Exception:
                metrics.record_error("agent.run")
                raise

    return wrapper


SYSTEM_PROMPT = """You are AgentHarness, a self-hosted AI agent. You have access to tools and persistent context stored in PostgreSQL.

When you need to do something, use the available tools. Think step by step:
1. Understand the task
2. Decide what tools to use
3. Execute and observe results
4. Repeat until done

Be concise. Don't over-explain. Get things done."""

# Maximum token budget for a single agent run (fallback, session.context_budget takes precedence)
MAX_TOKEN_BUDGET = 50_000
MAX_PENDING_LEARNING_REVIEWS = 16
_pending_learning_reviews: set[asyncio.Task] = set()


class BaseReActAgent:
    """Base class for ReAct agents — Template Method pattern.

    Contains all shared logic for session retrieval, context storage,
    memory retrieval, RAG retrieval, prompt assembly, tool execution,
    and memory consolidation. Subclasses implement run() and run_stream().
    """

    def __init__(
        self,
        provider: LLMProvider | None = None,
        max_iterations: int = 10,
        agent_id: str = "harness",
        system_prompt: str | None = None,
        memory_retriever: MemoryRetriever | None = None,
        memory_consolidator: MemoryConsolidator | None = None,
        rag_pipeline: RAGPipeline | None = None,
        allowed_tools: list[str] | None = None,
    ) -> None:
        self.provider = provider or get_provider()
        self.max_iterations = max_iterations
        self.agent_id = agent_id
        self.system_prompt = system_prompt or SYSTEM_PROMPT
        self.memory_retriever = memory_retriever or MemoryRetriever(store=memory_store)
        self.memory_consolidator = memory_consolidator
        self.rag_pipeline = rag_pipeline
        # None = every registered tool; a list restricts the agent to those names.
        self.allowed_tools = allowed_tools
        # Keep strong references to background consolidation tasks so the
        # event loop's weakref-only tracking never lets them be GC'd
        # mid-execution (the classic "Task was destroyed but it is pending"
        # bug).  A done-callback discards each task once it finishes.
        self._consolidation_tasks: set[asyncio.Task] = set()
        self._learning_tasks: set[asyncio.Task] = set()

    def _tool_defs(self) -> list[ToolDefinition]:
        """Tool definitions offered to the model, honoring the allow-list."""
        defs = registry.get_tool_definitions()
        if self.allowed_tools is None:
            return defs
        allowed = set(self.allowed_tools)
        return [d for d in defs if d.name in allowed]

    async def _get_rag_context(
        self,
        session_id: uuid.UUID,
        query: str,
    ) -> list[tuple]:
        """Retrieve RAG context for the query if a RAG pipeline is configured.

        Returns a list of (ContextChunk, score) tuples for prompt assembly.
        """
        if self.rag_pipeline is None:
            return []

        try:
            results = await self.rag_pipeline.search(
                query=query,
                session_id=session_id,
                top_k=5,
                rerank=True,
            )
            return [(r.chunk, r.score) for r in results]
        except Exception as e:
            logger.warning("RAG retrieval failed: %s", e)
            return []

    async def _call_llm_with_retry(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        session_id: uuid.UUID | None = None,
    ):
        """Call provider.complete() with exponential backoff retry.

        Retries up to 3 times on failure with delays of 1s, 2s, 4s.
        """
        last_exception: Exception | None = None
        for attempt in range(4):  # 1 initial + 3 retries
            try:
                return await usage_store.complete_call(
                    self.provider,
                    session_id,
                    self.agent_id,
                    messages,
                    tools=tools,
                )
            except (UsageBudgetExceededError, DatabaseError):
                raise
            except Exception as e:
                last_exception = e
                if attempt < 3:
                    delay = 2**attempt  # 1s, 2s, 4s
                    logger.warning(
                        "LLM call failed (attempt %d/%d): %s — retrying in %ds",
                        attempt + 1,
                        4,
                        e,
                        delay,
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.error(
                        "LLM call failed after %d attempts: %s",
                        attempt + 1,
                        e,
                    )
        raise last_exception  # type: ignore[misc]

    async def _stream_llm_with_retry(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        session_id: uuid.UUID | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Call provider.stream_complete() with exponential backoff retry.

        Yields StreamEvent objects as they arrive from the provider.
        """
        last_exception: Exception | None = None
        for attempt in range(4):  # 1 initial + 3 retries
            emitted = False
            try:
                reservation = await self._reserve_usage(session_id, messages, tools)
                usage = None
                actual_model = None
                completed = False
                try:
                    async for event in self.provider.stream_complete(
                        messages=messages,
                        tools=tools,
                    ):
                        emitted = True
                        if event.type == "done" and event.response is not None:
                            usage = event.response.usage
                            actual_model = event.response.model
                            completed = True
                        yield event
                finally:
                    await self._finish_usage(
                        reservation,
                        usage,
                        failed=not completed,
                        model=actual_model,
                    )
                return  # Success — exit retry loop
            except (UsageBudgetExceededError, DatabaseError):
                raise
            except Exception as e:
                last_exception = e
                # Once a delta reached the caller, replaying the request would
                # duplicate text (and could repeat a tool call).
                if emitted:
                    raise
                if attempt < 3:
                    delay = 2**attempt  # 1s, 2s, 4s
                    logger.warning(
                        "LLM stream failed (attempt %d/%d): %s — retrying in %ds",
                        attempt + 1,
                        4,
                        e,
                        delay,
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.error(
                        "LLM stream failed after %d attempts: %s",
                        attempt + 1,
                        e,
                    )
        raise last_exception  # type: ignore[misc]

    async def _reserve_usage(
        self,
        session_id: uuid.UUID | None,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> uuid.UUID | None:
        if session_id is None:
            return None
        model = str(getattr(self.provider, "model", "unknown"))
        provider_name = type(self.provider).__name__.removesuffix("Provider").lower()
        try:
            return await usage_store.reserve(
                session_id,
                self.agent_id,
                provider_name,
                model,
                messages,
                tools,
                4096,
            )
        except UsageBudgetExceededError:
            raise
        except Exception as e:
            raise DatabaseError("usage accounting unavailable") from e

    async def _finish_usage(
        self,
        reservation: uuid.UUID | None,
        usage: dict[str, Any] | None = None,
        *,
        failed: bool = False,
        model: str | None = None,
    ) -> None:
        try:
            await usage_store.finish(reservation, usage, failed=failed, model=model)
        except Exception as e:
            raise DatabaseError("usage accounting unavailable") from e

    async def _prepare_context(
        self,
        session_id: uuid.UUID,
        user_message: str,
        include_memory: bool = True,
    ) -> tuple[Session, list[dict[str, Any]]]:
        """Prepare session, context, and prompt messages.

        Handles session retrieval, user message storage, recent context retrieval,
        memory retrieval (optional), RAG retrieval, and prompt assembly.

        Args:
            session_id: The session ID.
            user_message: The user's message.
            include_memory: Whether to include memory retrieval. Defaults to True.

        Returns:
            A tuple of (Session, messages list).

        Raises:
            ValueError: If the session is not found.
        """
        # Get session for context budget and goal
        session = await session_manager.get(session_id)
        if session is None:
            raise SessionNotFoundError(f"Session {session_id} not found")

        await plugin_registry.dispatch("pre_agent_run", session, user_message)

        assembler = PromptAssembler(session.context_budget)

        # Store user message in context
        await context_manager.add_chunk(
            session_id=session_id,
            agent_id=self.agent_id,
            chunk_type="user_message",
            payload={"content": user_message},
            token_count=len(user_message) // 4,
        )

        # Get recent context for prompt assembly
        recent = await context_manager.get_recent_context(session_id, limit=3)

        # Build retrieved chunks for the assembler
        retrieved_chunks: list[tuple] = []

        # Retrieve relevant long-term memories (optional)
        if include_memory:
            try:
                retrieved_memories = await self.memory_retriever.retrieve(
                    query=user_message,
                    agent_id=self.agent_id,
                    session_id=session_id,
                    emotion=session.state.get("emotion"),
                )
                # Convert retrieved memories to ContextChunk-like tuples for assembler
                from ah.core.models import ContextChunk

                for rm in retrieved_memories:
                    m = rm.memory
                    chunk = ContextChunk(
                        id=m.id,
                        session_id=session_id,
                        agent_id=self.agent_id,
                        chunk_type="memory",
                        payload={
                            "content": m.content,
                            "importance": m.importance,
                            "category": m.category,
                            "persona_interpretation": rm.persona_interpretation,
                        },
                        token_count=len(m.content) // 4,
                    )
                    retrieved_chunks.append((chunk, rm.score))
            except Exception as e:
                logger.warning("Memory retrieval failed: %s", e)

        # Retrieve RAG context if pipeline is configured
        rag_chunks = await self._get_rag_context(session_id, user_message)
        retrieved_chunks.extend(rag_chunks)

        try:
            retrieved_chunks.extend(
                await context_manager.search_archive_text(session_id, user_message)
            )
        except Exception as e:
            logger.warning("Archived context retrieval failed: %s", e)

        system_prompt = self.system_prompt
        if self.allowed_tools is None or "skill_read" in self.allowed_tools:
            try:
                from ah.skills.runtime import matching_skill_catalog

                system_prompt += matching_skill_catalog(user_message)
            except (OSError, ValueError) as exc:
                logger.warning("Skill catalog unavailable: %s", exc)

        # Assemble prompt
        prompt = assembler.assemble(
            system_prompt=system_prompt,
            goal=session.goal,
            recent_chunks=recent,
            retrieved_chunks=retrieved_chunks,
            query=user_message,
        )

        messages = [
            {"role": "user", "content": prompt},
        ]
        return session, messages

    def _append_tool_messages(
        self,
        messages: list[dict[str, Any]],
        response: LLMResponse,
        tc: dict[str, Any],
        content: str,
    ) -> None:
        """Append the assistant tool-call turn and its tool result to *messages*."""
        messages.append(
            {
                "role": "assistant",
                "content": response.content,
                "tool_calls": [tc],
            }
        )
        messages.append(
            {
                "role": "tool",
                "content": content,
                "tool_call_id": tc.get("id", ""),
            }
        )

    async def _execute_tool_calls(
        self,
        response: LLMResponse,
        messages: list[dict[str, Any]],
        tool_calls_made: list[dict[str, Any]],
        session_id: uuid.UUID,
        verbose: bool,
    ) -> None:
        """Execute tool calls for the non-streaming loop.

        Thin wrapper over :meth:`_execute_tool_calls_stream` (the single
        implementation) that prints progress when *verbose* is set.
        """
        async for event in self._execute_tool_calls_stream(
            response, messages, tool_calls_made, session_id
        ):
            if not verbose:
                continue
            if event.type == "tool_call":
                args_preview = json.dumps(event.tool_args, default=str)[:80]
                console.print(f"[yellow]  → {event.tool_name}({args_preview})[/yellow]")
            elif event.type == "tool_result":
                preview = event.tool_result[:200].replace("\n", " ")
                console.print(f"[green]  ← {preview}[/green]")

    async def _execute_tool_calls_stream(
        self,
        response: LLMResponse,
        messages: list[dict[str, Any]],
        tool_calls_made: list[dict[str, Any]],
        session_id: uuid.UUID,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Execute tool calls from an LLM response, yielding StreamEvents.

        Shared by ``run()`` and ``run_stream()``. Handles missing tool names,
        invalid JSON arguments, execution with timing, audit logging, context
        storage, and message updates. Modifies *messages* and
        *tool_calls_made* in place.
        """
        for tc in response.tool_calls:
            function_data = tc.get("function", {})
            tool_name = function_data.get("name")
            if not tool_name:
                logger.warning("Tool call missing 'name' field: %s", tc)
                audit_log(
                    "tool_call_invalid",
                    session_id=str(session_id),
                    error="missing_name",
                )
                self._append_tool_messages(
                    messages, response, tc, "Error: tool call missing 'name' field"
                )
                continue

            raw_args = function_data.get("arguments", "{}")
            try:
                tool_args = raw_args if isinstance(raw_args, dict) else json.loads(raw_args)
                if not isinstance(tool_args, dict):
                    raise ValueError("tool arguments must be an object")
            except (TypeError, ValueError) as e:
                logger.error(
                    "Failed to parse tool arguments for '%s': %s (raw: %s)",
                    tool_name,
                    e,
                    raw_args,
                )
                audit_log(
                    "tool_call_invalid_args",
                    session_id=str(session_id),
                    tool_name=tool_name,
                    error=str(e),
                )
                self._append_tool_messages(
                    messages, response, tc, f"Error: invalid tool arguments: {e}"
                )
                continue

            if self.allowed_tools is not None and tool_name not in self.allowed_tools:
                result_str = f"Error: tool '{tool_name}' is not allowed for agent '{self.agent_id}'"
                audit_log(
                    "tool_call_denied",
                    session_id=str(session_id),
                    tool_name=tool_name,
                    agent_id=self.agent_id,
                )
                yield StreamEvent(type="tool_call", tool_name=tool_name, tool_args=tool_args)
                yield StreamEvent(type="tool_result", tool_name=tool_name, tool_result=result_str)
                self._append_tool_messages(messages, response, tc, result_str)
                continue

            yield StreamEvent(type="tool_call", tool_name=tool_name, tool_args=tool_args)
            audit_log(
                "tool_call_start",
                session_id=str(session_id),
                tool_name=tool_name,
            )

            start = time.monotonic()
            try:
                with span("agent.tool", tool_name=tool_name, session_id=str(session_id)):
                    await plugin_registry.dispatch("on_tool_call", tool_name, tool_args)
                    if tool_name in {
                        "delegate",
                        "remember",
                        "recall",
                        "share_memory",
                        "session_recall",
                        "session_recall_window",
                    }:
                        from ah.tools.agents import current_agent_id, current_session_id

                        token = current_session_id.set(session_id)
                        agent_token = current_agent_id.set(self.agent_id)
                        try:
                            result = await asyncio.wait_for(
                                registry.execute(tool_name, **tool_args),
                                timeout=1,
                            )
                        finally:
                            current_agent_id.reset(agent_token)
                            current_session_id.reset(token)
                    else:
                        result = await asyncio.wait_for(
                            registry.execute(tool_name, **tool_args),
                            timeout=1,
                        )
            except Exception as e:
                logger.exception("Tool execution failed for '%s'", tool_name)
                audit_log(
                    "tool_call_error",
                    session_id=str(session_id),
                    tool_name=tool_name,
                    error=str(e),
                    duration_ms=int((time.monotonic() - start) * 1000),
                )
                result = f"Error: {e}"

            result_str = str(result)
            metrics.record_latency(f"tool.{tool_name}", (time.monotonic() - start) * 1000)
            metrics.increment_counter(f"tool.{tool_name}.calls")
            if result_str.startswith("Error:"):
                metrics.record_error(f"tool.{tool_name}")
            await plugin_registry.dispatch("on_tool_result", tool_name, result_str)
            audit_log(
                "tool_call_complete",
                session_id=str(session_id),
                tool_name=tool_name,
                duration_ms=int((time.monotonic() - start) * 1000),
            )
            yield StreamEvent(type="tool_result", tool_name=tool_name, tool_result=result_str)

            await context_manager.add_chunk(
                session_id=session_id,
                agent_id=self.agent_id,
                chunk_type="tool_call",
                payload={
                    "tool": tool_name,
                    "args": tool_args,
                    "result_preview": result_str[:500],
                },
                token_count=len(result_str) // 4,
            )
            tool_calls_made.append(
                {
                    "tool": tool_name,
                    "args": tool_args,
                    "result_preview": result_str[:200],
                }
            )
            self._append_tool_messages(messages, response, tc, result_str[:1000])

    def _schedule_memory_consolidation(self, session_id: uuid.UUID) -> None:
        """Schedule memory consolidation as a background task.

        Errors in the background task are logged but never propagated
        to avoid disrupting the user experience.
        """
        if not self.memory_consolidator:
            return
        task = asyncio.create_task(self._consolidate_memories(session_id))
        self._consolidation_tasks.add(task)
        task.add_done_callback(self._consolidation_tasks.discard)

    async def _consolidate_memories(self, session_id: uuid.UUID) -> None:
        """Consolidate session context into long-term memories.

        Called after each agent run as a background task. Failures are logged
        but never propagated to avoid disrupting the user experience.
        """
        try:
            new_memories = await self.memory_consolidator.consolidate_session(
                session_id=session_id,
                agent_id=self.agent_id,
            )
            if new_memories:
                logger.info(
                    "Consolidated %d new memories for session %s",
                    len(new_memories),
                    session_id,
                )
        except Exception as e:
            logger.warning("Memory consolidation failed for session %s: %s", session_id, e)

    def _schedule_learning_review(
        self, session_id: uuid.UUID, user_message: str, response: AgentResponse
    ) -> None:
        """Review successful turns in the background when explicitly enabled."""
        if not config.get("learning_review_enabled"):
            return
        if len(_pending_learning_reviews) >= MAX_PENDING_LEARNING_REVIEWS:
            logger.warning("Skipping learning review: process-wide review limit reached")
            return
        task = asyncio.create_task(self._review_learning(session_id, user_message, response))
        self._learning_tasks.add(task)
        _pending_learning_reviews.add(task)
        task.add_done_callback(self._learning_tasks.discard)
        task.add_done_callback(_pending_learning_reviews.discard)

    async def _review_learning(
        self, session_id: uuid.UUID, user_message: str, response: AgentResponse
    ) -> None:
        from ah.skills.learning import learning_reviewer

        try:
            await learning_reviewer.review_turn(
                session_id,
                self.agent_id,
                user_message,
                response.content,
                self.provider,
                tool_calls=response.tool_calls,
            )
        except Exception as exc:
            logger.warning("Learning review failed for session %s: %s", session_id, exc)


class ReActAgent(BaseReActAgent):
    """ReAct loop: Thought → Action → Observation.

    Provides both synchronous (run) and streaming (run_stream) execution.
    """

    @instrument_run
    async def run(
        self,
        session_id: uuid.UUID,
        user_message: str,
        verbose: bool = True,
    ) -> AgentResponse:
        """Run the ReAct loop for a user message."""
        # Audit log: session start
        audit_log("agent_run_start", session_id=str(session_id), agent_id=self.agent_id)

        # Prepare context (includes memory retrieval)
        session, messages = await self._prepare_context(
            session_id, user_message, include_memory=True
        )

        total_tokens = 0
        tool_calls_made = []

        for iteration in range(self.max_iterations):
            if verbose:
                console.print(f"[dim]Iteration {iteration + 1}/{self.max_iterations}[/dim]")

            # Check token budget before calling LLM (use session.context_budget)
            effective_budget = min(session.context_budget, MAX_TOKEN_BUDGET)
            if total_tokens >= effective_budget:
                logger.warning(
                    "Token budget exceeded (%d >= %d) — stopping agent loop",
                    total_tokens,
                    effective_budget,
                )
                audit_log(
                    "agent_run_budget_exceeded",
                    session_id=str(session_id),
                    total_tokens=total_tokens,
                    max_budget=effective_budget,
                )
                # Consolidate memories before returning
                self._schedule_memory_consolidation(session_id)
                return AgentResponse(
                    content="Reached maximum token budget. Partial results may be available.",
                    tool_calls=tool_calls_made,
                    tokens_used=total_tokens,
                    iterations=iteration,
                )

            # Get tool definitions
            tool_defs = self._tool_defs()

            # Call LLM with retry
            try:
                response = await self._call_llm_with_retry(
                    messages=messages,
                    tools=tool_defs,
                    session_id=session_id,
                )
            except UsageBudgetExceededError as e:
                audit_log("agent_run_budget_exceeded", session_id=str(session_id), error=str(e))
                return AgentResponse(
                    content=str(e),
                    tool_calls=tool_calls_made,
                    tokens_used=total_tokens,
                    iterations=iteration,
                )
            except Exception as e:
                logger.error("LLM call ultimately failed: %s", e)
                audit_log(
                    "agent_run_llm_error",
                    session_id=str(session_id),
                    error=str(e),
                    iteration=iteration,
                )
                # Consolidate memories before returning
                self._schedule_memory_consolidation(session_id)
                return AgentResponse(
                    content="LLM provider error. Check server logs.",
                    tool_calls=tool_calls_made,
                    tokens_used=total_tokens,
                    iterations=iteration + 1,
                )

            total_tokens += response.usage.get("total_tokens", 0)
            metrics.record_tokens(
                str(session_id),
                response.usage.get("prompt_tokens", 0),
                response.usage.get("completion_tokens", 0),
            )

            # Check for tool calls
            if not response.tool_calls:
                # No tool calls — final answer
                content = response.content or ""
                await context_manager.add_chunk(
                    session_id=session_id,
                    agent_id=self.agent_id,
                    chunk_type="assistant_message",
                    payload={"content": content},
                    token_count=len(content) // 4,
                )
                await session_manager.update_activity(session_id)
                audit_log(
                    "agent_run_complete",
                    session_id=str(session_id),
                    iterations=iteration + 1,
                    total_tokens=total_tokens,
                    tool_calls_count=len(tool_calls_made),
                )
                # Consolidate memories after successful run
                self._schedule_memory_consolidation(session_id)
                completed = AgentResponse(
                    content=content,
                    tool_calls=tool_calls_made,
                    tokens_used=total_tokens,
                    iterations=iteration + 1,
                )
                self._schedule_learning_review(session_id, user_message, completed)
                return completed

            # Execute tool calls
            await self._execute_tool_calls(response, messages, tool_calls_made, session_id, verbose)

        # Max iterations reached
        audit_log(
            "agent_run_max_iterations",
            session_id=str(session_id),
            max_iterations=self.max_iterations,
            total_tokens=total_tokens,
        )
        # Consolidate memories before returning
        self._schedule_memory_consolidation(session_id)
        return AgentResponse(
            content="Reached max iterations. Partial results may be available.",
            tool_calls=tool_calls_made,
            tokens_used=total_tokens,
            iterations=self.max_iterations,
        )

    @instrument_stream
    async def run_stream(
        self,
        session_id: uuid.UUID,
        user_message: str,
        verbose: bool = True,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Run the ReAct loop with streaming output.

        Yields StreamEvent objects:
        - type="text": content delta from LLM
        - type="tool_call": tool call started
        - type="tool_result": tool execution completed
        - type="token_usage": token count update
        - type="done": final AgentResponse available in .response
        """
        # Audit log: session start
        audit_log("agent_run_stream_start", session_id=str(session_id), agent_id=self.agent_id)

        # Prepare context (includes memory retrieval, same as run())
        session, messages = await self._prepare_context(
            session_id, user_message, include_memory=True
        )

        total_tokens = 0
        tool_calls_made = []

        for iteration in range(self.max_iterations):
            if verbose:
                console.print(f"[dim]Iteration {iteration + 1}/{self.max_iterations}[/dim]")

            # Check token budget before calling LLM (use session.context_budget)
            effective_budget = min(session.context_budget, MAX_TOKEN_BUDGET)
            if total_tokens >= effective_budget:
                logger.warning(
                    "Token budget exceeded (%d >= %d) — stopping agent loop",
                    total_tokens,
                    effective_budget,
                )
                audit_log(
                    "agent_run_budget_exceeded",
                    session_id=str(session_id),
                    total_tokens=total_tokens,
                    max_budget=effective_budget,
                )
                yield StreamEvent(
                    type="done",
                    response=AgentResponse(
                        content="Reached maximum token budget. Partial results may be available.",
                        tool_calls=tool_calls_made,
                        tokens_used=total_tokens,
                        iterations=iteration,
                    ),
                )
                return

            # Get tool definitions
            tool_defs = self._tool_defs()

            # Stream LLM call with retry
            try:
                final_response = None
                async for event in self._stream_llm_with_retry(
                    messages=messages,
                    tools=tool_defs,
                    session_id=session_id,
                ):
                    if event.type == "text":
                        yield StreamEvent(type="text", content=event.content)
                    elif event.type == "done":
                        final_response = event.response
                        total_tokens += event.response.usage.get("total_tokens", 0)
                        metrics.record_tokens(
                            str(session_id),
                            event.response.usage.get("prompt_tokens", 0),
                            event.response.usage.get("completion_tokens", 0),
                        )
                        yield StreamEvent(
                            type="token_usage",
                            tokens_used=event.response.usage.get("total_tokens", 0),
                        )

                if final_response is None:
                    raise RuntimeError("Stream completed without a final response")

            except UsageBudgetExceededError as e:
                audit_log("agent_run_budget_exceeded", session_id=str(session_id), error=str(e))
                yield StreamEvent(
                    type="done",
                    response=AgentResponse(
                        content=str(e),
                        tool_calls=tool_calls_made,
                        tokens_used=total_tokens,
                        iterations=iteration,
                    ),
                )
                return
            except Exception as e:
                logger.error("LLM stream ultimately failed: %s", e)
                audit_log(
                    "agent_run_llm_error",
                    session_id=str(session_id),
                    error=str(e),
                    iteration=iteration,
                )
                yield StreamEvent(
                    type="done",
                    response=AgentResponse(
                        content="LLM provider error. Check server logs.",
                        tool_calls=tool_calls_made,
                        tokens_used=total_tokens,
                        iterations=iteration + 1,
                    ),
                )
                return

            response = final_response

            # Check for tool calls
            if not response.tool_calls:
                # No tool calls — final answer
                content = response.content or ""
                await context_manager.add_chunk(
                    session_id=session_id,
                    agent_id=self.agent_id,
                    chunk_type="assistant_message",
                    payload={"content": content},
                    token_count=len(content) // 4,
                )
                await session_manager.update_activity(session_id)
                audit_log(
                    "agent_run_complete",
                    session_id=str(session_id),
                    iterations=iteration + 1,
                    total_tokens=total_tokens,
                    tool_calls_count=len(tool_calls_made),
                )
                self._schedule_memory_consolidation(session_id)
                completed = AgentResponse(
                    content=content,
                    tool_calls=tool_calls_made,
                    tokens_used=total_tokens,
                    iterations=iteration + 1,
                )
                self._schedule_learning_review(session_id, user_message, completed)
                yield StreamEvent(
                    type="done",
                    response=completed,
                )
                return

            # Execute tool calls with streaming events
            async for event in self._execute_tool_calls_stream(
                response, messages, tool_calls_made, session_id
            ):
                yield event

        # Max iterations reached
        audit_log(
            "agent_run_max_iterations",
            session_id=str(session_id),
            max_iterations=self.max_iterations,
            total_tokens=total_tokens,
        )
        self._schedule_memory_consolidation(session_id)
        yield StreamEvent(
            type="done",
            response=AgentResponse(
                content="Reached max iterations. Partial results may be available.",
                tool_calls=tool_calls_made,
                tokens_used=total_tokens,
                iterations=self.max_iterations,
            ),
        )
