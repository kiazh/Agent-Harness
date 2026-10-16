"""Prompt assembly and token counting."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from machina.core.checkpoint import validate_checkpoint
from machina.core.models import ContextChunk

__all__ = [
    "TokenCounter",
    "PromptAssembler",
    "get_token_count",
    "truncate_to_tokens",
    "compress_context_chunks",
]

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

    def _encode_or_none(self, text: str) -> list[int] | None:
        """Encode *text*, returning None when tiktoken rejects it.

        Literal special-token spellings (e.g. ``<|endoftext|>``) are ordinary
        user text, but tiktoken's default disallowed-special policy raises
        ValueError for them. Returning None lets callers fall back to the
        length heuristic instead of failing the turn (usage.py applies the
        same conversion at its own call site).
        """
        if self._encoding is None:
            return None
        try:
            return self._encoding.encode(text)
        except ValueError:
            return None

    def count(self, text: str) -> int:
        """Return the number of tokens in *text*."""
        tokens = self._encode_or_none(text)
        if tokens is not None:
            return len(tokens)
        return len(text) // 4

    def truncate(self, text: str, max_tokens: int) -> str:
        """Return the longest prefix of *text* that fits in *max_tokens* tokens."""
        if max_tokens <= 0:
            return ""
        tokens = self._encode_or_none(text)
        if tokens is not None:
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
        *,
        omitted_evidence: int = 0,
    ) -> str:
        """Assemble a prompt within the token budget.

        Keep the system instructions and current query first, then use any
        remaining space for the goal, the latest compaction summary, recent
        activity, and retrieved context — in that priority order.
        ``omitted_evidence`` discloses budget-dropped retrieved entries so a
        trimmed section is never mistaken for the full evidence set.
        """
        budget = max(1, self.session_budget)
        sections: dict[str, str] = {}
        order = ("system", "goal", "summary", "recent", "retrieved", "query")

        def render() -> str:
            return "\n".join(sections[key] for key in order if sections.get(key))

        def add(key: str, value: str, cap: int | None = None) -> None:
            if not value:
                return
            # None when the value contains a literal special token; the
            # char-based fallback below keeps assembly alive (m2).
            encoded = _token_counter._encode_or_none(value)

            def prefix(tokens: int) -> str:
                if encoded is not None:
                    return _token_counter._encoding.decode(encoded[:tokens])
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
        # H04: the latest compaction summary/checkpoint is reserved ahead of
        # raw recent chunks and retrieved context — it is the only remaining
        # record of pre-compaction state, and the newest-three recent window
        # can never select it (the compressor stamps it behind the preserved
        # recent chunks). ``add`` still truncates it to fit the authoritative
        # token budget.
        summary_chunk = self._latest_compaction_summary(recent_chunks, retrieved_chunks)
        if summary_chunk is not None:
            add("summary", f"\n\n## Compacted Context\n{self._compress_chunk(summary_chunk)}")
        if recent_chunks:
            recent_parts = [
                self._compress_chunk(chunk)
                for chunk in reversed(recent_chunks[:3])
                if not self._is_compaction_summary(chunk)
            ]
            if recent_parts:
                add("recent", "\n\n## Recent Activity\n" + "\n".join(recent_parts))
        if retrieved_chunks or omitted_evidence:
            # Summaries are owned by the reserved section above; rendering
            # them again here would duplicate the compacted context.
            relevant_parts = [
                self._compress_chunk({"type": chunk.chunk_type, "payload": chunk.payload})
                for chunk, _score in retrieved_chunks
                if not self._is_compaction_summary(chunk)
            ]
            if relevant_parts or omitted_evidence:
                # The omission note leads the section: sections are fitted by
                # cutting a prefix, so a trailing note would be the first
                # thing lost under budget pressure — exactly when the
                # disclosure matters.
                note = (
                    f"[{omitted_evidence} lower-ranked retrieved excerpts omitted "
                    f"by the token budget]\n"
                    if omitted_evidence
                    else ""
                )
                section = "\n\n## Relevant Context\n" + note + "\n".join(relevant_parts)
                add("retrieved", section)
        return render()

    @staticmethod
    def _is_compaction_summary(entry: Any) -> bool:
        """True for a ``compression_summary`` dict row or ContextChunk."""
        if isinstance(entry, dict):
            return entry.get("type") == "compression_summary"
        return getattr(entry, "chunk_type", None) == "compression_summary"

    @staticmethod
    def _summary_payload(entry: Any) -> dict[str, Any] | None:
        """Return the payload dict of a compaction summary entry, else None."""
        if isinstance(entry, dict):
            payload = entry.get("payload")
        else:
            payload = getattr(entry, "payload", None)
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _summary_is_usable(payload: dict[str, Any]) -> bool:
        """A summary must carry free-form content or a (renderable) checkpoint."""
        content = payload.get("content")
        if isinstance(content, str) and content.strip():
            return True
        return isinstance(payload.get("checkpoint"), dict)

    def _latest_compaction_summary(
        self,
        recent_chunks: list[dict[str, Any]],
        retrieved_chunks: list[tuple[ContextChunk, float]],
    ) -> dict[str, Any] | None:
        """Latest compaction summary available for the reserved section.

        ``recent_chunks`` is newest-first (``get_recent_context`` ordering):
        the first usable summary is the latest live one. Archived summaries
        from retrieved context participate only when the live window no
        longer carries one; they are ordered newest-first by
        ``created_at`` (id as the deterministic tiebreak) so a keyword recall
        of an old summary cannot displace a newer one.
        """
        for entry in recent_chunks:
            if not self._is_compaction_summary(entry):
                continue
            payload = self._summary_payload(entry)
            if payload is not None and self._summary_is_usable(payload):
                return {"type": "compression_summary", "payload": payload}
        candidates: list[tuple[datetime, str, dict[str, Any]]] = []
        for entry in retrieved_chunks:
            chunk = entry[0] if isinstance(entry, tuple) and entry else entry
            if not self._is_compaction_summary(chunk):
                continue
            payload = self._summary_payload(chunk)
            if payload is None or not self._summary_is_usable(payload):
                continue
            created = getattr(chunk, "created_at", None)
            if isinstance(created, datetime) and created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            candidates.append(
                (created or datetime(1, 1, 1, tzinfo=UTC), str(getattr(chunk, "id", "")), payload)
            )
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return {"type": "compression_summary", "payload": candidates[0][2]}

    def _compress_chunk(self, chunk_data: dict[str, Any]) -> str:
        """Compress a context chunk into minimal text for the LLM."""
        chunk_type = chunk_data.get("type", "unknown")
        payload = chunk_data.get("payload", {})

        if chunk_type == "compression_summary":
            return self._render_compaction_summary(payload)
        if chunk_type in ("tool_call", "user_message"):
            tool = payload.get("tool", payload.get("content", "unknown"))
            args = payload.get("args", {})
            if args:
                args_str = ", ".join(f"{k}={v}" for k, v in args.items())
                base = f"[{chunk_type}] {tool}({args_str})"
            else:
                base = f"[{chunk_type}] {tool}"
            if chunk_type != "tool_call":
                return base
            return base + self._artifact_pointer(payload)
        elif chunk_type in ("result", "assistant_message"):
            status = payload.get("status", "ok")
            result = payload.get("result", payload.get("content", ""))
            limit = 500 if payload.get("agent") else 200
            if isinstance(result, str) and len(result) > limit:
                result = result[:limit] + "..."
            return f"  -> {status}: {result}"
        elif chunk_type == "memory":
            fact = f"[memory fact] {payload.get('content', '')}"
            interpretation = payload.get("persona_interpretation")
            return fact + (f"\n[persona interpretation] {interpretation}" if interpretation else "")
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

    # Bound on the rendered preview of a prior-turn tool_call's stored result
    # (B0/G4): the chunk's `result` may be up to 65k chars; a stored handle
    # must never pull that into the rebuilt context.
    _ARTIFACT_PREVIEW_CHARS = 400

    @staticmethod
    def _artifact_pointer(payload: dict[str, Any]) -> str:
        """Render a tool_call's stored result + retained-artifact pointer (B0/G4).

        Prior turns kept the call shape only, so a persisted
        ``artifact_handle`` was never surfaced back into rebuilt context and
        recovery beyond the assembled window was unreachable. This renders a
        bounded preview of the stored result plus the handle. It claims only
        what the record holds: a capped stored copy is disclosed, and
        availability is resolved by ``artifact_read`` under the caller's ACL —
        nothing here fabricates content for a revoked or expired handle
        (§14.6). Chunks without a handle render exactly as before.
        """
        handle = payload.get("artifact_handle")
        if not isinstance(handle, str) or not handle:
            return ""
        preview = payload.get("result_preview")
        if not isinstance(preview, str) or not preview:
            result = payload.get("result")
            preview = result if isinstance(result, str) else ""
        lines: list[str] = []
        capped = preview[: PromptAssembler._ARTIFACT_PREVIEW_CHARS]
        if capped:
            if len(preview) > len(capped):
                capped += "…"
            lines.append(f"  -> {capped}")
        note = f"  [artifact {handle}: retained result; artifact_read pages it if still available"
        if payload.get("result_truncated"):
            note += "; the stored copy above is truncated below the retained result"
        note += "]"
        lines.append(note)
        return "\n" + "\n".join(lines)

    def _render_compaction_summary(self, payload: dict[str, Any]) -> str:
        """Render a compaction summary explicitly (H04).

        Free-form summary text is never reduced to a payload-dict preview;
        a persisted checkpoint is validated first and its full structured
        content is appended (an incomplete checkpoint is rejected with a
        warning instead of being used silently). The section budget, not this
        renderer, bounds the final text.
        """
        parts: list[str] = []
        content = payload.get("content")
        if isinstance(content, str) and content:
            parts.append(content)
        checkpoint = payload.get("checkpoint")
        if isinstance(checkpoint, dict):
            try:
                validated = validate_checkpoint(checkpoint)
            except ValueError:
                logger.warning("invalid compaction checkpoint dropped from prompt")
            else:
                lines = [
                    f"objective: {validated['objective']}",
                    f"artifact: {validated['expected_artifact']}",
                    f"constraints: {'; '.join(validated['constraints']) or '(none)'}",
                    f"read-only scope: {validated['read_only_scope'] or '(none)'}",
                ]
                for finding in validated["findings"]:
                    lines.append(f"finding ({finding['count']}): {finding['finding']}")
                if validated["unresolved_questions"]:
                    lines.append("open: " + "; ".join(validated["unresolved_questions"]))
                lines.append(f"next: {validated['next_action']}")
                lines.append("Latest explicit user instruction overrides this checkpoint.")
                parts.append("\n".join(lines))
        if not parts:
            return f"[compression_summary] {str(payload)[:100]}"
        return "\n".join(parts)

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
    from machina.core.compression import ContextCompressor

    compressor = ContextCompressor(config=config)
    return compressor.compress(chunks, session_id, agent_id, llm_provider=llm_provider)
