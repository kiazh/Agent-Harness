"""Shared batch-tail helpers for the ReAct loops (A5-min extraction).

``BaseReActAgent.run`` and ``.run_stream`` used to carry near-identical
post-batch tails: pause collection from ``tool_calls_made[batch_start:]``,
the S3.1(d) stagnation hook, and the failed-stop response shape. FM8 fixed
the state-write honesty on BOTH copies at once; this module removes the
copy so the two loops cannot drift again.

Interface shape (documented choice, behavior-preserving):

- ``collect_pauses`` is the exact pause-finder the loops inlined.
- ``stagnation_step`` owns the guarded controller-wiring access, the
  progress fold and the truthful stop text. The session-state write is
  performed here through the ``session_manager`` the caller passes (the
  agent module global, so tests that patch it keep observing the write),
  with the caller's ``flush`` awaited first so the original
  write-after-flush ordering is preserved. ``audit`` is injected so the
  event keeps using the agent's own ``audit_log`` object.
- ``stopped_response`` builds the single failed-stop ``AgentResponse``.

Import discipline: stdlib plus ``AgentResponse`` (a core-layer model) at
module level; the controller dependency is imported lazily inside
``stagnation_step`` under the same guarded try/except the agent loops
used — any failure skips the hook silently and the turn proceeds.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from machina.core.models import AgentResponse

logger = logging.getLogger(__name__)


def collect_pauses(batch: list[dict]) -> list[dict]:
    """Typed pause records from one tool batch (exact previous semantics).

    An entry contributes its approval-list field when truthy, else its
    single approval record; only dict pauses carrying a ``request_id``
    count.
    """
    return [
        pause
        for entry in batch
        for pause in (entry.get("needs_approvals") or [entry.get("needs_approval")])
        if isinstance(pause, dict) and pause.get("request_id")
    ]


async def stagnation_step(
    history: Any,
    batch: Any,
    tool_calls_made: Any,
    *,
    session_id: Any = None,
    session_manager: Any = None,
    audit: Callable[..., Any] | None = None,
    flush: Callable[[], Any] | None = None,
) -> tuple[Any, str | None]:
    """Fold the batch into progress history; on a stop, log and persist it.

    Returns ``(history, stop_text)``; ``stop_text`` is None unless the fold
    reports K consecutive stagnant rounds. On a stop the original order is
    kept: warning -> audit -> caller flush -> truthful session-state write
    via the passed ``session_manager`` (the wiring payload builder). A
    wiring-import or progress-fold failure returns ``(history, None)``: the
    turn proceeds, exactly like the guarded block this replaced.
    """
    try:
        from machina.controller import wiring as _wiring

        history, stop = _wiring.check_stagnation(history, batch)
        if stop is None:
            return history, None
        text = _wiring.stagnation_content(stop, len(tool_calls_made), tool_calls_made)
    except Exception:
        return history, None
    logger.warning("s3 stagnation stop: %s", stop)
    if audit is not None:
        audit("agent_stagnation_stop", session_id=str(session_id), reason=stop)
    if flush is not None:
        await flush()
    if session_manager is not None:
        await _wiring.record_stagnation_state(
            session_manager, session_id, stop, text, tool_calls_made
        )
    return history, text


def stopped_response(
    stop_text: str,
    tool_calls: list[dict[str, Any]],
    tokens: int,
    iterations: int,
) -> AgentResponse:
    """The shared failed-stop response shape for a stagnation halt."""
    return AgentResponse(
        content=stop_text,
        outcome="failed",
        tool_calls=tool_calls,
        tokens_used=tokens,
        iterations=iterations,
    )
