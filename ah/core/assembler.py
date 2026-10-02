"""Prompt assembly and token counting."""
from __future__ import annotations

import logging
from typing import Any

from ah.core.models import ContextChunk

try:
    import tiktoken

    _TIKTOKEN_AVAILABLE = True
except ImportError:
    _TIKTOKEN_AVAILABLE = False

logger = logging.getLogger(__name__)


class TokenCounter:
    """Accurate token counting using tiktoken, with len//4 fallback."""

    def __init__(self, encoding_name: str = "cl100k_base") -> None:
        self._encoding = None
        if _TIKTOKEN_AVAILABLE:
            try:
                self._encoding = tiktoken.get_encoding(encoding_name)
            except Exception:
                self._encoding = None

    def count(self, text: str) -> int:
        """Return the number of tokens in *text*."""
        if self._encoding is not None:
            return len(self._encoding.encode(text))
        return len(text) // 4


_token_counter = TokenCounter()


def get_token_count(text: str) -> int:
    """Return the number of tokens in *text* (tiktoken or len//4 fallback)."""
    return _token_counter.count(text)


class PromptAssembler:
    """Assembles prompts from context chunks with token budget."""

    def __init__(self, session_budget: int = 8000) -> None:
        self.session_budget = session_budget

    def assemble(
        self,
        system_prompt: str,
        goal: str | None,
        recent_chunks: list[dict[str, Any]],
        retrieved_chunks: list[tuple[ContextChunk, float]],
        query: str,
    ) -> str:
        """Assemble a prompt within the token budget.

        Strategy:
        1. Always include system prompt + goal + query
        2. Add recent chunks (compressed)
        3. Fill remaining budget with retrieved chunks by relevance
        """
        parts = []
        used_tokens = 0

        # System prompt (always included)
        parts.append(system_prompt)
        used_tokens += self._estimate_tokens(system_prompt)

        # Goal
        if goal:
            goal_text = f"\n\n## Current Goal\n{goal}"
            parts.append(goal_text)
            used_tokens += self._estimate_tokens(goal_text)

        # Current query
        query_text = f"\n\n## Current Query\n{query}"
        parts.append(query_text)
        used_tokens += self._estimate_tokens(query_text)

        # Recent chunks (last 3 actions, compressed)
        if recent_chunks:
            recent_text = "\n\n## Recent Activity\n"
            for chunk_data in recent_chunks[:3]:
                compressed = self._compress_chunk(chunk_data)
                recent_text += compressed + "\n"
            parts.append(recent_text)
            used_tokens += self._estimate_tokens(recent_text)

        # Retrieved chunks (fill remaining budget)
        remaining = self.session_budget - used_tokens
        if retrieved_chunks and remaining > 100:
            retrieved_text = "\n\n## Relevant Context\n"
            for chunk, sim in retrieved_chunks:
                compressed = self._compress_chunk({
                    "type": chunk.chunk_type,
                    "payload": chunk.payload,
                })
                chunk_tokens = self._estimate_tokens(compressed)
                if chunk_tokens > remaining:
                    compressed = compressed[:remaining * 4]
                    retrieved_text += compressed + "...\n"
                    break
                retrieved_text += compressed + "\n"
                remaining -= chunk_tokens
                if remaining < 50:
                    break
            parts.append(retrieved_text)

        return "\n".join(parts)

    def _compress_chunk(self, chunk_data: dict[str, Any]) -> str:
        """Compress a context chunk into minimal text for the LLM."""
        chunk_type = chunk_data.get("type", "unknown")
        payload = chunk_data.get("payload", {})

        if chunk_type in ("tool_call", "user_message"):
            tool = payload.get("tool", payload.get("content", "unknown"))
            args = payload.get("args", {})
            if args:
                args_str = ", ".join(f"{k}={v}" for k, v in args.items())
                return f"[{chunk_type}] {tool}({args_str})"
            return f"[{chunk_type}] {tool}"
        elif chunk_type in ("result", "assistant_message"):
            status = payload.get("status", "ok")
            result = payload.get("result", payload.get("content", ""))
            if isinstance(result, str) and len(result) > 200:
                result = result[:200] + "..."
            return f"  -> {status}: {result}"
        elif chunk_type == "memory":
            return f"[memory] {payload.get('content', '')}"
        elif chunk_type == "heartbeat":
            return f"[heartbeat] {payload.get('prompt', '')}"
        elif chunk_type == "system":
            return f"[system] {payload.get('message', '')}"
        elif chunk_type == "user":
            return f"[user] {payload.get('content', '')}"
        elif chunk_type == "assistant":
            return f"[assistant] {payload.get('content', '')}"
        else:
            return f"[{chunk_type}] {str(payload)[:100]}"

    def _estimate_tokens(self, text: str) -> int:
        """Accurate token count via tiktoken (falls back to len//4)."""
        return get_token_count(text)


def compress_context_chunks(
    chunks: list[ContextChunk],
    session_id: Any,
    agent_id: str,
    llm_provider: Any = None,
    config: Any = None,
) -> Any:
    """Compress context chunks using the ContextCompressor.

    This is a convenience function that creates a ContextCompressor and
    compresses the given chunks.

    Args:
        chunks: The context chunks to compress (newest first).
        session_id: The session ID.
        agent_id: The agent ID.
        llm_provider: Optional LLM provider for summarization.
        config: Optional CompressionConfig.

    Returns:
        CompressionResult with compressed chunks and metadata.
    """
    from ah.core.compression import ContextCompressor

    compressor = ContextCompressor(config=config)
    return compressor.compress(chunks, session_id, agent_id, llm_provider=llm_provider)
