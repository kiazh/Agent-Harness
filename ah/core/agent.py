"""ReAct agent loop — Thought → Action → Observation."""

from __future__ import annotations

import asyncio
import inspect
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
# Shared per-session consolidation single-flight (LP-14): concurrent agent
# instances working one session never re-extract the same range.
_consolidation_inflight: dict[str, asyncio.Task] = {}

# Per-tool execution timeouts. Delegation-style tools fan out to sub-agents
# and legitimately run long; everything else must answer quickly to keep
# turns responsive. The default must exceed the slowest legitimate tool
# (web_search/web_extract allow 10-30s, terminal 60s, RAG embeddings longer) —
# a 1s cap aborted healthy network tools, which then cost *another* LLM call
# to react to a failure that never happened.
_TOOL_TIMEOUTS: dict[str, float] = {
    "delegate": 120.0,
    "share_memory": 60.0,
    "web_extract": 45.0,
    "index_document": 120.0,
    "terminal": 60.0,
}
_TOOL_TIMEOUT_DEFAULT = 30.0

# Independent tool calls in one assistant message run concurrently, capped so a
# model that emits a large batch cannot spawn unbounded subprocesses / queries.
_MAX_PARALLEL_TOOLS = 8


def _is_rate_limit_error(exc: BaseException) -> bool:
    """True for HTTP 429 failures (duck-typed so no httpx import is needed).

    The provider layer already retried these with backoff, so the agent
    layer must not retry them again.
    """
    return getattr(getattr(exc, "response", None), "status_code", None) == 429


