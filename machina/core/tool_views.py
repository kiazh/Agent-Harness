"""Bounded model-visible views for oversized tool results (T2/T3 seams).

Extracted from the agent loop (A5: ``agent.py`` must not grow past its
ratified ceiling). The agent delegates here at the tool-append seam; every
failure path is fail-open toward the legacy blind cap.

**Store-before-lossy-publish.** A reducer view drops content; it is only
published when the exact original was durably retained first (artifact handle
in the disclosure). Without a stored original — store disabled, unavailable,
quota-refused, oversize — the caller keeps the legacy blind append cap, the
same behavior as before this feature existed. This mirrors the plan's
recovery invariant: never publish a lossy result whose recovery object failed
to persist, and never label a view recoverable without a working handle.
"""

from __future__ import annotations

import uuid
from typing import Any

from machina.core.artifacts import artifact_disclosure, artifact_store
from machina.core.config import config
from machina.core.metrics import metrics
from machina.core.prompt import tool_message_limit
from machina.core.reducers import reduce_tool_result
from machina.observability.diagnostics import record_failure

__all__ = ["bounded_tool_view", "REDUCER_EXEMPT_TOOLS", "mark_turn_accepted"]

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


async def bounded_tool_view(
    tool_name: str,
    result_str: str,
    *,
    session_id: uuid.UUID,
    agent_id: str,
    status: str | None = None,
) -> tuple[str, str | None]:
    """Return ``(model_visible_view, artifact_id_or_None)`` for one result.

    Small results pass through verbatim. Oversized results are retained as
    artifacts (durable originals) and rendered through the deterministic
    format reducers with their omission/recovery disclosure; when the store is
    unavailable or reduction fails, the legacy blind append cap is kept and
    no recovery is claimed.
    """
    try:
        limit = _append_limit(tool_name)
        if len(result_str) <= limit:
            return result_str, None
        if tool_name in REDUCER_EXEMPT_TOOLS:
            # Already bounded/paging-addressed: never a reducer view, but the
            # append cap still applies so an oversized page cannot slip
            # through verbatim. The page header leads the result, so
            # continuation offsets remain resolvable after the bound.
            return result_str[:limit], None
        reducers_enabled, artifacts_enabled = _flags()
        artifact_id: str | None = None
        if artifacts_enabled:
            stored = await artifact_store.store(
                f"tool:{tool_name}",
                result_str,
                session_id=session_id,
                agent_id=agent_id,
                status=status,
            )
            if stored is not None:
                artifact_id = stored["artifact_id"]
        if artifact_id is None:
            # No durable original: publish nothing lossy. The legacy cap is
            # the pre-feature behavior and makes no recovery claim.
            metrics.increment_counter("reducers.fallback_cap")
            return result_str[:limit], None
        if reducers_enabled:
            reduction = reduce_tool_result(result_str, budget_chars=limit, artifact_id=artifact_id)
            if reduction.changed:
                metrics.increment_counter(f"reducers.{reduction.method}")
                return reduction.output, artifact_id
        metrics.increment_counter("reducers.plain_cap")
        disclosure = artifact_disclosure(artifact_id, len(result_str))
        return result_str[:limit] + disclosure, artifact_id
    except Exception as error:
        # Fail-open: bounded blind cap, no artifact claim. When a handle was
        # already stored, it is only surfaced if the disclosure can be built;
        # a broken path never fabricates recovery.
        record_failure("tool_views.bounded_tool_view", error)
        return result_str[: tool_message_limit(tool_name)], None


async def mark_turn_accepted(session_id: uuid.UUID, agent_id: str, usage_store: Any) -> None:
    """Best-effort accepted-completion attribution (never breaks the turn)."""
    try:
        await usage_store.mark_accepted(session_id, agent_id)
    except Exception as error:
        record_failure("tool_views.mark_turn_accepted", error)
