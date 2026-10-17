"""Prompt assembly and token counting."""

from __future__ import annotations

import logging
import threading
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


# ── deterministic fallback token heuristic ────────────────────────────────
#
# The fallback runs only when tiktoken is unavailable, fails to load, or
# rejects the text (literal special-token spellings such as ``<|endoftext|>``
# are ordinary user text to tiktoken's disallowed-special policy). The old
# ``len(text) // 4`` basis assumed ~4 ASCII characters per token, so it was
# ~4x low for CJK (≈1 token/char) and worse for emoji (2-3 tokens per
# codepoint); a short emoji-only string counted as 0.
#
# Documented heuristic (weights are quarter-tokens, summed then rounded up):
#
#   * emoji / pictographs / regional indicators .... 12 (≈3 tokens/char)
#   * variation selectors / ZWJ ....................  4 (1 token)
#   * CJK ideographs, CJK punctuation, fullwidth ...  4 (1 token)
#   * kana / hangul (jamo and syllables) ...........  3 (0.75 token)
#   * any other non-ASCII character ................  2 (0.5 token)
#   * ASCII ........................................  1 (¼ token, i.e. the
#     documented four-characters-per-token basis, rounded up)
#
# Crucially the fallback is DETERMINISTIC and never returns 0 for non-empty
# text. Measured against ``cl100k_base`` on the samples in
# ``tools/check_token_counter.py`` it stays within ±25% for dense scripts
# (CJK, kana, hangul, emoji, mixed) and is never 4x low; ASCII keeps the
# historic chars/4 basis. Tool results and prose may still be undercounted by
# up to roughly 2x, so the fallback is a degraded mode, not a replacement.
_WEIGHTED_CHAR_RANGES: tuple[tuple[int, int, int], ...] = (
    (0x1F000, 0x1FAFF, 12),  # emoji, pictographs, skin-tone modifiers
    (0x2600, 0x27BF, 12),  # miscellaneous symbols and dingbats (❤, ✅, ...)
    (0xFE00, 0xFE0F, 4),  # variation selectors (e.g. ❤️)
    (0x200D, 0x200D, 4),  # zero-width joiner (e.g. 🧑‍💻)
    (0x3400, 0x4DBF, 4),  # CJK unified ideographs extension A
    (0x4E00, 0x9FFF, 4),  # CJK unified ideographs
    (0xF900, 0xFAFF, 4),  # CJK compatibility ideographs
    (0x20000, 0x2FA1F, 4),  # CJK unified ideographs extensions B-F
    (0x3000, 0x303F, 4),  # CJK punctuation
    (0xFF00, 0xFFEF, 4),  # halfwidth and fullwidth forms
    (0x3040, 0x30FF, 3),  # hiragana and katakana
    (0x31F0, 0x31FF, 3),  # katakana phonetic extensions
    (0x1100, 0x11FF, 3),  # hangul jamo
    (0x3130, 0x318F, 3),  # hangul compatibility jamo
    (0xAC00, 0xD7AF, 3),  # hangul syllables
)

# Emitted once per process (not per call) so a degraded counter is visible
# without turning every token decision into log noise.
_fallback_notified = False
_fallback_notify_lock = threading.Lock()


def _char_quarter_tokens(char: str) -> int:
    """Quarter-token weight for one character (see the documented table)."""
    code = ord(char)
    if code < 0x80:
        return 1
    for start, end, weight in _WEIGHTED_CHAR_RANGES:
        if start <= code <= end:
            return weight
    return 2


