"""Evidence projection: exact-duplicate removal and token-budgeted selection.

One coordinator for the flat evidence list the agent annotates prompts with
(memory facts, RAG excerpts, archived-context hits). Two deterministic passes:

1. **Exact deduplication.** Entries whose normalized text is byte-identical
   after whitespace folding are the same evidence; the first occurrence wins.
   Distinct versions, conflicting statements, and corroborating-but-different
   passages are preserved — only literal repetition is removed.
2. **Token-budgeted coverage.** Entries are kept in their incoming priority
   order (memory, then RAG, then archive) until the token budget is spent;
   later entries that do not fit are omitted and counted, never silently.
   Compaction/checkpoint summaries are the exception: they are protected
   evidence and always survive the budget pass.

The returned omission count is disclosed in the assembled prompt so a
budget-trimmed view is never mistaken for the full evidence set.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from machina.core.config import config
from machina.core.metrics import metrics
from machina.observability.diagnostics import record_failure

__all__ = ["dedupe_evidence", "select_evidence", "project_evidence", "project_retrieved"]

_WHITESPACE_RE = re.compile(r"\s+")

#: Chunk types that carry compaction/checkpoint state. They are protected
#: evidence: losing one silently erases the only record of pre-compaction
#: context, so the projector keeps them even under budget pressure.
_PROTECTED_CHUNK_TYPES = frozenset(
    {"compression_summary", "compaction_summary", "checkpoint_summary"}
)


def _is_protected_evidence(entry: Any) -> bool:
    """True for a compaction/checkpoint summary entry (dict row or chunk)."""
    chunk = _entry_chunk(entry)
    if isinstance(chunk, dict):
        type_value = chunk.get("type") or chunk.get("chunk_type")
        if type_value in _PROTECTED_CHUNK_TYPES:
            return True
        payload = chunk.get("payload")
        if isinstance(payload, dict):
            return (
                payload.get("type") in _PROTECTED_CHUNK_TYPES
                or payload.get("chunk_type") in _PROTECTED_CHUNK_TYPES
            )
        return False
    if getattr(chunk, "chunk_type", None) in _PROTECTED_CHUNK_TYPES:
        return True
    payload = getattr(chunk, "payload", None)
    if isinstance(payload, dict):
        return (
            payload.get("type") in _PROTECTED_CHUNK_TYPES
            or payload.get("chunk_type") in _PROTECTED_CHUNK_TYPES
        )
    return False


def _entry_chunk(entry: Any) -> Any:
    """The chunk inside a ``(chunk, score)`` tuple (or the entry itself)."""
    if isinstance(entry, tuple) and entry:
        return entry[0]
    return entry


def _entry_text(entry: Any) -> str:
    """Best-effort text of an evidence entry for identity and sizing."""
    chunk = _entry_chunk(entry)
    payload = getattr(chunk, "payload", None)
    if isinstance(chunk, dict):
        payload = chunk.get("payload", chunk)
    if isinstance(payload, dict):
        for key in ("content", "text", "result", "result_preview", "message", "prompt"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return str(payload)
    if isinstance(payload, str):
        return payload
    if isinstance(chunk, str):
        return chunk
    return str(chunk)


def _entry_tokens(entry: Any) -> int:
    """Token estimate for one entry: stored count when present, else len//4."""
    chunk = _entry_chunk(entry)
    stored = getattr(chunk, "token_count", None)
    if isinstance(stored, int) and not isinstance(stored, bool) and stored > 0:
        return stored
    return max(1, len(_entry_text(entry)) // 4)


def dedupe_evidence(entries: list[Any]) -> tuple[list[Any], int]:
    """Remove exact duplicates (whitespace-normalized) keeping first order.

    Returns ``(kept, removed_count)``.
    """
    kept: list[Any] = []
    seen: set[str] = set()
    removed = 0
    for entry in entries:
        text = _WHITESPACE_RE.sub(" ", _entry_text(entry)).strip()
        if not text:
            kept.append(entry)
            continue
        digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
        if digest in seen:
            removed += 1
            continue
        seen.add(digest)
        kept.append(entry)
    return kept, removed


def select_evidence(entries: list[Any], budget_tokens: int) -> tuple[list[Any], int]:
    """Keep entries in order until *budget_tokens* is spent.

    Compaction/checkpoint summaries are protected evidence: their cost is
    reserved up front and they are always kept, even when the summary alone
    exceeds the budget (the assembler may truncate it later, but the projector
    must never silently drop the only record of pre-compaction state).

    Other oversized entries are skipped (a smaller later entry may still fit)
    and counted as omitted. ``budget_tokens <= 0`` keeps everything. When
    nothing fits, the first entry is still kept so the assembler can truncate
    it — evidence must never disappear entirely because of a budget estimate.
    Returns ``(kept, omitted_count)``.
    """
    if budget_tokens <= 0:
        return list(entries), 0

    protected_cost = sum(_entry_tokens(entry) for entry in entries if _is_protected_evidence(entry))
    # Reserve the protected summary's share first: ordinary entries compete
    # only for what remains, so a summary appended later in the list is never
    # displaced by cheaper evidence ahead of it.
    ordinary_budget = max(0, budget_tokens - protected_cost)

    kept: list[Any] = []
    omitted = 0
    used = 0
    for entry in entries:
        if _is_protected_evidence(entry):
            kept.append(entry)
            continue
        cost = _entry_tokens(entry)
        if used + cost > ordinary_budget:
            omitted += 1
            continue
        kept.append(entry)
        used += cost
    if not kept and entries:
        return [entries[0]], max(0, omitted - 1)
    return kept, omitted


def project_evidence(entries: list[Any], budget_tokens: int) -> tuple[list[Any], int, int]:
    """Full pipeline: dedupe then budget. Returns ``(kept, dupes, omitted)``.

    Duplicates are not counted as omissions (they carried no unique
    information); only budget-dropped unique entries are disclosed.
    """
    deduped, duplicates = dedupe_evidence(entries)
    kept, omitted = select_evidence(deduped, budget_tokens)
    return kept, duplicates, omitted


def project_retrieved(entries: list[Any]) -> tuple[list[Any], int]:
    """Config-gated projection of the agent's retrieved-evidence list.

    Returns ``(kept_entries, omitted_count)``. Disabled, zero budget, empty
    input, or any failure returns the entries unchanged with zero omissions —
    retrieval selection is an enhancement and must never lose evidence
    because of its own error.
    """
    omitted = 0
    try:
        if not bool(config.get("retrieval_selection_enabled")) or not entries:
            return entries, 0
        budget = int(config.get("retrieval_token_budget") or 0)
        if not budget:
            return entries, 0
        kept, removed, omitted = project_evidence(entries, budget)
        if removed:
            metrics.increment_counter("retrieval.duplicates_removed", removed)
        if omitted:
            metrics.increment_counter("retrieval.entries_omitted", omitted)
        return kept, omitted
    except Exception as error:
        record_failure("projection.project_retrieved", error)
        return entries, 0
