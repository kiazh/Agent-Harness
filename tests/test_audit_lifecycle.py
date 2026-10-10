"""Audit writer generations keep their own queues and shutdown ownership."""

import asyncio

import pytest

from ah.observability.audit import AuditPersistence


async def _cancel_tasks(*tasks):
    for task in tasks:
        if task is not None and not task.done():
            task.cancel()
    await asyncio.gather(*(task for task in tasks if task is not None), return_exceptions=True)


@pytest.mark.asyncio
async def test_finished_writer_callback_does_not_drop_restarted_events(monkeypatch):
    from ah.observability import audit

    persisted = []
    first_written = asyncio.Event()

    async def execute(_sql, event, _payload):
        persisted.append(event)
        first_written.set()

    monkeypatch.setattr(audit.db, "execute", execute)
    writer = AuditPersistence()
    writer.start()
    old_task = writer._task
    old_task.cancel()
    # Resume after cancellation, before the old task's scheduled done callback.
    await asyncio.sleep(0)
    writer.start()
    new_task = writer._task
    writer.submit({"event": "restarted-first"})
    try:
        await asyncio.wait_for(first_written.wait(), 1)
        writer.submit({"event": "restarted-second"})
        await writer.stop()
        await writer.stop()
        assert persisted == ["restarted-first", "restarted-second"]
    finally:
        await _cancel_tasks(old_task, new_task)


@pytest.mark.asyncio
async def test_timed_out_writer_finishes_cancellation_on_its_original_queue(monkeypatch):
    from ah.observability import audit

    started = asyncio.Event()
    cancelling = asyncio.Event()
    release = asyncio.Event()

    async def execute(*_args):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelling.set()
            await release.wait()
            raise

    monkeypatch.setattr(audit.db, "execute", execute)
    writer = AuditPersistence()
    writer.start()
    old_task = writer._task
    writer.submit({"event": "slow"})
    await asyncio.wait_for(started.wait(), 1)
    try:
        await asyncio.wait_for(writer.stop(timeout=0), 1)
        await asyncio.wait_for(cancelling.wait(), 1)
        release.set()
        result = (await asyncio.gather(old_task, return_exceptions=True))[0]
        assert isinstance(result, asyncio.CancelledError)
    finally:
        release.set()
        await _cancel_tasks(old_task)


@pytest.mark.asyncio
async def test_start_during_shutdown_persists_events_in_a_new_writer(monkeypatch):
    from ah.observability import audit

    started = asyncio.Event()
    release = asyncio.Event()
    persisted = []

    async def execute(_sql, event, _payload):
        if event == "first":
            started.set()
            await release.wait()
        persisted.append(event)

    monkeypatch.setattr(audit.db, "execute", execute)
    writer = AuditPersistence()
    writer.start()
    old_task = writer._task
    writer.submit({"event": "first"})
    await asyncio.wait_for(started.wait(), 1)
    stopping = asyncio.create_task(writer.stop())
    # Let stop enqueue the sentinel and suspend while the first write is blocked.
    await asyncio.sleep(0)
    writer.start()
    new_task = writer._task
    writer.submit({"event": "second"})
    release.set()
    try:
        await asyncio.wait_for(stopping, 1)
        await writer.stop()
        assert sorted(persisted) == ["first", "second"]
    finally:
        release.set()
        await _cancel_tasks(stopping, old_task, new_task)


@pytest.mark.asyncio
async def test_cancelled_shutdown_cancels_the_owned_writer(monkeypatch):
    from ah.observability import audit

    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def execute(*_args):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(audit.db, "execute", execute)
    writer = AuditPersistence()
    writer.start()
    old_task = writer._task
    writer.submit({"event": "slow"})
    await asyncio.wait_for(started.wait(), 1)
    stopping = asyncio.create_task(writer.stop())
    await asyncio.sleep(0)
    stopping.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await stopping
        await asyncio.wait_for(cancelled.wait(), 0.5)
    finally:
        await _cancel_tasks(stopping, old_task)
