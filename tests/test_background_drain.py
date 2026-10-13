"""Bounded drain for fire-and-forget agent background tasks.

Regression for the shutdown race diagnosed 2026-10-13: the agent schedules
consolidation/learning-review tasks, and every pool-closing path (CLI chat,
gateway close, runtime shutdown) raced them with ``db.close()`` — tasks died
with asyncpg InterfaceError inside the close window and CLI-session memories
silently never consolidated.
"""

import asyncio
from types import SimpleNamespace

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


async def test_drain_cancellation_still_settles_tasks():
    """A cancelled shutdown must not close the pool under a live writer."""
    slow = asyncio.get_running_loop().create_task(asyncio.sleep(30))
    background_tasks.consolidation_inflight["session-1"] = slow

    drain = asyncio.get_running_loop().create_task(
        background_tasks.drain_background_tasks(timeout=30)
    )
    await asyncio.sleep(0.05)
    drain.cancel()
    with pytest.raises(asyncio.CancelledError):
        await drain

    await asyncio.sleep(0.05)
    assert slow.cancelled(), "cancelled drain still cancels and settles pending tasks"


async def test_drain_second_pass_picks_up_late_tasks():
    async def schedule_late():
        late = asyncio.get_running_loop().create_task(asyncio.sleep(0.05, result="late"))
        background_tasks.pending_learning_reviews.add(late)

    first = asyncio.get_running_loop().create_task(schedule_late())
    background_tasks.pending_learning_reviews.add(first)

    drained = await background_tasks.drain_background_tasks(timeout=1.0)

    assert drained == 2, "tasks scheduled during the drain are caught by a second pass"


async def test_join_agent_background_tasks_bounded():
    """Turn-close paths join the agent's tasks before the provider closes."""
    agent = SimpleNamespace(_consolidation_tasks=set(), _learning_tasks=set())
    quick = asyncio.get_running_loop().create_task(asyncio.sleep(0.02))
    stall = asyncio.get_running_loop().create_task(asyncio.sleep(30))
    agent._consolidation_tasks.add(quick)
    agent._learning_tasks.add(stall)

    joined = await background_tasks.join_agent_background_tasks(agent, timeout=0.05)

    assert joined == 2
    await asyncio.sleep(0.05)
    assert quick.done() and not quick.cancelled()
    assert stall.cancelled()
