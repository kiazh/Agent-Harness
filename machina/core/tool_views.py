"""Bounded model-visible views for oversized tool results (T2/T3 seams).

Extracted from the agent loop (A5: ``agent.py`` must not grow past its
ratified ceiling). The agent delegates here at the tool-append seam; every
failure path is fail-open toward the legacy blind cap — but never silent.

**Store-before-lossy-publish.** A reducer view drops content; it is only
published when the exact original was durably retained first (artifact handle
in the disclosure). Without a stored original — store disabled, unavailable,
quota-refused, oversize — the caller keeps the bounded blind cap, now carrying
an explicit ``no_artifact`` marker that names the loss as unrecoverable (C06).
A failed store never publishes a view that claims recovery.

**Upstream-truncation honesty (B0/G1).** Audited producers append an in-band
marker when they truncate their own output (terminal, read_file,
learn_directory). A marked result is stored with
``upstream_truncated=true``/``complete=false`` and never receives a reducer
view, whose disclosure claims an exact original (C07). Unmarked results from
audited producers are positively complete; untabulated producers are recorded
as unknown and are never reported complete.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import uuid
from typing import Any

from machina import __version__
from machina.core.artifacts import artifact_disclosure, artifact_store
from machina.core.config import config
from machina.core.metrics import metrics
from machina.core.prompt import tool_message_limit
from machina.core.reducers import reduce_tool_result
from machina.observability.diagnostics import record_failure

__all__ = [
    "bounded_tool_view",
    "artifact_source_version",
    "append_limit",
    "ensure_item_artifact_view",
    "acknowledged_chunk_ids",
    "reconcile_pending_flush",
    "flush_pending_chunks",
    "REDUCER_EXEMPT_TOOLS",
    "mark_turn_accepted",
]

logger = logging.getLogger(__name__)

# Tools whose output is already bounded and continuation/paging-addressed;
# their append caps must never be replaced by a reducer view because offsets
# into them must keep resolving (skill pages, status snapshots, document
# search, and artifact paging itself — reducing an artifact page would defeat
# the recovery path it exists to serve).
REDUCER_EXEMPT_TOOLS = frozenset(
    {
        "skill_read",
        "skill_resource",
        "harness_status",
        "search_documents",
        "artifact_read",
        "artifact_search",
        "artifact_describe",
    }
)

# Exempt tools still obey an append cap (a runaway page must not bypass it).
# Artifact paging is page-aware: artifact_read's own default page is 4000
# chars (its maximum is 20000), so a default page passes through while an
# oversized one is bounded without ever being replaced by a reducer view.
_EXEMPT_PAGE_LIMITS = {
    "artifact_read": 4000,
    "artifact_search": 4000,
    "artifact_describe": 4000,
}

# Audited upstream-truncation contracts (B0/G1): each entry is the exact
# in-band marker a producer appends when it truncated its own output. Verified
# in source: tools/terminal.py ("\\n[output truncated]"), tools/file.py
# read_file ("\\n[read_file: showing lines "), tools/repomap.py
# ("\\n[digest truncated]"). A tabled tool whose result lacks its marker is
# positively complete; any untabulated tool stays unknown (None) and is never
# recorded as complete. search_documents is intentionally absent: it is
# reducer-exempt and never reaches this store seam.
_UPSTREAM_TRUNCATION_MARKERS = {
    "terminal": "\n[output truncated]",
    "read_file": "\n[read_file: showing lines ",
    "learn_directory": "\n[digest truncated]",
}


def _upstream_truncated(tool_name: str, result_str: str) -> bool | None:
    """``True``/``False`` for audited producers, ``None`` when unknown."""
    marker = _UPSTREAM_TRUNCATION_MARKERS.get(tool_name)
    if marker is None:
        return None
    return marker in result_str


def artifact_source_version() -> str:
    """Harness-build stamp for retained artifacts (B0/G3, §14.6).

    No per-tool source revision exists at the tool-append seam, so the honest
    version available there is the build that produced the result; recovery
    cursors bind it alongside the store-derived ``content_revision`` content
    hash. Kept here rather than inline in the agent loop so a one-constant
    change does not grow the A5 god-file ceiling.
    """
    return f"machina:{__version__}"


def _append_limit(tool_name: str) -> int:
    """Append-time cap for *tool_name*, page-aware for exempt paging tools."""
    limit = tool_message_limit(tool_name)
    if tool_name in REDUCER_EXEMPT_TOOLS:
        limit = max(limit, _EXEMPT_PAGE_LIMITS.get(tool_name, 0))
    return limit


def append_limit(tool_name: str) -> int:
    """Model-visible cap a page-producing tool must budget its envelope to (C5).

    Exposed so ``artifact_read``/``artifact_search`` can size header + body +
    cursor to exactly the space the append seam will grant; without this the
    append cap would blind-slice a cursor-bearing page and lose its
    continuation pointer.
    """
    return _append_limit(tool_name)


# Page-aware tools whose emitted envelope must never be blind-sliced: a cut
# header or a cut cursor line lies about what was emitted (C5). Oversized
# pages come back as a bounded refusal instead; the tool re-runs smaller.
_PAGE_REFUSAL_TOOLS = frozenset({"artifact_read", "artifact_search"})


def _page_refusal(tool_name: str, limit: int, total: int) -> str:
    """Bounded refusal marker for a page too large for the visible budget (C5)."""
    marker = (
        f"[{tool_name}: {total}-char page exceeds the {limit}-char model-visible "
        f"budget; re-run {tool_name} with a smaller limit]"
    )
    return marker[:limit]


def _flags() -> tuple[bool, bool]:
    """Read the reducer/artifact switches; broken config degrades safely."""
    try:
        return bool(config.get("reducers_enabled")), bool(config.get("artifacts_enabled"))
    except Exception:
        return True, True


def _blind_cap(result_str: str, limit: int, *, reason: str) -> str:
    """Bounded blind cap with an explicit unrecoverable marker (C06).

    The marker is appended to the kept prefix (the established disclosure
    shape) so it survives later condensation; it states exactly what was
    kept, that the omitted text cannot be recovered this turn, and the honest
    remedy — re-run the tool. No artifact path is claimed.
    """
    kept = min(len(result_str), max(0, limit))
    marker = (
        f"\n[no_artifact: {reason}; showing {kept} of {len(result_str)} chars — "
        f"the omitted text is unrecoverable, re-run the tool for the full output]"
    )
    return result_str[:kept] + marker


async def bounded_tool_view(
    tool_name: str,
    result_str: str,
    *,
    session_id: uuid.UUID,
    agent_id: str,
    status: str | None = None,
    source_version: str | None = None,
) -> tuple[str, str | None]:
    """Return ``(model_visible_view, artifact_id_or_None)`` for one result.

    Small results pass through verbatim. Oversized results are retained as
    artifacts (durable originals) and rendered through the deterministic
    format reducers with their omission/recovery disclosure; when the store is
    unavailable or reduction fails, the legacy blind cap is kept — marked
    explicitly as unrecoverable — and no recovery is claimed.

    ``source_version`` stamps the record so recovery cursors bind the version
    observed at capture time; the store derives ``content_revision`` as a
    content hash and records the audited producer-truncation state.
    """
    try:
        limit = _append_limit(tool_name)
        if len(result_str) <= limit:
            return result_str, None
        if tool_name in REDUCER_EXEMPT_TOOLS:
            # Already bounded/paging-addressed: never a reducer view, but the
            # append cap still applies so an oversized page cannot slip
            # through verbatim. The page header leads the result, so
            # continuation offsets remain resolvable after the bound, and no
            # unrecoverable marker is added — smaller pages can be re-read.
            if len(result_str) <= limit:
                return result_str, None
            if tool_name in _PAGE_REFUSAL_TOOLS:
                # C5: a page envelope carries its own header/cursor contract;
                # blind-slicing it emits a partial cursor line (or cuts the
                # cursor away entirely) and lies about the emitted range.
                metrics.increment_counter("reducers.page_refused")
                return _page_refusal(tool_name, limit, len(result_str)), None
            return result_str[:limit], None
        reducers_enabled, artifacts_enabled = _flags()
        upstream = _upstream_truncated(tool_name, result_str)
        # Tri-state: True only when the producer is audited complete; unknown
        # provenance stays None so nothing is ever silently complete (C07).
        complete = None if upstream is None else (not upstream)
        artifact_id: str | None = None
        if artifacts_enabled:
            stored = await artifact_store.store(
                f"tool:{tool_name}",
                result_str,
                session_id=session_id,
                agent_id=agent_id,
                status=status,
                source_version=source_version,
                upstream_truncated=upstream,
                complete=complete,
            )
            if stored is not None:
                artifact_id = stored["artifact_id"]
        if artifact_id is None:
            # No durable original: publish nothing recoverable. The legacy
            # cap stays bounded and carries an explicit unrecoverable marker;
            # a failed/refused/disabled store must never look like a normal
            # result (C06).
            metrics.increment_counter("reducers.fallback_cap")
            reason = (
                "artifact retention is disabled"
                if not artifacts_enabled
                else "the artifact store refused or was unavailable"
            )
            return _blind_cap(result_str, limit, reason=reason), None
        if reducers_enabled and upstream is False:
            # Reducer disclosures claim the exact original; only a positively
            # complete producer may take a reducer view (C07). Unknown or
            # upstream-truncated results keep the graded plain disclosure.
            reduction = reduce_tool_result(
                result_str,
                budget_chars=limit,
                artifact_id=artifact_id,
                source_version=source_version,
            )
            if reduction.changed:
                metrics.increment_counter(f"reducers.{reduction.method}")
                return reduction.output, artifact_id
        metrics.increment_counter("reducers.plain_cap")
        disclosure = artifact_disclosure(
            artifact_id,
            len(result_str),
            upstream_truncated=upstream,
            complete=complete,
        )
        return result_str[:limit] + disclosure, artifact_id
    except Exception as error:
        # Fail-open: bounded blind cap, no artifact claim. When a handle was
        # already stored, it is only surfaced if the disclosure can be built;
        # a broken path never fabricates recovery and marks the loss (C06).
        record_failure("tool_views.bounded_tool_view", error)
        return (
            _blind_cap(
                result_str,
                tool_message_limit(tool_name),
                reason="artifact retention failed",
            ),
            None,
        )


async def ensure_item_artifact_view(
    item: dict[str, Any],
    session_id: uuid.UUID,
    agent_id: str,
    *,
    status: str | None = None,
) -> str:
    """Retain an action's original once and cache its model-visible view (MCH-010).

    The retention decision — and therefore the artifact handle — is made BEFORE
    any durable record can claim the action, so the pending payload and the
    durable ``tool_call`` chunk always carry the same link. A refused or failed
    store keeps the bounded legacy view and claims no recovery; repeated calls
    reuse the cached view.
    """
    view = item.get("_artifact_view")
    if view is not None:
        return view
    view, artifact_id = await bounded_tool_view(
        item["name"],
        item["result"],
        session_id=session_id,
        agent_id=agent_id,
        status=status,
        source_version=artifact_source_version(),
    )
    item["_artifact_view"] = view
    if artifact_id:
        item["_artifact_handle"] = artifact_id
    return view


def acknowledged_chunk_ids(rows: Any) -> set[str]:
    """Chunk ids the store reported as durably written (C1)."""
    try:
        iterator = iter(rows or ())
    except TypeError:
        # Opaque consumer result (older fakes/mocks): no explicit acks.
        return set()
    acknowledged: set[str] = set()
    for row in iterator:
        row_id = row.get("id") if isinstance(row, dict) else getattr(row, "id", None)
        if row_id is not None:
            acknowledged.add(str(row_id))
    return acknowledged


async def _verify_persisted_chunk_ids(db: Any, chunk_ids: list[Any]) -> set[str]:
    """SELECT-by-id confirmation for an ambiguous batch acknowledgement (C1).

    A connection that dropped after the INSERT still persisted the rows, so an
    ambiguous success is verified before anything leaves the pending queue.
    Unverifiable ids stay queued for the next flush (fail-open policy).
    """
    try:
        keys = [cid if isinstance(cid, uuid.UUID) else uuid.UUID(str(cid)) for cid in chunk_ids]
        rows = await db.fetch("SELECT id FROM context_chunks WHERE id = ANY($1::uuid[])", keys)
    except Exception as exc:
        record_failure("tool_views.verify_persisted_chunk_ids", exc)
        return set()
    return acknowledged_chunk_ids(rows)


async def reconcile_pending_flush(
    result: Any, pending_chunks: list[dict[str, Any]], *, db: Any
) -> bool:
    """Apply a batch acknowledgement to the pending queue (contract C1).

    Every producer-assigned ``id`` is a stable event identity: retries resubmit
    the same ids, and only records the store (or a by-id SELECT) confirms leave
    the queue. Legacy batches without ids keep the historical clear-on-success
    behavior. Returns True only when the queue is empty.
    """
    identified = [record for record in pending_chunks if record.get("id") is not None]
    if not identified:
        pending_chunks.clear()
        return True
    confirmed = acknowledged_chunk_ids(result)
    missing = [record for record in identified if str(record.get("id")) not in confirmed]
    if missing and db is not None:
        confirmed |= await _verify_persisted_chunk_ids(db, [record.get("id") for record in missing])
    remaining = [record for record in identified if str(record.get("id")) not in confirmed]
    pending_chunks[:] = remaining
    return not remaining


async def flush_pending_chunks(
    pending_chunks: list[dict[str, Any]], *, store: Any, db: Any
) -> bool:
    """Insert all pending chunks in one batch, keeping the fail-open policy.

    R4-025/PB-1: a failing context store must never fail the turn. Returns True
    only when every pending chunk was durably written (the queue is then
    cleared, so a later flush cannot re-insert it). A failed write retains the
    chunks for a later retry — never silently dropped (code_review #6) — and
    cancellation still propagates to the caller. Contract C1 reconciliation
    (stable ids, ambiguous acknowledgements verified by id) lives in
    :func:`reconcile_pending_flush`.
    """
    if not pending_chunks:
        return True
    try:
        result = store.add_chunks_batch(pending_chunks)
        if inspect.isawaitable(result):
            result = await result
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("Context flush failed (%s); continuing turn", type(exc).__name__)
        return False
    return await reconcile_pending_flush(result, pending_chunks, db=db)


async def mark_turn_accepted(session_id: uuid.UUID, agent_id: str, usage_store: Any) -> None:
    """Best-effort accepted-completion attribution (never breaks the turn)."""
    try:
        await usage_store.mark_accepted(session_id, agent_id)
    except Exception as error:
        record_failure("tool_views.mark_turn_accepted", error)
