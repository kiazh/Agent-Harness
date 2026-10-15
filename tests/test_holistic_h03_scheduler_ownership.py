"""H03 (holistic review 2026-10-15): an overridden scheduled job must execute.

R5-C (49e592c) made the JOB's agent identity govern the run via an in-memory
session view. ``ReActAgent.run`` reloads the durable session and enforces
``session.agent_id == running agent``, so a job pinned to a specialist on a
differently owned session raised ``PermissionError`` before executing — the
tick recorded a job error despite the override.

The fix reconciles the durable session owner with the executed identity
before the run (the documented R5-C execution model: the job's identity
governs), which is the smallest change that preserves job context, the
approval pause/resume session binding, and every existing scheduler pin.
This E2E drives a real scheduler tick, real session/job rows, the real agent
loop and ownership check; only the LLM provider boundary is stubbed.
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio

from machina.core.models import LLMResponse

pytestmark = pytest.mark.db


@pytest_asyncio.fixture
async def scheduler_db():
    """Wire the shared ``db`` singleton to a PRIVATE test schema.

    Pattern from ``tests/test_r4_105_caller_scope_real_run.py``: a private
    schema's DDL never locks the public relations other workspace processes
    are writing to.
    """
    dsn = os.environ.get("MACHINA_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("MACHINA_TEST_DATABASE_URL not set")
    from machina.db.connection import Database, db
    from tests.support.schema import isolated_schema

    administrator = Database(dsn=dsn)
    await administrator.connect()
    try:
        async with isolated_schema(administrator) as (_schema, database):
            original_pool, original_dsn = db._pool, db.dsn
            db._pool = database._pool
            db.dsn = database.dsn
            try:
                yield database
            finally:
                db._pool = original_pool
                db.dsn = original_dsn
    finally:
        await administrator.close()


def _quiet_agent_config(monkeypatch) -> None:
    """Turn off retrieval/side-effect subsystems this proof does not exercise."""
    from machina.core.config import config

    for field in (
        "memory_enabled",
        "personality_growth_enabled",
        "rag_enabled",
        "memory_consolidation_enabled",
    ):
        monkeypatch.setattr(config, field, False)


class _FinalProvider:
    """Provider boundary stub: one completion with final text and no tools."""

    model = "h03-stub-model"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, **kwargs) -> LLMResponse:
        self.calls += 1
        return LLMResponse(content="job done", model=self.model, usage={"total_tokens": 3})


@pytest.mark.asyncio
async def test_e2e_overridden_job_runs_and_reconciles_session_owner(scheduler_db, monkeypatch):
    from machina.core.scheduler import JobRunner, job_store
    from machina.core.session import SessionManager

    _quiet_agent_config(monkeypatch)
    provider = _FinalProvider()
    monkeypatch.setattr("machina.core.provider.get_provider", lambda **kwargs: provider)

    session = await SessionManager().create(title="h03 override", agent_id="harness")
    job = await job_store.create(
        name="h03 researcher job",
        kind="interval",
        session_id=session.id,
        prompt="survey the repo",
        interval_seconds=10,
        agent_name="researcher",
    )
    await scheduler_db.execute(
        "UPDATE jobs SET next_run_at = now() - interval '1 second' WHERE id = $1", job.id
    )

    assert await JobRunner().run_due_once() is True

    finished = await job_store.get(job.id)
    assert finished is not None
    assert finished.run_count == 1
    assert finished.status == "idle" and finished.last_error is None, finished.last_error
    assert provider.calls == 1, "the selected specialist must actually execute"

    owner = await scheduler_db.fetchval("SELECT agent_id FROM sessions WHERE id = $1", session.id)
    assert owner == "researcher", "the durable owner must be reconciled with the executed identity"
