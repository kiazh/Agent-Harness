"""Registries for fire-and-forget agent tasks + a bounded shutdown drain.

Memory consolidation and learning-review tasks are asyncio tasks the agent
schedules after a turn. Every shutdown path that closes the DB pool (CLI
one-shot chat, gateway close, API lifespan) must let them finish first:
without a drain the task races ``db.close()`` and dies with asyncpg
``InterfaceError`` — CLI-session memories then never consolidate.

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


async def drain_background_tasks(timeout: float = 5.0) -> int:
    """Await pending consolidation/learning tasks; bounded, never raises.

    Returns how many tasks were awaited. Tasks still pending at the deadline
    are cancelled — consolidation is watermark-based and retries safely, so a
    cancelled task is quarantined input, not lost work.
    """
    tasks = [
        task
        for group in (consolidation_inflight.values(), tuple(pending_learning_reviews))
        for task in group
        if isinstance(task, asyncio.Task) and not task.done()
    ]
    if not tasks:
        return 0
    _, pending = await asyncio.wait(tasks, timeout=timeout)
    for task in pending:
        task.cancel()
    if pending:
        logger.warning("background drain timed out; cancelled %d task(s)", len(pending))
    return len(tasks)
