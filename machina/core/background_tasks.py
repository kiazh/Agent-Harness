"""Registries for fire-and-forget agent tasks + bounded shutdown joins.

Memory consolidation and learning-review tasks are asyncio tasks the agent
schedules after a turn. Every shutdown or turn-close path that closes the DB
pool or the provider must let them finish first: without a join the task
races ``db.close()`` (asyncpg InterfaceError) or dies on a closed provider
client — CLI-session memories then never consolidate, and the same failure
class would hit gateway/REST/scheduler/delegated close paths.

The containers live here rather than in ``core.agent`` so shutdown paths can
drain without importing the agent stack. ``core.agent`` imports them back
(same live objects), so ``agent._consolidation_inflight`` keeps resolving.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


# Strong references so scheduled tasks are never garbage-collected mid-flight.
pending_learning_reviews: set[asyncio.Task] = set()
# Shared per-session consolidation single-flight (LP-14): concurrent agent
# instances working one session never re-extract the same range.
consolidation_inflight: dict[str, asyncio.Task] = {}


def _live_tasks() -> list[asyncio.Task]:
    return [
        task
        for group in (consolidation_inflight.values(), tuple(pending_learning_reviews))
        for task in group
        if isinstance(task, asyncio.Task) and not task.done()
    ]


async def _cancel_and_settle(tasks: list[asyncio.Task], grace: float = 1.0) -> None:
    """Cancel leftovers and wait (bounded) until they stop touching the DB."""
    for task in tasks:
        task.cancel()
    if not tasks:
        return
    try:
        await asyncio.wait(tasks, timeout=grace)
    except asyncio.CancelledError:
        # The caller itself is being cancelled; cleanup stays best-effort and
        # the cancellation still propagates afterwards.
        pass


async def drain_background_tasks(timeout: float = 5.0) -> int:
    """Await pending consolidation/learning tasks; bounded, never leaves strays.

    Cancellation-safe: if the caller is cancelled mid-wait, pending tasks are
    still cancelled and given a bounded settle window before the exception
    propagates, so a cancelled shutdown never closes the pool under a live
    writer. A second bounded pass catches tasks scheduled while draining.
    Returns how many tasks were awaited.
    """
    scheduled = _live_tasks()
    if not scheduled:
        return 0
    late: list[asyncio.Task] = []
    try:
        await asyncio.wait(scheduled, timeout=timeout)
        late = [task for task in _live_tasks() if task not in scheduled]
        if late:
            await asyncio.wait(late, timeout=min(1.0, max(0.0, timeout)))
    finally:
        await _cancel_and_settle([task for task in _live_tasks() if not task.done()])
    unsettled = [task for task in (*scheduled, *late) if not task.done()]
    if unsettled:
        logger.warning("background drain timed out; cancelled %d task(s)", len(unsettled))
    return len(scheduled) + len(late)


async def join_agent_background_tasks(
    agent: object, timeout: float = 2.0, settle_grace: float = 1.0
) -> int:
    """Bounded join of one agent's consolidation/learning tasks before close.

    Turn-close paths (gateway, API streaming, scheduler jobs, delegated
    children) close the session provider at turn end; consolidation shares
    that provider, so without this join its extraction calls a closed client
    and the session silently never consolidates. Bounded and never raises
    beyond cancellation: on cancellation the leftovers are cancelled and given
    ``settle_grace`` seconds to stop touching the DB before the exception
    propagates. Turn-close callers keep ``timeout + settle_grace`` under the
    turn-completion deadline even for cancellation-resistant tasks.
    """
    tasks = [
        task
        for group in (
            getattr(agent, "_consolidation_tasks", ()),
            getattr(agent, "_learning_tasks", ()),
        )
        for task in group
        if isinstance(task, asyncio.Task) and not task.done()
    ]
    if not tasks:
        return 0
    try:
        await asyncio.wait(tasks, timeout=timeout)
    finally:
        await _cancel_and_settle(
            [task for task in tasks if not task.done()], grace=settle_grace
        )
    return len(tasks)
