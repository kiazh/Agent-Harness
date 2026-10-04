"""Cron due times use the same clock as the database claim query."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from ah.core.scheduler import JobStore


async def test_finish_uses_database_clock_for_cron(monkeypatch):
    from ah.core import scheduler

    clock = datetime(2035, 1, 1, 12, 0, tzinfo=UTC)
    db = SimpleNamespace(fetchval=AsyncMock(return_value=clock), execute=AsyncMock())
    monkeypatch.setattr(scheduler, "db", db)
    store = JobStore()
    store.get = AsyncMock(return_value=SimpleNamespace(kind="cron", cron_expression="* * * * *"))
    job_id = uuid.uuid4()
    await store.finish(job_id)
    db.fetchval.assert_awaited_with("SELECT now()")
    assert db.execute.await_args.args[4] == clock + timedelta(minutes=1)


async def test_create_uses_database_clock_for_interval(monkeypatch):
    from ah.core import scheduler

    clock = datetime(2035, 1, 1, 12, 0, tzinfo=UTC)
    db = SimpleNamespace(
        fetchval=AsyncMock(return_value=clock), fetchrow=AsyncMock(return_value={})
    )
    monkeypatch.setattr(scheduler, "db", db)
    monkeypatch.setattr(scheduler, "_row_to_job", lambda row: row)
    await JobStore().create(
        name="check",
        kind="interval",
        session_id=uuid.uuid4(),
        prompt="check",
        interval_seconds=60,
    )
    db.fetchval.assert_awaited_with("SELECT now()")
    assert db.fetchrow.await_args.args[7] == clock + timedelta(seconds=60)
