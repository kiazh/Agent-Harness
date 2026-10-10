"""Cancellation-resistant cleanup must not retain the caller indefinitely."""

import asyncio
from types import SimpleNamespace

import pytest

from ah.core.agent_factory import close_agent_provider
from ah.core.runtime import _bounded_join


async def _resist_cancellation(started, release):
    started.set()
    while not release.is_set():
        try:
            await release.wait()
        except asyncio.CancelledError:
            continue


async def test_bounded_join_returns_resistant_tasks_at_deadline():
    started, release = asyncio.Event(), asyncio.Event()
    worker = asyncio.create_task(_resist_cancellation(started, release))
    await started.wait()
    join = asyncio.create_task(_bounded_join([worker], timeout=0.01, label="test"))
    try:
        done, _ = await asyncio.wait({join}, timeout=0.2)
        assert join in done, "cleanup waited for cancellation-resistant work past its deadline"
        assert await join == [worker]
        assert not worker.done()
    finally:
        release.set()
        await asyncio.gather(worker, join, return_exceptions=True)


async def test_bounded_join_propagates_caller_cancellation():
    started, release = asyncio.Event(), asyncio.Event()
    worker = asyncio.create_task(_resist_cancellation(started, release))
    await started.wait()
    join = asyncio.create_task(_bounded_join([worker], timeout=60, label="test"))
    await asyncio.sleep(0)
    join.cancel()
    try:
        done, _ = await asyncio.wait({join}, timeout=0.2)
        assert join in done, "caller cancellation remained blocked by resistant work"
        with pytest.raises(asyncio.CancelledError):
            await join
    finally:
        release.set()
        await asyncio.gather(worker, join, return_exceptions=True)


async def test_owned_provider_close_returns_when_close_resists_cancellation():
    started, release = asyncio.Event(), asyncio.Event()

    class Provider:
        async def close(self):
            await _resist_cancellation(started, release)

    agent = SimpleNamespace(_owns_provider=True, provider=Provider())
    close = asyncio.create_task(close_agent_provider(agent, timeout=0.01))
    await started.wait()
    try:
        done, _ = await asyncio.wait({close}, timeout=0.2)
        assert close in done, "provider cleanup exceeded its deadline"
        await close
    finally:
        release.set()
        await asyncio.gather(close, return_exceptions=True)
