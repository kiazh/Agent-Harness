"""Audit writer shutdown behavior under a stalled database."""

import asyncio

import pytest

from ah.observability.audit import AuditPersistence


@pytest.mark.asyncio
async def test_stop_finishes_when_queue_is_full_and_database_is_stalled(monkeypatch):
    from ah.observability import audit

    started = asyncio.Event()
    release = asyncio.Event()

    async def stalled_execute(*_args):
        started.set()
        await release.wait()

    monkeypatch.setattr(audit.db, "execute", stalled_execute)
    writer = AuditPersistence(max_pending=1)
    writer.start()
    writer.submit({"event": "first"})
    await started.wait()
    writer.submit({"event": "second"})

    try:
        await asyncio.wait_for(writer.stop(timeout=0.02), timeout=0.3)
        assert writer._task is None
    finally:
        release.set()
        if writer._task is not None:
            writer._task.cancel()
            await asyncio.gather(writer._task, return_exceptions=True)
