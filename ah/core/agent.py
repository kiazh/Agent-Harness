"""ReAct agent loop — Thought → Action → Observation."""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Optional

from rich.console import Console

from ah.core.assembler import PromptAssembler
from ah.core.context import context_manager
from ah.core.models import AgentResponse, LLMResponse, StreamEvent, ToolDefinition
from ah.core.provider import LLMProvider, audit_log, get_provider
from ah.core.session import Session, session_manager
from ah.memory.consolidator import MemoryConsolidator
from ah.memory.retriever import MemoryRetriever
from ah.memory.store import memory_store
from ah.rag.pipeline import RAGPipeline
from ah.tools.base import registry

logger = logging.getLogger(__name__)

console = Console()

SYSTEM_PROMPT = """You are AgentHarness, a self-hosted AI agent. You have access to tools and persistent context stored in PostgreSQL.

When you need to do something, use the available tools. Think step by step:
1. Understand the task
2. Decide what tools to use
3. Execute and observe results
4. Repeat until done

Be concise. Don't over-explain. Get things done."""

# Maximum token budget for a single agent run
MAX_TOKEN_BUDGET = 50_000


class ReActAgent:
    """ReAct loop: Thought → Action → Observation."""

    def __init__(
        self,
        provider: LLMProvider | None = None,
        max_iterations: int = 10,
        agent_id: str = "harness",
        system_prompt: str | None = None,
        memory_retriever: MemoryRetriever | None = None,
        memory_consolidator: MemoryConsolidator | None = None,
        rag_pipeline: RAGPipeline | None = None,
    ) -> None:
        self.provider = provider or get_provider()
        self.max_iterations = max_iterations
        self.agent_id = agent_id
        self.system_prompt = system_prompt or SYSTEM_PROMPT
        self.memory_retriever = memory_retriever or MemoryRetriever(store=memory_store)
        self.memory_consolidator = memory_consolidator
        self.rag_pipeline = rag_pipeline

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
    ):
        """Call provider.complete() with exponential backoff retry.

        Retries up to 3 times on failure with delays of 1s, 2s, 4s.
        """
        last_exception: Exception | None = None
        for attempt in range(4):  # 1 initial + 3 retries
            try:
                return await self.provider.complete(
                    messages=messages,
                    tools=tools,
                )
            except Exception as e:
                last_exception = e
                if attempt < 3:
                    delay = 2 ** attempt  # 1s, 2s, 4s
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
    ) -> AsyncGenerator[StreamEvent, None]:
        """Call provider.stream_complete() with exponential backoff retry.

        Yields StreamEvent objects as they arrive from the provider.
        """
        last_exception: Exception | None = None
        for attempt in range(4):  # 1 initial + 3 retries
            try:
                async for event in self.provider.stream_complete(
                    messages=messages,
                    tools=tools,
                ):
                    yield event
                return  # Success — exit retry loop
            except Exception as e:
                last_exception = e
                if attempt < 3:
                    delay = 2 ** attempt  # 1s, 2s, 4s
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

    async def run(
        self,
        session_id: uuid.UUID,
        user_message: str,
        verbose: bool = True,
    ) -> AgentResponse:
        """Run the ReAct loop for a user message."""
        # Audit log: session start
        audit_log("agent_run_start", session_id=str(session_id), agent_id=self.agent_id)

        # Get session for context budget and goal
        session = await session_manager.get(session_id)
        if session is None:
            audit_log("agent_run_error", session_id=str(session_id), error="session_not_found")
            raise ValueError(f"Session {session_id} not found")

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

        # Retrieve relevant long-term memories
        retrieved_memories = []
        try:
            retrieved_memories = await self.memory_retriever.retrieve(
                query=user_message,
                agent_id=self.agent_id,
                limit=5,
            )
        except Exception as e:
            logger.warning("Memory retrieval failed: %s", e)

        # Convert retrieved memories to ContextChunk-like tuples for assembler
        from ah.core.models import ContextChunk
        retrieved_chunks = []
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
                },
                token_count=len(m.content) // 4,
            )
            retrieved_chunks.append((chunk, rm.score))

        # Retrieve RAG context if pipeline is configured
        rag_chunks = await self._get_rag_context(session_id, user_message)
        retrieved_chunks.extend(rag_chunks)

        # Assemble prompt
        prompt = assembler.assemble(
            system_prompt=self.system_prompt,
            goal=session.goal,
            recent_chunks=recent,
            retrieved_chunks=retrieved_chunks,
            query=user_message,
        )

        messages = [
            {"role": "user", "content": prompt},
        ]

        total_tokens = 0
        tool_calls_made = []

        for iteration in range(self.max_iterations):
            if verbose:
                console.print(f"[dim]Iteration {iteration + 1}/{self.max_iterations}[/dim]")

            # Check token budget before calling LLM
            if total_tokens >= MAX_TOKEN_BUDGET:
                logger.warning(
                    "Token budget exceeded (%d >= %d) — stopping agent loop",
                    total_tokens,
                    MAX_TOKEN_BUDGET,
                )
                audit_log(
                    "agent_run_budget_exceeded",
                    session_id=str(session_id),
                    total_tokens=total_tokens,
                    max_budget=MAX_TOKEN_BUDGET,
                )
                # Consolidate memories before returning
                await self._consolidate_memories(session_id)
                return AgentResponse(
                    content="Reached maximum token budget. Partial results may be available.",
                    tool_calls=tool_calls_made,
                    tokens_used=total_tokens,
                    iterations=iteration,
                )

            # Get tool definitions
            tool_defs = registry.get_tool_definitions()

            # Call LLM with retry
            try:
                response = await self._call_llm_with_retry(
                    messages=messages,
                    tools=tool_defs,
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
                await self._consolidate_memories(session_id)
                return AgentResponse(
                    content=f"LLM provider error: {e}",
                    tool_calls=tool_calls_made,
                    tokens_used=total_tokens,
                    iterations=iteration + 1,
                )

            total_tokens += response.usage.get("total_tokens", 0)

            # Check for tool calls
            if not response.tool_calls:
                # No tool calls — final answer
                await context_manager.add_chunk(
                    session_id=session_id,
                    agent_id=self.agent_id,
                    chunk_type="assistant_message",
                    payload={"content": response.content},
                    token_count=len(response.content) // 4,
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
                await self._consolidate_memories(session_id)
                return AgentResponse(
                    content=response.content,
                    tool_calls=tool_calls_made,
                    tokens_used=total_tokens,
                    iterations=iteration + 1,
                )

            # Execute tool calls
            for tc in response.tool_calls:
                # Handle missing tool name gracefully
                function_data = tc.get("function", {})
                tool_name = function_data.get("name")
                if not tool_name:
                    logger.warning("Tool call missing 'name' field: %s", tc)
                    audit_log(
                        "tool_call_invalid",
                        session_id=str(session_id),
                        error="missing_name",
                        raw_call=str(tc),
                    )
                    error_msg = "Error: tool call missing 'name' field"
                    messages.append({
                        "role": "assistant",
                        "content": response.content,
                        "tool_calls": [tc],
                    })
                    messages.append({
                        "role": "tool",
                        "content": error_msg,
                        "tool_call_id": tc.get("id", ""),
                    })
                    continue

                # Parse tool arguments with JSONDecodeError handling
                try:
                    tool_args = json.loads(function_data.get("arguments", "{}"))
                except json.JSONDecodeError as e:
                    logger.error(
                        "Failed to parse tool arguments for '%s': %s (raw: %s)",
                        tool_name,
                        e,
                        function_data.get("arguments", ""),
                    )
                    audit_log(
                        "tool_call_invalid_args",
                        session_id=str(session_id),
                        tool_name=tool_name,
                        error=str(e),
                        raw_args=function_data.get("arguments", ""),
                    )
                    error_msg = f"Error: invalid JSON in tool arguments: {e}"
                    messages.append({
                        "role": "assistant",
                        "content": response.content,
                        "tool_calls": [tc],
                    })
                    messages.append({
                        "role": "tool",
                        "content": error_msg,
                        "tool_call_id": tc.get("id", ""),
                    })
                    continue

                if verbose:
                    console.print(f"[yellow]  → {tool_name}({json.dumps(tool_args, default=str)[:80]})[/yellow]")

                # Audit log: tool execution start
                audit_log(
                    "tool_call_start",
                    session_id=str(session_id),
                    tool_name=tool_name,
                    tool_args=tool_args,
                )

                tool_start_time = time.monotonic()
                try:
                    result = await registry.execute(tool_name, **tool_args)
                except Exception as e:
                    logger.exception("Tool execution failed for '%s'", tool_name)
                    audit_log(
                        "tool_call_error",
                        session_id=str(session_id),
                        tool_name=tool_name,
                        error=str(e),
                        duration_ms=int((time.monotonic() - tool_start_time) * 1000),
                    )
                    result = f"Error: {e}"

                tool_duration_ms = int((time.monotonic() - tool_start_time) * 1000)
                audit_log(
                    "tool_call_complete",
                    session_id=str(session_id),
                    tool_name=tool_name,
                    duration_ms=tool_duration_ms,
                    result_preview=str(result)[:200],
                )

                result_str = str(result)
                if verbose:
                    preview = result_str[:200].replace("\n", " ")
                    console.print(f"[green]  ← {preview}[/green]")

                # Store tool call and result in context
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

                tool_calls_made.append({
                    "tool": tool_name,
                    "args": tool_args,
                    "result_preview": result_str[:200],
                })

                # Add tool result to messages for next iteration
                messages.append({
                    "role": "assistant",
                    "content": response.content,
                    "tool_calls": [tc],
                })
                messages.append({
                    "role": "tool",
                    "content": result_str[:1000],
                    "tool_call_id": tc.get("id", ""),
                })

        # Max iterations reached
        audit_log(
            "agent_run_max_iterations",
            session_id=str(session_id),
            max_iterations=self.max_iterations,
            total_tokens=total_tokens,
        )
        # Consolidate memories before returning
        await self._consolidate_memories(session_id)
        return AgentResponse(
            content="Reached max iterations. Partial results may be available.",
            tool_calls=tool_calls_made,
            tokens_used=total_tokens,
            iterations=self.max_iterations,
        )

    async def _consolidate_memories(self, session_id: uuid.UUID) -> None:
        """Consolidate session context into long-term memories.

        Called after each agent run. Failures are logged but never propagated
        to avoid disrupting the user experience.
        """
        if not self.memory_consolidator:
            return
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

        # Get session for context budget and goal
        session = await session_manager.get(session_id)
        if session is None:
            audit_log("agent_run_error", session_id=str(session_id), error="session_not_found")
            raise ValueError(f"Session {session_id} not found")

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

        # Retrieve RAG context if pipeline is configured
        rag_chunks = await self._get_rag_context(session_id, user_message)

        # Assemble prompt
        prompt = assembler.assemble(
            system_prompt=self.system_prompt,
            goal=session.goal,
            recent_chunks=recent,
            retrieved_chunks=rag_chunks,
            query=user_message,
        )

        messages = [
            {"role": "user", "content": prompt},
        ]

        total_tokens = 0
        tool_calls_made = []

        for iteration in range(self.max_iterations):
            if verbose:
                console.print(f"[dim]Iteration {iteration + 1}/{self.max_iterations}[/dim]")

            # Check token budget before calling LLM
            if total_tokens >= MAX_TOKEN_BUDGET:
                logger.warning(
                    "Token budget exceeded (%d >= %d) — stopping agent loop",
                    total_tokens,
                    MAX_TOKEN_BUDGET,
                )
                audit_log(
                    "agent_run_budget_exceeded",
                    session_id=str(session_id),
                    total_tokens=total_tokens,
                    max_budget=MAX_TOKEN_BUDGET,
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
            tool_defs = registry.get_tool_definitions()

            # Stream LLM call with retry
            try:
                final_response = None
                async for event in self._stream_llm_with_retry(
                    messages=messages,
                    tools=tool_defs,
                ):
                    if event.type == "text":
                        yield StreamEvent(type="text", content=event.content)
                    elif event.type == "done":
                        final_response = event.response
                        total_tokens += event.response.usage.get("total_tokens", 0)
                        yield StreamEvent(
                            type="token_usage",
                            tokens_used=event.response.usage.get("total_tokens", 0),
                        )

                if final_response is None:
                    raise RuntimeError("Stream completed without a final response")

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
                        content=f"LLM provider error: {e}",
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
                await context_manager.add_chunk(
                    session_id=session_id,
                    agent_id=self.agent_id,
                    chunk_type="assistant_message",
                    payload={"content": response.content},
                    token_count=len(response.content) // 4,
                )
                await session_manager.update_activity(session_id)
                audit_log(
                    "agent_run_complete",
                    session_id=str(session_id),
                    iterations=iteration + 1,
                    total_tokens=total_tokens,
                    tool_calls_count=len(tool_calls_made),
                )
                yield StreamEvent(
                    type="done",
                    response=AgentResponse(
                        content=response.content,
                        tool_calls=tool_calls_made,
                        tokens_used=total_tokens,
                        iterations=iteration + 1,
                    ),
                )
                return

            # Execute tool calls
            for tc in response.tool_calls:
                # Handle missing tool name gracefully
                function_data = tc.get("function", {})
                tool_name = function_data.get("name")
                if not tool_name:
                    logger.warning("Tool call missing 'name' field: %s", tc)
                    audit_log(
                        "tool_call_invalid",
                        session_id=str(session_id),
                        error="missing_name",
                        raw_call=str(tc),
                    )
                    error_msg = "Error: tool call missing 'name' field"
                    messages.append({
                        "role": "assistant",
                        "content": response.content,
                        "tool_calls": [tc],
                    })
                    messages.append({
                        "role": "tool",
                        "content": error_msg,
                        "tool_call_id": tc.get("id", ""),
                    })
                    continue

                # Parse tool arguments with JSONDecodeError handling
                try:
                    tool_args = json.loads(function_data.get("arguments", "{}"))
                except json.JSONDecodeError as e:
                    logger.error(
                        "Failed to parse tool arguments for '%s': %s (raw: %s)",
                        tool_name,
                        e,
                        function_data.get("arguments", ""),
                    )
                    audit_log(
                        "tool_call_invalid_args",
                        session_id=str(session_id),
                        tool_name=tool_name,
                        error=str(e),
                        raw_args=function_data.get("arguments", ""),
                    )
                    error_msg = f"Error: invalid JSON in tool arguments: {e}"
                    messages.append({
                        "role": "assistant",
                        "content": response.content,
                        "tool_calls": [tc],
                    })
                    messages.append({
                        "role": "tool",
                        "content": error_msg,
                        "tool_call_id": tc.get("id", ""),
                    })
                    continue

                yield StreamEvent(
                    type="tool_call",
                    tool_name=tool_name,
                    tool_args=tool_args,
                )

                # Audit log: tool execution start
                audit_log(
                    "tool_call_start",
                    session_id=str(session_id),
                    tool_name=tool_name,
                    tool_args=tool_args,
                )

                tool_start_time = time.monotonic()
                try:
                    result = await registry.execute(tool_name, **tool_args)
                except Exception as e:
                    logger.exception("Tool execution failed for '%s'", tool_name)
                    audit_log(
                        "tool_call_error",
                        session_id=str(session_id),
                        tool_name=tool_name,
                        error=str(e),
                        duration_ms=int((time.monotonic() - tool_start_time) * 1000),
                    )
                    result = f"Error: {e}"

                tool_duration_ms = int((time.monotonic() - tool_start_time) * 1000)
                audit_log(
                    "tool_call_complete",
                    session_id=str(session_id),
                    tool_name=tool_name,
                    duration_ms=tool_duration_ms,
                    result_preview=str(result)[:200],
                )

                result_str = str(result)

                yield StreamEvent(
                    type="tool_result",
                    tool_name=tool_name,
                    tool_result=result_str,
                )

                # Store tool call and result in context
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

                tool_calls_made.append({
                    "tool": tool_name,
                    "args": tool_args,
                    "result_preview": result_str[:200],
                })

                # Add tool result to messages for next iteration
                messages.append({
                    "role": "assistant",
                    "content": response.content,
                    "tool_calls": [tc],
                })
                messages.append({
                    "role": "tool",
                    "content": result_str[:1000],
                    "tool_call_id": tc.get("id", ""),
                })

        # Max iterations reached
        audit_log(
            "agent_run_max_iterations",
            session_id=str(session_id),
            max_iterations=self.max_iterations,
            total_tokens=total_tokens,
        )
        yield StreamEvent(
            type="done",
            response=AgentResponse(
                content="Reached max iterations. Partial results may be available.",
                tool_calls=tool_calls_made,
                tokens_used=total_tokens,
                iterations=self.max_iterations,
            ),
        )
