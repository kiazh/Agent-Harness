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

    def truncate(self, text: str, max_tokens: int) -> str:
        """Return the longest prefix of *text* that fits in *max_tokens* tokens."""
        if max_tokens <= 0:
            return ""
        if self._encoding is not None:
            tokens = self._encoding.encode(text)
            if len(tokens) <= max_tokens:
                return text
            return self._encoding.decode(tokens[:max_tokens])
        return text[: max_tokens * 4]


_token_counter = TokenCounter()


def get_token_count(text: str) -> int:
    """Return the number of tokens in *text* (tiktoken or len//4 fallback)."""
    return _token_counter.count(text)


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Truncate *text* to at most *max_tokens* tokens."""
    return _token_counter.truncate(text, max_tokens)


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

        Keep the system instructions and current query first, then use any
        remaining space for the goal, recent activity, and retrieved context.
        """
        budget = max(1, self.session_budget)
        sections: dict[str, str] = {}
        order = ("system", "goal", "recent", "retrieved", "query")

        def render() -> str:
            return "\n".join(sections[key] for key in order if sections.get(key))

        def add(key: str, value: str, cap: int | None = None) -> None:
            if not value:
                return
            encoding = _token_counter._encoding
            encoded = encoding.encode(value) if encoding is not None else None

            def prefix(tokens: int) -> str:
                if encoded is not None:
                    return encoding.decode(encoded[:tokens])
                return value[: tokens * 4]

            value_tokens = len(encoded) if encoded is not None else max(1, (len(value) + 3) // 4)
            upper = min(value_tokens, cap) if cap is not None else value_tokens
            if upper == value_tokens:
                sections[key] = value
                if self._estimate_tokens(render()) <= budget:
                    return
            lower = 0
            # Joining sections can change token boundaries, so measure the
            # final prompt while searching for the longest fitting prefix.
            while lower < upper:
                middle = (lower + upper + 1) // 2
                sections[key] = prefix(middle)
                if self._estimate_tokens(render()) <= budget:
                    lower = middle
                else:
                    upper = middle - 1
            if lower:
                sections[key] = prefix(lower)
            else:
                sections.pop(key, None)

        query_text = f"\n\n## Current Query\n{query}"
        # Reserve up to half the budget for the user's current request.
        system_cap = max(1, budget - min(self._estimate_tokens(query_text), budget // 2))
        add("system", system_prompt, system_cap)
        add("query", query_text)
        if goal:
            add("goal", f"\n\n## Current Goal\n{goal}")
        if recent_chunks:
            recent = "\n\n## Recent Activity\n" + "\n".join(
                self._compress_chunk(chunk) for chunk in reversed(recent_chunks[:3])
            )
            add("recent", recent)
        if retrieved_chunks:
            relevant = "\n\n## Relevant Context\n" + "\n".join(
                self._compress_chunk({"type": chunk.chunk_type, "payload": chunk.payload})
                for chunk, _score in retrieved_chunks
            )
            add("retrieved", relevant)
        return render()

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
            limit = 500 if payload.get("agent") else 200
            if isinstance(result, str) and len(result) > limit:
                result = result[:limit] + "..."
            return f"  -> {status}: {result}"
        elif chunk_type == "memory":
            return f"[memory] {payload.get('content', '')}"
        elif chunk_type == "document":
            source = payload.get("source", payload.get("metadata", {}).get("source", ""))
            return f"[document: {source}] {payload.get('text', '')}"
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