def _fallback_token_count(text: str) -> int:
    """Deterministic offline estimate (no tiktoken); 0 only for empty text."""
    if not text:
        return 0
    if text.isascii():
        # Historic documented basis: one token per four characters, rounded up
        # so non-empty text can never count as zero tokens.
        return max(1, (len(text) + 3) // 4)
    quarters = 0
    for char in text:
        quarters += _char_quarter_tokens(char)
    return max(1, (quarters + 3) // 4)


def _fallback_prefix(text: str, max_tokens: int) -> str:
    """Longest character prefix whose fallback count fits *max_tokens*."""
    if max_tokens <= 0 or not text:
        return ""
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if _fallback_token_count(text[:middle]) <= max_tokens:
            low = middle
        else:
            high = middle - 1
    return text[:low]


def _notify_fallback(reason: str) -> None:
    """Emit one debug event the first time the fallback engages in a process.

    ``machina.observability.instrumentation`` is an optional, independently
    versioned dependency, so the import is lazy: counting must keep working
    (and importing ``assembler`` must stay DB/observability independent) when
    the module is absent. Exactly one notice per process avoids hot-path noise.
    """
    global _fallback_notified
    if _fallback_notified:
        return
    with _fallback_notify_lock:
        if _fallback_notified:
            return
        _fallback_notified = True
    try:
        from machina.observability.instrumentation import log_event
    except Exception as error:
        logger.debug("token counter fallback engaged (%s): no instrumentation (%s)", reason, error)
        return
    try:
        log_event("tokens.count", level="debug", basis="fallback", reason=reason)
    except Exception as error:
        # Observability must never break a token decision; report instead.
        logger.debug("tokens.count fallback event failed (%s): %s", reason, error)


class TokenCounter:
    """Accurate token counting using tiktoken, with a dense-script fallback.

    Thread safety: the tiktoken encoding is loaded lazily under a lock
    (double-checked), ``encode``/``decode`` on a loaded encoding are
    thread-safe, and all other state is per-call except the one-shot fallback
    notice (guarded by its own lock).
    """

    def __init__(self, encoding_name: str = "cl100k_base") -> None:
        self._encoding_name = encoding_name
        self._encoding: Any | None = None
        self._encoding_failed = False
        self._available = _TIKTOKEN_AVAILABLE
        self._init_lock = threading.Lock()

    def _get_encoding(self) -> Any | None:
        """Lazily load the configured encoding, or None when unavailable."""
        if not self._available:
            return None
        if self._encoding is not None:
            return self._encoding
        with self._init_lock:
            if self._encoding is None and not self._encoding_failed:
                try:
                    self._encoding = tiktoken.get_encoding(self._encoding_name)
                except Exception as error:
                    # A missing/corrupt BPE file is a real degradation: report
                    # it once, then keep the deterministic fallback.
                    self._encoding_failed = True
                    logger.warning(
                        "tiktoken encoding %r unavailable; using the documented "
                        "fallback token heuristic: %s",
                        self._encoding_name,
                        error,
                    )
        return self._encoding

    def _encode_or_none(self, text: str) -> list[int] | None:
        """Encode *text*, returning None when tiktoken rejects it.

        Literal special-token spellings (e.g. ``<|endoftext|>``) are ordinary
        user text, but tiktoken's default disallowed-special policy raises
        ValueError for them. Returning None lets callers fall back to the
        documented heuristic instead of failing the turn (usage.py applies the
        same conversion at its own call site).
        """
        encoding = self._get_encoding()
        if encoding is None:
            return None
        try:
            return encoding.encode(text)
        except ValueError:
            return None

    def _fallback_reason(self) -> str:
        if self._get_encoding() is None:
            return "tiktoken_unavailable"
        return "special_token_literal"

    def count(self, text: str) -> int:
        """Return the number of tokens in *text*."""
        tokens = self._encode_or_none(text)
        if tokens is not None:
            return len(tokens)
        reason = self._fallback_reason()
        _notify_fallback(reason)
        return _fallback_token_count(text)

    def truncate(self, text: str, max_tokens: int) -> str:
        """Return the longest prefix of *text* that fits in *max_tokens* tokens.

        Both the tiktoken and fallback paths binary-search character prefixes
        using the same counting basis, so the result is always a true prefix
        (never a decoded replacement character) and its count never exceeds
        the budget. A leading count short-circuits the common "already fits"
        case with a single encode.

        Instrumentation is deliberately NOT wrapped around this method: the
        structured sink costs ~445µs per call against a ~3µs token-decision
        baseline (measured), which would make the hot path ~100x slower. The
        one-shot fallback notice below is the only observability on this path.
        """
        if max_tokens <= 0 or not text:
            return ""
        if self.count(text) <= max_tokens:
            return text
        # Monotone (weights are positive), so the longest fitting prefix can
        # be found by binary search under the SAME counting basis used by
        # ``count`` — tiktoken when available, the documented heuristic else.
        low, high = 0, len(text)
        while low < high:
            middle = (low + high + 1) // 2
            if self.count(text[:middle]) <= max_tokens:
                low = middle
            else:
                high = middle - 1
        return text[:low]


_token_counter = TokenCounter()


def get_token_count(text: str) -> int:
    """Return the number of tokens in *text*.

    Canonical basis: ``tiktoken.get_encoding("cl100k_base").encode(text)``
    length. When tiktoken is unavailable or rejects the text (literal special
    tokens), the documented deterministic fallback (``_fallback_token_count``)
    is used instead.
    """
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
                    # Decoding a token prefix may split a multi-byte character
                    # and yield U+FFFD, which is NOT a prefix of *value* (and
                    # can re-encode differently). Walk back to the nearest clean
                    # character boundary; at most a few bytes/tokens are lost.
                    candidate = _token_counter._encoding.decode(encoded[:tokens])
                    while candidate and not value.startswith(candidate):
                        tokens -= 1
                        if tokens <= 0:
                            return ""
                        candidate = _token_counter._encoding.decode(encoded[:tokens])
                    return candidate
                return _fallback_prefix(value, tokens)

            value_tokens = (
                len(encoded) if encoded is not None else max(1, _fallback_token_count(value))
            )
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
        """Accurate token count via tiktoken (documented fallback if absent)."""
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
