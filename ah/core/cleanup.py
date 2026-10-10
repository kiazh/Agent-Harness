"""Bounded cleanup observation with strong retirement tracking and safe logs."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from ah.core.metrics import metrics

logger = logging.getLogger(__name__)
_cleanup_tasks: set[asyncio.Future] = set()


async def bounded_cleanup(
    cleanup: Awaitable[Any],
    timeout: float,
    label: str,
    *,
    report: Callable[[str, BaseException | None], None] | None = None,
) -> bool:
    """Return whether cleanup retired; propagate errors and caller cancellation.

    A deadline requests cancellation without waiting for acknowledgement.
    Leftover tasks stay strongly referenced and their eventual failures are
    retrieved and reported. Labels identify services, never action payloads.
    """
    task = asyncio.ensure_future(cleanup)
    _cleanup_tasks.add(task)
    quarantined = False

    def outcome(name: str, error: BaseException | None = None) -> None:
        metrics.increment_counter(f"cleanup.{name}")
        if name != "success":
            logger.warning(
                "cleanup %s service=%s error_type=%s",
                name,
                label,
                type(error).__name__ if error else "none",
            )
        if report is not None:
            report(name, error)

    def retired(done: asyncio.Future) -> None:
        _cleanup_tasks.discard(done)
        if not done.cancelled():
            error = done.exception()
            if quarantined and error is not None:
                outcome("late_failed", error)

    task.add_done_callback(retired)
    try:
        done, _ = await asyncio.wait({task}, timeout=timeout)
        if task not in done:
            quarantined = True
            task.cancel()
            outcome("timeout")
            return False
        task.result()
    except asyncio.CancelledError:
        quarantined = True
        task.cancel()
        outcome("cancelled")
        raise
    except Exception as error:
        outcome("failed", error)
        raise
    outcome("success")
    return True


async def cancel_and_join(
    tasks: list[asyncio.Task], timeout: float, label: str
) -> list[asyncio.Task]:
    """Request group cancellation and return tasks still alive at the deadline."""
    if not tasks:
        return []
    for task in tasks:
        _cleanup_tasks.add(task)

        def retired(done: asyncio.Task) -> None:
            _cleanup_tasks.discard(done)
            if not done.cancelled():
                error = done.exception()
                if error is not None:
                    metrics.increment_counter("cleanup.failed")
                    logger.warning(
                        "cleanup failed service=%s error_type=%s", label, type(error).__name__
                    )

        task.add_done_callback(retired)
        if not task.done():
            task.cancel()
    await asyncio.wait(tasks, timeout=timeout)
    return [task for task in tasks if not task.done()]