def _is_transient_error(exc: BaseException) -> bool:
    """True for HTTP 502, 503, 504 failures (transient server errors)."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return status in (502, 503, 504)


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
            self._last_rag_mode = getattr(self, "_last_rag_mode", "disabled")
            return []

        try:
            results = await self.rag_pipeline.search(
                query=query,
                session_id=session_id,
                top_k=5,
                rerank=True,
            )
            self._last_rag_mode = "ready" if results else "empty-index"
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

        Retries once on transient errors (502, 503, 504) with a 1s delay.
        """
        last_exception: Exception | None = None
        for attempt in range(2):  # 1 initial + 1 retry
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
                if _is_rate_limit_error(e):
                    # Provider already retried 429s with backoff — re-raising
                    # here avoids amplifying one rate limit into many requests.
                    raise
                if not _is_transient_error(e):
                    raise
                if attempt < 1:
                    delay = 2**attempt  # 1s
                    logger.warning(
                        "LLM call failed (attempt %d/%d): %s — retrying in %ds",
                        attempt + 1,
                        2,
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
        Retries once on transient errors (502, 503, 504) with a 1s delay.
        """
        last_exception: Exception | None = None
        for attempt in range(2):  # 1 initial + 1 retry
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
                if _is_rate_limit_error(e):
                    # Provider already retried 429s with backoff — don't amplify.
                    raise
                if not _is_transient_error(e):
                    raise
                if attempt < 1:
                    delay = 2**attempt  # 1s
                    logger.warning(
                        "LLM stream failed (attempt %d/%d): %s — retrying in %ds",
                        attempt + 1,
                        2,
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
        pending_chunks: list[dict[str, Any]] | None = None,
    ) -> tuple[Session, list[dict[str, Any]]]:
        """Prepare session, context, and prompt messages.

        Handles session retrieval, user message storage, recent context retrieval,
        memory retrieval (optional), RAG retrieval, and prompt assembly.

        Args:
            session_id: The session ID.
            user_message: The user's message.
            include_memory: Whether to include memory retrieval. Defaults to True.
            pending_chunks: If provided, chunks are accumulated here instead of
                being inserted immediately. The caller is responsible for flushing.

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
        if pending_chunks is not None:
            pending_chunks.append(
                {
                    "session_id": session_id,
                    "agent_id": self.agent_id,
                    "chunk_type": "user_message",
                    "payload": {"content": user_message},
                    "token_count": len(user_message) // 4,
                }
            )
        else:
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
        self._last_retrieval_mode = "disabled"

        # Retrieve relevant long-term memories (optional). memory_enabled
        # gates auto-retrieval; explicit remember/recall tools stay available
        # when disabled.
        memory_enabled = True
        try:
            memory_enabled = bool(config.get("memory_enabled"))
        except Exception:
            pass
        if include_memory and memory_enabled:
            try:
                from ah.memory.embeddings import embed_query

                query_embedding = await embed_query(user_message)
                self._last_retrieval_mode = "dense+sparse" if query_embedding else "keyword-only"
                retrieved_memories = await self.memory_retriever.retrieve(
                    query=user_message,
                    agent_id=self.agent_id,
                    session_id=session_id,
                    emotion=session.state.get("emotion"),
                    query_embedding=query_embedding,
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

        # Retrieve RAG context if enabled AND material exists. Never scans or
        # uploads host files implicitly — only previously indexed session docs.
        rag_enabled = True
        try:
            rag_enabled = bool(config.get("rag_enabled"))
        except Exception:
            pass
        if rag_enabled:
            rag_chunks = await self._get_rag_context(session_id, user_message)
        else:
            rag_chunks = []
            self._last_rag_mode = "disabled"
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
        pending_chunks: list[dict[str, Any]] | None = None,
    ) -> None:
        """Execute tool calls for the non-streaming loop.

        Thin wrapper over :meth:`_execute_tool_calls_stream` (the single
        implementation) that prints progress when *verbose* is set.
        """
        async for event in self._execute_tool_calls_stream(
            response, messages, tool_calls_made, session_id, pending_chunks
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
        pending_chunks: list[dict[str, Any]] | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Execute tool calls from an LLM response, yielding StreamEvents.

        Shared by ``run()`` and ``run_stream()``. Handles missing tool names,
        invalid JSON arguments, execution with timing, audit logging, context
        storage, and message updates. Modifies *messages* and
        *tool_calls_made* in place.

        Independent tool calls in one assistant message run **concurrently**
        (bounded by ``_MAX_PARALLEL_TOOLS``) instead of one after another, so a
        batch of N reads costs roughly max(latency) rather than sum(latency).
        Every ``tool_call`` event is emitted up front, then results are
        replayed in the original order so the transcript stays deterministic.

        If *pending_chunks* is provided, tool_call chunks are accumulated there
        instead of being inserted immediately. The caller is responsible for
        flushing them via ``context_manager.add_chunks_batch``.
        """
        # ── Phase A: parse and validate the whole batch ─────────────────────
        planned: list[dict[str, Any]] = []
        for idx, tc in enumerate(response.tool_calls):
            function_data = tc.get("function", {})
            tool_name = function_data.get("name")
            call_id = str(tc.get("id") or f"call-{idx}")
            if not tool_name:
                logger.warning("Tool call missing 'name' field: %s", tc)
                audit_log(
                    "tool_call_invalid",
                    session_id=str(session_id),
                    error="missing_name",
                )
                planned.append(
                    {
                        "tc": tc,
                        "name": None,
                        "args": {},
                        "result": "Error: tool call missing 'name' field",
                        "call_id": call_id,
                    }
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
                planned.append(
                    {
                        "tc": tc,
                        "name": None,
                        "args": {},
                        "result": f"Error: invalid tool arguments: {e}",
                        "call_id": call_id,
                    }
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
                planned.append(
                    {
                        "tc": tc,
                        "name": tool_name,
                        "args": tool_args,
                        "result": result_str,
                        "call_id": call_id,
                    }
                )
                continue

            planned.append(
                {"tc": tc, "name": tool_name, "args": tool_args, "result": None, "call_id": call_id}
            )

        # ── Phase B: emit starts BEFORE execution (AH-020) ────────────────
        # Long-running tools must appear as running while executing, not only
        # after gather() finishes. Starts are emitted in request order with
        # stable call IDs; completions follow as each finishes.
        for item in planned:
            if item["name"] is None:
                continue
            # Denied tools (result already set) still get start+complete for
            # UI consistency — they complete immediately below.
            yield StreamEvent(
                type="tool_call",
                tool_name=item["name"],
                tool_args=item["args"],
                tool_call_id=item["call_id"],
            )

        # ── Phase C: execute concurrently, yielding completions as they finish
        async def _run_one(item: dict[str, Any]) -> None:
            tool_name = item["name"]
            tool_args = item["args"]
            # Denied/invalid already have results — skip execution.
            if item["result"] is not None and item["name"] is not None and item.get("_denied"):
                return
            if item["result"] is not None and item["name"] is not None:
                # Denied tools: result preset, no execution needed. Still
                # record metrics below; skip provider call.
                # Mark elapsed 0 so Phase D handles them uniformly.
                item["elapsed_ms"] = 0.0
                return
            audit_log("tool_call_start", session_id=str(session_id), tool_name=tool_name)
            start = time.monotonic()
            tool_timeout = _TOOL_TIMEOUTS.get(tool_name, _TOOL_TIMEOUT_DEFAULT)
            try:
                with span("agent.tool", tool_name=tool_name, session_id=str(session_id)):
                    await plugin_registry.dispatch("on_tool_call", tool_name, tool_args)
                    # AH-018: bind caller scope around EVERY tool (not just a
                    # subset) so RAG tools and future tools see the active
                    # session/agent. Tokens always reset in finally.
                    from ah.tools.agents import current_agent_id, current_session_id

                    token = current_session_id.set(session_id)
                    agent_token = current_agent_id.set(self.agent_id)
                    # LP-08: publish this agent's authority for delegation
                    # children (restored afterwards like all caller scope).
                    try:
                        from ah.core.agent_factory import parent_authority_var as _pav

                        authority_token = _pav.set({"tools": self.allowed_tools})
                    except Exception:
                        authority_token = None
                    try:
                        # Phase D: permission broker gates every tool path.
                        # Validation errors stay distinct from authorization:
                        # malformed actions fail here before any approval.
                        from ah.permissions import tools as _perm_tools
                        from ah.permissions.broker import (
                            ApprovalDenied as _Denied,
                        )
                        from ah.permissions.broker import NeedsApproval as _Needs
                        from ah.permissions.broker import permission_broker as _broker
                        from ah.permissions.policy import build_request as _build_req

                        _spec = _perm_tools.action_for_tool(tool_name, tool_args)
                        try:
                            from ah.core.config import config as _cfg

                            _mode = _cfg.get("execution_mode") or "ask"
                            _sandbox_cfg = (_cfg.get("terminal_sandbox") or "disabled").lower()
                        except Exception:
                            _mode, _sandbox_cfg = "ask", "disabled"
                        _backend = (
                            "sandbox"
                            if (_mode == "sandbox" or _sandbox_cfg == "docker")
                            and tool_name == "terminal"
                            else "host"
                        )
                        _req = _build_req(
                            operation=_spec.get("operation", f"tool.{tool_name}"),
                            targets=_spec.get("targets"),
                            argv=_spec.get("argv"),
                            shell_payload=_spec.get("shell_payload", ""),
                            cwd=_spec.get("cwd", "."),
                            content=_spec.get("content", ""),
                            mode=_mode,
                            backend=_backend,
                            agent_id=self.agent_id,
                            session_id=str(session_id),
                            capabilities=_spec.get("capabilities"),
                            network=_spec.get("network"),
                            tool=tool_name,
                        )
                        try:
                            await _broker.guard(_req)
                            item["_approval_id"] = _req.approval_id or ""
                        except _Needs as _need:
                            _ap = _need.approval
                            result = (
                                f"Needs approval ({_ap.get('request_id')}:{_ap.get('operation')} "
                                f"{_ap.get('target')}). The action was NOT executed. "
                                "Ask the user to approve it (approvals list/resolve), "
                                "or choose another path."
                            )
                            item["result"] = str(result)
                            item["elapsed_ms"] = (time.monotonic() - start) * 1000
                            return
                        except _Denied as _den:
                            result = (
                                f"Permission denied: {_den.reason}. The action was NOT executed."
                            )
                            item["result"] = str(result)
                            item["elapsed_ms"] = (time.monotonic() - start) * 1000
                            return
                        try:
                            result = await asyncio.wait_for(
                                registry.execute(tool_name, **tool_args),
                                timeout=tool_timeout,
                            )
                        except asyncio.CancelledError:
                            # LP-01 lifecycle: a claimed approval must not hang
                            # in 'claimed' when its execution is cancelled.
                            _aid = item.get("_approval_id", "")
                            if _aid:
                                try:
                                    await asyncio.shield(_broker.complete(_aid, "cancelled"))
                                except Exception:
                                    pass
                            raise
                    finally:
                        try:
                            if authority_token is not None:
                                from ah.core.agent_factory import parent_authority_var as _pav2

                                _pav2.reset(authority_token)
                        except Exception:
                            pass
                        current_agent_id.reset(agent_token)
                        current_session_id.reset(token)
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
            item["result"] = str(result)
            item["elapsed_ms"] = (time.monotonic() - start) * 1000

        # Mark denied items so _run_one skips execution but Phase D still emits.
        for item in planned:
            if item["name"] is not None and item["result"] is not None:
                item["_denied"] = True

        runnable = [
            item for item in planned if item["name"] is not None and not item.get("_denied")
        ]
        completion_order: list[dict[str, Any]] = []
        if runnable:
            semaphore = asyncio.Semaphore(_MAX_PARALLEL_TOOLS)

            async def _bounded(item: dict[str, Any]) -> dict[str, Any]:
                async with semaphore:
                    await _run_one(item)
                return item

            tasks = [asyncio.create_task(_bounded(item)) for item in runnable]
            try:
                for coro in asyncio.as_completed(tasks):
                    try:
                        finished = await coro
                    except Exception:
                        continue
                    completion_order.append(finished)
                    # Yield completion immediately (out-of-order OK — call ID
                    # pairs it with its start). Metrics + persistence happen
                    # in Phase D in provider-required original order.
                    tool_name = finished["name"]
                    result_str = finished["result"]
                    if "elapsed_ms" in finished:
                        metrics.record_latency(f"tool.{tool_name}", finished["elapsed_ms"])
                        metrics.increment_counter(f"tool.{tool_name}.calls")
                        if result_str.startswith("Error:"):
                            metrics.record_error(f"tool.{tool_name}")
                    yield StreamEvent(
                        type="tool_result",
                        tool_name=tool_name,
                        tool_result=result_str,
                        tool_call_id=finished["call_id"],
                    )
            finally:
                # Ensure no task leaks on cancellation.
                for t in tasks:
                    if not t.done():
                        t.cancel()
                # Do not await here (we are yielding); Phase D handles records.

        # ── Phase D: metrics for denied + persistence in provider order ───
        # Provider message order must follow the original tool_calls order
        # (OpenAI requires tool replies in order), even though UI completions
        # above may have arrived out of order.
        for item in planned:
            tool_name = item["name"]
            tool_args = item["args"]
            result_str = item["result"]
            tc = item["tc"]

            if tool_name is None:
                # Missing name / unparsable args: no events, just the reply.
                self._append_tool_messages(messages, response, tc, result_str)
                continue

            if item.get("_denied"):
                metrics.increment_counter(f"tool.{tool_name}.calls")
                metrics.record_error(f"tool.{tool_name}")
                await plugin_registry.dispatch("on_tool_result", tool_name, result_str)
                audit_log(
                    "tool_call_complete",
                    session_id=str(session_id),
                    tool_name=tool_name,
                    duration_ms=0,
                )
                # Denied completions were not yielded in Phase C (no
                # execution); emit them now in order.
                yield StreamEvent(
                    type="tool_result",
                    tool_name=tool_name,
                    tool_result=result_str,
                    tool_call_id=item["call_id"],
                )
            else:
                # Executed tools: metrics already recorded at completion time;
                # still dispatch result hooks + audit once (idempotent).
                if "elapsed_ms" in item and not item.get("_hooked"):
                    # Phase C recorded latency/counters; dispatch hooks here
                    # once in original order for determinism.
                    await plugin_registry.dispatch("on_tool_result", tool_name, result_str)
                    audit_log(
                        "tool_call_complete",
                        session_id=str(session_id),
                        tool_name=tool_name,
                        duration_ms=int(item["elapsed_ms"]),
                    )
                    item["_hooked"] = True
                # LP-01 lifecycle: claimed → completed/failed exactly once.
                _aid = item.get("_approval_id", "")
                if _aid and not item.get("_approval_done"):
                    item["_approval_done"] = True
                    try:
                        from ah.permissions.broker import permission_broker as _broker2

                        outcome = "failed" if result_str.startswith("Error:") else "completed"
                        await _broker2.complete(_aid, outcome)
                    except Exception:
                        pass

            if pending_chunks is not None:
                pending_chunks.append(
                    {
                        "session_id": session_id,
                        "agent_id": self.agent_id,
                        "chunk_type": "tool_call",
                        "payload": {
                            "tool": tool_name,
                            "args": tool_args,
                            "result_preview": result_str[:500],
                        },
                        "token_count": len(result_str) // 4,
                    }
                )
            else:
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

    async def _flush_pending_chunks(self, pending_chunks: list[dict[str, Any]]) -> None:
        """Insert all pending chunks in a single batch operation."""
        if pending_chunks:
            result = context_manager.add_chunks_batch(pending_chunks)
            if inspect.isawaitable(result):
                await result
            pending_chunks.clear()

    async def _flush_pending_cancellation_safe(
        self,
        pending_chunks: list[dict[str, Any]],
        session_id: uuid.UUID,
        note: str = "interrupted",
    ) -> None:
        """Flush pending history on cancellation without losing writes (AH-015).

        Uses a shielded, bounded flush so CancelledError during a turn does
        not drop the user message or completed tool records. Appends an
        explicit interruption marker instead of misrepresenting partial
        effects as complete.
        """
        if pending_chunks:
            try:
                # Shield so the flush itself is not cancelled mid-write.
                await asyncio.shield(
                    asyncio.wait_for(self._flush_pending_chunks(list(pending_chunks)), timeout=10.0)
                )
                pending_chunks.clear()
            except Exception as e:
                logger.warning("Cancellation-safe flush failed: %s", e)
        # Record the interruption explicitly (best-effort, bounded).
        try:
            await asyncio.shield(
                asyncio.wait_for(
                    context_manager.add_chunk(
                        session_id=session_id,
                        agent_id=self.agent_id,
                        chunk_type="assistant_message",
                        payload={
                            "content": f"[{note}] Turn interrupted; partial effects above are retained."
                        },
                        token_count=20,
                    ),
                    timeout=10.0,
                )
            )
        except Exception as e:
            logger.warning("Could not record interruption marker: %s", e)

    def _schedule_memory_consolidation(self, session_id: uuid.UUID) -> None:
        """Schedule memory consolidation as a bounded background task.

        Bounded and shared: one consolidation per SESSION across all agent
        instances (LP-14); skipped when memory or consolidation is disabled.
        Watermark cursor (see _consolidate_memories) keeps repeated runs
        idempotent.
        """
        if not self.memory_consolidator:
            return
        try:
            if not config.get("memory_consolidation_enabled"):
                return
        except Exception:
            pass
        key = str(session_id)
        running = _consolidation_inflight.get(key)
        if running is not None and not running.done():
            logger.debug("consolidation already running for session, skipping duplicate")
            return
        if any(not t.done() for t in self._consolidation_tasks):
            logger.debug("consolidation already running, skipping duplicate")
            return
        task = asyncio.create_task(self._consolidate_memories(session_id))
        _consolidation_inflight[key] = task
        self._consolidation_tasks.add(task)
        task.add_done_callback(self._consolidation_tasks.discard)

        def _drop(_t: asyncio.Task) -> None:
            if _consolidation_inflight.get(key) is _t:
                _consolidation_inflight.pop(key, None)

        task.add_done_callback(_drop)

    async def _consolidate_memories(self, session_id: uuid.UUID) -> None:
        """Consolidate new session context into long-term memories.

        Cursor-filtered (LP-14): only chunks newer than
        ``mem_consolidated_up_to`` are extracted; the cursor advances past a
        range only after its writes succeed, so failed ranges retry instead
        of being skipped or duplicated.
        """
        try:
            from datetime import UTC

            session = await session_manager.get(session_id)
            cursor = (session.state or {}).get("mem_consolidated_up_to") if session else None
            since = None
            if cursor and cursor.get("id"):
                try:
                    since_time = cursor.get("at")
                    if isinstance(since_time, str):
                        text = (
                            since_time[:-1] + "+00:00" if since_time.endswith("Z") else since_time
                        )
                        from datetime import datetime as _dt

                        since_time = _dt.fromisoformat(text)
                        if since_time.tzinfo is None:
                            since_time = since_time.replace(tzinfo=UTC)
                    import uuid as _uuid

                    since = (since_time, _uuid.UUID(str(cursor["id"])))
                except Exception:
                    since = None
            try:
                newest = await context_manager.get_chunks(session_id, limit=1)
            except Exception:
                newest = []
            if since is not None:
                try:
                    pending = await context_manager.get_chunks_since(
                        session_id,
                        since_time=since[0],
                        since_id=since[1],
                        limit=1,
                    )
                    if not pending:
                        logger.debug("consolidation cursor unchanged, skipping")
                        return
                except Exception:
                    pass
            elif cursor and newest and str(newest[0].id) == str(cursor.get("id")):
                logger.debug("consolidation cursor unchanged, skipping")
                return
            new_memories = await self.memory_consolidator.consolidate_session(
                session_id=session_id,
                agent_id=self.agent_id,
                since=since,
            )
            if newest and session is not None:
                try:
                    state = dict(session.state or {})
                    state["mem_consolidated_up_to"] = {
                        "id": str(newest[0].id),
                        "at": newest[0].created_at.isoformat() if newest[0].created_at else None,
                    }
                    await session_manager.update_state(session_id, state)
                except Exception as e:
                    logger.debug("consolidation cursor persist failed: %s", e)
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
        pending_chunks: list[dict[str, Any]] = []
        session, messages = await self._prepare_context(
            session_id, user_message, include_memory=True, pending_chunks=pending_chunks
        )
        # AH-015: persist the user message immediately so cancellation during
        # the first LLM call cannot lose it.
        await self._flush_pending_chunks(pending_chunks)

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
                # Flush any pending chunks before returning
                await self._flush_pending_chunks(pending_chunks)
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
                await self._flush_pending_chunks(pending_chunks)
                audit_log("agent_run_budget_exceeded", session_id=str(session_id), error=str(e))
                return AgentResponse(
                    content=str(e),
                    tool_calls=tool_calls_made,
                    tokens_used=total_tokens,
                    iterations=iteration,
                )
            except Exception as e:
                await self._flush_pending_chunks(pending_chunks)
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
                pending_chunks.append(
                    {
                        "session_id": session_id,
                        "agent_id": self.agent_id,
                        "chunk_type": "assistant_message",
                        "payload": {"content": content},
                        "token_count": len(content) // 4,
                    }
                )
                await self._flush_pending_chunks(pending_chunks)
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
            try:
                await self._execute_tool_calls(
                    response, messages, tool_calls_made, session_id, verbose, pending_chunks
                )
            except asyncio.CancelledError:
                # AH-015: preserve user/tool records accumulated so far;
                # do not misrepresent partial effects as complete.
                await self._flush_pending_cancellation_safe(pending_chunks, session_id)
                raise
            # AH-015: incremental persistence — flush each batch so a later
            # cancellation loses at most the in-flight batch, not all history.
            await self._flush_pending_chunks(pending_chunks)

        # Max iterations reached
        audit_log(
            "agent_run_max_iterations",
            session_id=str(session_id),
            max_iterations=self.max_iterations,
            total_tokens=total_tokens,
        )
        # Flush any pending chunks before returning
        await self._flush_pending_chunks(pending_chunks)
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
        pending_chunks: list[dict[str, Any]] = []
        session, messages = await self._prepare_context(
            session_id, user_message, include_memory=True, pending_chunks=pending_chunks
        )
        # AH-015: persist the user message immediately; run_stream otherwise
        # holds everything in pending until the final done event, so a
        # CancelledError would drop the entire turn. Incremental flushing
        # below preserves tool records as well.
        try:
            await self._flush_pending_chunks(pending_chunks)
        except asyncio.CancelledError:
            await self._flush_pending_cancellation_safe(pending_chunks, session_id)
            raise

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
                await self._flush_pending_chunks(pending_chunks)
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
                await self._flush_pending_chunks(pending_chunks)
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
                await self._flush_pending_chunks(pending_chunks)
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
                pending_chunks.append(
                    {
                        "session_id": session_id,
                        "agent_id": self.agent_id,
                        "chunk_type": "assistant_message",
                        "payload": {"content": content},
                        "token_count": len(content) // 4,
                    }
                )
                await self._flush_pending_chunks(pending_chunks)
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

            # Execute tool calls with streaming events.
            # AH-015: run_stream previously held ALL history in pending until
            # the final done event — a CancelledError bypassed `except
            # Exception` and dropped user/tool records. Flush incrementally
            # and, on cancellation, persist what completed with an explicit
            # interruption marker.
            try:
                async for event in self._execute_tool_calls_stream(
                    response, messages, tool_calls_made, session_id, pending_chunks
                ):
                    yield event
            except asyncio.CancelledError:
                await self._flush_pending_cancellation_safe(pending_chunks, session_id)
                raise
            # Incremental persistence: do not wait for done.
            try:
                await self._flush_pending_chunks(pending_chunks)
            except asyncio.CancelledError:
                await self._flush_pending_cancellation_safe(pending_chunks, session_id)
                raise

        # Max iterations reached
        audit_log(
            "agent_run_max_iterations",
            session_id=str(session_id),
            max_iterations=self.max_iterations,
            total_tokens=total_tokens,
        )
        await self._flush_pending_chunks(pending_chunks)
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
