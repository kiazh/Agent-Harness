"""Scheduler usage tracking: reserve/finish around job execution."""
from __future__ import annotations

import os
import uuid
from unittest.mock import AsyncMock

import pytest

from ah.core.models import AgentResponse

TEST_DSN = os.environ.get("AGENT_HARNESS_TEST_DATABASE_URL", "")
needs_db = pytest.mark.skipif(not TEST_DSN, reason="AGENT_HARNESS_TEST_DATABASE_URL not set")


@pytest.fixture
async def connected_db():
    from ah.db.connection import db

    await db.connect()
    yield
    await db.close()


class RecordingAgent:
    """Fake agent that records which sessions and prompts it was run with."""

    runs: list[tuple[uuid.UUID, str]] = []

    def __init__(self, agent_name: str) -> None:
        self.agent_name = agent_name

    async def run(self, session_id, user_message, verbose=True) -> AgentResponse:
        RecordingAgent.runs.append((session_id, user_message))
        return AgentResponse(content="done", tool_calls=[], tokens_used=5, iterations=1)


@needs_db
@pytest.mark.usefixtures("connected_db")
@pytest.mark.asyncio
async def test_scheduler_execute_calls_usage_store(monkeypatch):
    """JobRunner._execute() should call usage_store.reserve() and usage_store.finish() around the agent call."""
    from ah.core.scheduler import JobRunner, job_store

    RecordingAgent.runs = []
    sid = await _session()
    job = await job_store.create(
        name="interval",
        kind="interval",
        session_id=sid,
        prompt="do the thing",
        interval_seconds=30,
    )
    from ah.db.connection import db

    await db.execute("UPDATE jobs SET next_run_at = now() WHERE id = $1", job.id)

    reserve = AsyncMock(return_value=uuid.uuid4())
    finish = AsyncMock()
    monkeypatch.setattr("ah.core.scheduler.usage_store.reserve", reserve)
    monkeypatch.setattr("ah.core.scheduler.usage_store.finish", finish)

    runner = JobRunner(agent_factory=RecordingAgent)
    await runner._execute(job)

    reserve.assert_awaited_once()
    finish.assert_awaited_once()


@needs_db
@pytest.mark.usefixtures("connected_db")
@pytest.mark.asyncio
async def test_scheduler_execute_calls_finish_with_failed_on_error(monkeypatch):
    """JobRunner._execute() should call usage_store.finish(failed=True) when agent raises."""
    from ah.core.scheduler import JobRunner, job_store

    sid = await _session()
    job = await job_store.create(
        name="boom", kind="interval", session_id=sid, prompt="x", interval_seconds=30
    )
    from ah.db.connection import db

    await db.execute("UPDATE jobs SET next_run_at = now() WHERE id = $1", job.id)

    class Exploding:
        def __init__(self, name):
            pass

        async def run(self, *a, **k):
            raise RuntimeError("kaboom")

    reserve = AsyncMock(return_value=uuid.uuid4())
    finish = AsyncMock()
    monkeypatch.setattr("ah.core.scheduler.usage_store.reserve", reserve)
    monkeypatch.setattr("ah.core.scheduler.usage_store.finish", finish)

    runner = JobRunner(agent_factory=Exploding)
    with pytest.raises(RuntimeError, match="kaboom"):
        await runner._execute(job)

    reserve.assert_awaited_once()
    finish.assert_awaited_once()
    assert finish.call_args.kwargs.get("failed") is True


async def _session() -> uuid.UUID:
    from ah.core.session import session_manager

    return (await session_manager.create(title="scheduler test")).id
