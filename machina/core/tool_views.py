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
    "REDUCER_EXEMPT_TOOLS",
    "mark_turn_accepted",
]

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


async def mark_turn_accepted(session_id: uuid.UUID, agent_id: str, usage_store: Any) -> None:
    """Best-effort accepted-completion attribution (never breaks the turn)."""
    try:
        await usage_store.mark_accepted(session_id, agent_id)
    except Exception as error:
        record_failure("tool_views.mark_turn_accepted", error)
