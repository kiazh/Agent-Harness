"""Owned provider retirement is bounded, observable, and cancellation-safe."""

import asyncio
from types import SimpleNamespace

import pytest

from ah.core.agent_factory import close_agent_provider
from ah.core.metrics import MetricsCollector


@pytest.fixture
def collector(monkeypatch):
    value = MetricsCollector()
    monkeypatch.setattr("ah.core.metrics.metrics", value)
    return value


def agent(close, *, owns=True):
    return SimpleNamespace(
        _owns_provider=owns, agent_id="test-agent", provider=SimpleNamespace(close=close)
    )


async def test_close_success_and_injected_provider(collector):
    calls = []

    async def close():
        calls.append(True)

    await close_agent_provider(agent(close, owns=False))
    assert not calls
    await close_agent_provider(agent(close))
    assert calls == [True]
    assert collector._counters["provider.close.success"] == 1


@pytest.mark.parametrize("synchronous", [False, True])
async def test_close_failure_visible_without_secret_or_primary_error(
    collector, caplog, synchronous
):
    def fail():
        raise RuntimeError("sentinel-secret provider response content")

    async def async_fail():
        fail()

    await close_agent_provider(agent(fail if synchronous else async_fail))
    assert collector._counters["provider.close.failed"] == 1
    assert "RuntimeError" in caplog.text
    assert "sentinel-secret" not in caplog.text
    assert "provider response content" not in caplog.text


async def test_close_timeout_quarantines_resistant_task_and_records_late_error(collector, caplog):
    started, released = asyncio.Event(), asyncio.Event()

    async def close():
        started.set()
        try:
            await released.wait()
        except asyncio.CancelledError:
            await released.wait()
        raise ValueError("sentinel-secret")

    await asyncio.wait_for(close_agent_provider(agent(close), timeout=0.01), timeout=0.5)
    assert started.is_set()
    assert collector._counters["provider.close.timeout"] == 1
    from ah.core.cleanup import _cleanup_tasks as _provider_cleanup_tasks

    pending = set(_provider_cleanup_tasks)
    assert pending and all(not task.done() for task in pending)
    released.set()
    await asyncio.gather(*pending, return_exceptions=True)
    await asyncio.sleep(0)
    assert not _provider_cleanup_tasks
    assert collector._counters["provider.close.late_failed"] == 1
    assert "sentinel-secret" not in caplog.text


async def test_parent_cancellation_propagates_and_cancels_close(collector):
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def close():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    task = asyncio.create_task(close_agent_provider(agent(close)))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(cancelled.wait(), 0.5)
    assert collector._counters["provider.close.cancelled"] == 1
