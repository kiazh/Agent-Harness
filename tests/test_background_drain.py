"""Bounded drain for fire-and-forget agent background tasks.

Regression for the shutdown race diagnosed 2026-10-13: the agent schedules
consolidation/learning-review tasks, and every pool-closing path (CLI chat,
gateway close, runtime shutdown) raced them with ``db.close()`` — tasks died
with asyncpg InterfaceError inside the close window and CLI-session memories
silently never consolidated.
"""

import asyncio

import pytest

from machina.core import background_tasks


@pytest.fixture(autouse=True)
def isolated_registries(monkeypatch):
    """Fresh containers so real schedules/other tests never leak in."""
    monkeypatch.setattr(background_tasks, "consolidation_inflight", {})
    monkeypatch.setattr(background_tasks, "pending_learning_reviews", set())


async def test_drain_awaits_pending_tasks():
    task = asyncio.get_running_loop().create_task(asyncio.sleep(0.02, result="done"))
    background_tasks.pending_learning_reviews.add(task)

    drained = await background_tasks.drain_background_tasks(timeout=1.0)

    assert drained == 1
    assert task.done()
    assert task.result() == "done"
    assert not task.cancelled()


async def test_drain_covers_consolidation_registry():
    task = asyncio.get_running_loop().create_task(asyncio.sleep(0.01))
    background_tasks.consolidation_inflight["session-1"] = task

    assert await background_tasks.drain_background_tasks(timeout=1.0) == 1
    assert task.done() and not task.cancelled()


async def test_drain_cancels_tasks_past_the_deadline():
    task = asyncio.get_running_loop().create_task(asyncio.sleep(30))
    background_tasks.consolidation_inflight["session-1"] = task

    drained = await background_tasks.drain_background_tasks(timeout=0.05)

    assert drained == 1
    await asyncio.sleep(0)  # let the cancellation land
    assert task.cancelled()


async def test_drain_skips_done_tasks_and_empty_registries():
    done = asyncio.get_running_loop().create_task(asyncio.sleep(0))
    await done
    background_tasks.pending_learning_reviews.add(done)

    assert await background_tasks.drain_background_tasks(timeout=1.0) == 0
    assert await background_tasks.drain_background_tasks(timeout=1.0) == 0
