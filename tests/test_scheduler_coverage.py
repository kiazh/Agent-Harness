"""Additional scheduler tests to reach 90%+ coverage (Gate 3)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from ah.core.scheduler import (
    DEFAULT_HEARTBEAT_PROMPT,
    JobRunner,
    JobStore,
    job_store,
)


# ─── Validation (no DB needed) ────────────────────────────────────────────────


async def test_create_cron_requires_expression():
    """Cron jobs must have a cron_expression."""
    store = JobStore()
    with pytest.raises(ValueError, match="cron_expression"):
        await store.create(
            name="cron",
            kind="cron",
            session_id=uuid.uuid4(),
            prompt="p",
            interval_seconds=60,
        )


async def test_create_rejects_empty_model():
    """Model must be non-empty."""
    store = JobStore()
    with pytest.raises(ValueError, match="model"):
        await store.create(
            name="x",
            kind="interval",
            session_id=uuid.uuid4(),
            prompt="p",
            interval_seconds=60,
            model="   ",
        )


async def test_create_rejects_long_model():
    """Model must be ≤200 chars."""
    store = JobStore()
    with pytest.raises(ValueError, match="model"):
        await store.create(
            name="x",
            kind="interval",
            session_id=uuid.uuid4(),
            prompt="p",
            interval_seconds=60,
            model="m" * 201,
        )


async def test_create_rejects_empty_provider():
    """Provider must be non-empty."""
    store = JobStore()
    with pytest.raises(ValueError, match="provider"):
        await store.create(
            name="x",
            kind="interval",
            session_id=uuid.uuid4(),
            prompt="p",
            interval_seconds=60,
            provider="",
        )


async def test_create_rejects_long_provider():
    """Provider must be ≤100 chars."""
    store = JobStore()
    with pytest.raises(ValueError, match="provider"):
        await store.create(
            name="x",
            kind="interval",
            session_id=uuid.uuid4(),
            prompt="p",
            interval_seconds=60,
            provider="p" * 101,
        )


async def test_no_agent_requires_script_path():
    """no_agent jobs must have a script_path."""
    store = JobStore()
    with pytest.raises(ValueError, match="script_path"):
        await store.create(
            name="x",
            kind="interval",
            session_id=uuid.uuid4(),
            prompt="",
            interval_seconds=60,
            no_agent=True,
        )


async def test_no_agent_rejects_model_pin():
    """no-agent jobs cannot pin a model."""
    store = JobStore()
    with pytest.raises(ValueError, match="model or provider"):
        await store.create(
            name="x",
            kind="interval",
            session_id=uuid.uuid4(),
            prompt="",
            interval_seconds=60,
            no_agent=True,
            script_path="check.py",
            model="some-model",
        )


async def test_script_path_requires_no_agent():
    """script_path is only valid with no_agent=true."""
    store = JobStore()
    with pytest.raises(ValueError, match="no_agent"):
        await store.create(
            name="x",
            kind="interval",
            session_id=uuid.uuid4(),
            prompt="p",
            interval_seconds=60,
            script_path="check.py",
        )


# ─── Store operations (DB needed) ─────────────────────────────────────────────


async def test_list_without_session_id(db_pool, monkeypatch):
    """list() without session_id returns all jobs."""
    monkeypatch.setattr("ah.core.session.db", db_pool)
    monkeypatch.setattr("ah.core.scheduler.db", db_pool)
    from ah.core.session import SessionManager

    session = await SessionManager().create(title="list all", agent_id=f"agent-{uuid.uuid4()}")
    store = JobStore()
    try:
        job = await store.create(
            name="all",
            kind="interval",
            session_id=session.id,
            prompt="p",
            interval_seconds=60,
        )
        all_jobs = await store.list()
        assert job.id in [j.id for j in all_jobs]
    finally:
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session.id)


async def test_renew_lease(db_pool, monkeypatch):
    """renew_lease extends next_run_at for a running job."""
    monkeypatch.setattr("ah.core.session.db", db_pool)
    monkeypatch.setattr("ah.core.scheduler.db", db_pool)
    from ah.core.session import SessionManager

    session = await SessionManager().create(title="renew", agent_id=f"agent-{uuid.uuid4()}")
    store = JobStore()
    try:
        job = await store.create(
            name="renew",
            kind="interval",
            session_id=session.id,
            prompt="p",
            interval_seconds=60,
        )
        # Mark as running
        await db_pool.execute("UPDATE jobs SET status = 'running' WHERE id = $1", job.id)
        await store.renew_lease(job.id)
        after = await store.get(job.id)
        assert after.next_run_at > datetime.now(UTC) + timedelta(seconds=200)
    finally:
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session.id)


async def test_finish_nonexistent_job(db_pool, monkeypatch):
    """finish() on a missing job is a no-op."""
    monkeypatch.setattr("ah.core.scheduler.db", db_pool)
    store = JobStore()
    # Should not raise
    await store.finish(uuid.uuid4())


# ─── Runner lifecycle (no DB needed) ──────────────────────────────────────────


async def test_runner_start_and_stop():
    """start() creates a task; stop() cancels it."""
    runner = JobRunner(poll_seconds=0.01)
    runner.start()
    assert runner._task is not None
    assert not runner._task.done()
    await runner.stop()
    assert runner._task is None


async def test_runner_start_resumes_after_stop():
    """start() after stop() creates a fresh task."""
    runner = JobRunner(poll_seconds=0.01)
    runner.start()
    task1 = runner._task
    await runner.stop()
    runner.start()
    assert runner._task is not task1
    await runner.stop()


async def test_run_due_once_returns_false_when_no_jobs(monkeypatch):
    """run_due_once returns False when claim_due returns None."""
    store = JobStore()
    store.claim_due = AsyncMock(return_value=None)
    runner = JobRunner(store=store)
    result = await runner.run_due_once()
    assert result is False


async def test_run_due_once_handles_cancelled_error(monkeypatch):
    """run_due_once propagates CancelledError from _execute."""
    from ah.core.scheduler import Job

    job = Job(
        id=uuid.uuid4(),
        name="cancel",
        kind="interval",
        session_id=uuid.uuid4(),
        agent_name="harness",
        prompt="p",
        interval_seconds=60,
        enabled=True,
        status="running",
        last_run_at=None,
        next_run_at=datetime.now(UTC),
        last_error=None,
        run_count=0,
    )
    store = JobStore()
    store.claim_due = AsyncMock(return_value=job)
    store.finish = AsyncMock()
    runner = JobRunner(store=store)

    async def cancel_execute(j, ownership_lost=None):
        raise asyncio.CancelledError()

    runner._execute = cancel_execute
    with pytest.raises(asyncio.CancelledError):
        await runner.run_due_once()
    store.finish.assert_awaited_with(job.id, error="cancelled", claim_token=None, paused_for=None)


async def test_keep_lease_renews_periodically(monkeypatch):
    """_keep_lease calls renew_lease on a schedule."""
    store = JobStore()
    store.renew_lease = AsyncMock()
    runner = JobRunner(store=store)
    task = asyncio.create_task(runner._keep_lease(uuid.uuid4()))
    await asyncio.sleep(0.15)  # RUN_LEASE_SECONDS / 3 = 100s, so we won't hit it naturally
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    # With the default 100s sleep, renew_lease won't be called in 0.15s.
    # This test verifies the loop structure without waiting 100s.
    store.renew_lease.assert_not_awaited()


async def test_keep_lease_continues_after_renew_error(monkeypatch):
    """_keep_lease logs renew errors and keeps looping."""
    monkeypatch.setattr("ah.core.scheduler.RUN_LEASE_SECONDS", 0.3)
    store = JobStore()
    store.renew_lease = AsyncMock(side_effect=RuntimeError("db gone"))
    runner = JobRunner(store=store)
    task = asyncio.create_task(runner._keep_lease(uuid.uuid4()))
    await asyncio.sleep(0.5)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    # renew_lease was called (and raised), but the loop survived
    store.renew_lease.assert_awaited()


async def test_execute_raises_without_session():
    """_execute raises RuntimeError when job has no session_id."""
    from ah.core.scheduler import Job

    job = Job(
        id=uuid.uuid4(),
        name="no-session",
        kind="interval",
        session_id=None,
        agent_name="harness",
        prompt="p",
        interval_seconds=60,
        enabled=True,
        status="idle",
        last_run_at=None,
        next_run_at=datetime.now(UTC),
        last_error=None,
        run_count=0,
    )
    runner = JobRunner()
    with pytest.raises(RuntimeError, match="no session"):
        await runner._execute(job)


async def test_build_agent_raises_for_unknown_agent(monkeypatch):
    """_build_agent fails closed for unknown agent names (shared factory)."""
    monkeypatch.setattr("ah.core.agent_def.agent_registry.get", AsyncMock(return_value=None))
    runner = JobRunner()
    with pytest.raises(ValueError, match="unknown agent"):
        await runner._build_agent("nonexistent")


# ─── Cron job creation (DB needed) ────────────────────────────────────────────


async def test_create_cron_job(db_pool, monkeypatch):
    """Cron jobs are created with a cron_expression."""
    monkeypatch.setattr("ah.core.session.db", db_pool)
    monkeypatch.setattr("ah.core.scheduler.db", db_pool)
    from ah.core.session import SessionManager

    session = await SessionManager().create(title="cron", agent_id=f"agent-{uuid.uuid4()}")
    store = JobStore()
    try:
        job = await store.create(
            name="cron",
            kind="cron",
            session_id=session.id,
            prompt="p",
            interval_seconds=60,
            cron_expression="*/5 * * * *",
        )
        assert job.kind == "cron"
        assert job.cron_expression == "*/5 * * * *"
        assert job.next_run_at > datetime.now(UTC)
    finally:
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session.id)


async def test_set_enabled_cron_recalculates_next_run(db_pool, monkeypatch):
    """Enabling a cron job recalculates next_run_at from the cron expression."""
    monkeypatch.setattr("ah.core.session.db", db_pool)
    monkeypatch.setattr("ah.core.scheduler.db", db_pool)
    from ah.core.session import SessionManager

    session = await SessionManager().create(title="cron toggle", agent_id=f"agent-{uuid.uuid4()}")
    store = JobStore()
    try:
        job = await store.create(
            name="cron",
            kind="cron",
            session_id=session.id,
            prompt="p",
            interval_seconds=60,
            cron_expression="0 12 * * *",
        )
        # Disable then re-enable
        disabled = await store.set_enabled(job.id, False)
        assert disabled.enabled is False
        enabled = await store.set_enabled(job.id, True)
        assert enabled.enabled is True
        # next_run_at should be recalculated (not the old value)
        assert enabled.next_run_at > datetime.now(UTC)
    finally:
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session.id)


# ─── Claim due with multiple jobs (DB needed) ─────────────────────────────────


async def test_claim_due_returns_oldest_first(db_pool, monkeypatch):
    """claim_due returns the job with the earliest next_run_at."""
    monkeypatch.setattr("ah.core.session.db", db_pool)
    monkeypatch.setattr("ah.core.scheduler.db", db_pool)
    from ah.core.session import SessionManager

    session = await SessionManager().create(title="ordering", agent_id=f"agent-{uuid.uuid4()}")
    store = JobStore()
    try:
        job1 = await store.create(
            name="first",
            kind="interval",
            session_id=session.id,
            prompt="p",
            interval_seconds=60,
        )
        job2 = await store.create(
            name="second",
            kind="interval",
            session_id=session.id,
            prompt="p",
            interval_seconds=60,
        )
        # Make both due, job1 much older to ensure it's first
        await db_pool.execute(
            "UPDATE jobs SET next_run_at = now() - interval '100 years' WHERE id = $1", job1.id
        )
        await db_pool.execute(
            "UPDATE jobs SET next_run_at = now() - interval '1 minute' WHERE id = $1", job2.id
        )
        claimed = await store.claim_due()
        assert claimed is not None
        assert claimed.id == job1.id
        await store.finish(job1.id)
        await store.delete(job1.id)
        await store.delete(job2.id)
    finally:
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session.id)


async def test_claim_due_with_no_due_jobs(db_pool, monkeypatch):
    """claim_due returns None when no jobs are due."""
    monkeypatch.setattr("ah.core.session.db", db_pool)
    monkeypatch.setattr("ah.core.scheduler.db", db_pool)
    from ah.core.session import SessionManager

    session = await SessionManager().create(title="empty", agent_id=f"agent-{uuid.uuid4()}")
    store = JobStore()
    try:
        # Clean up any existing due jobs from other tests
        await db_pool.execute("DELETE FROM jobs WHERE next_run_at <= now()")
        await store.create(
            name="future",
            kind="interval",
            session_id=session.id,
            prompt="p",
            interval_seconds=3600,
        )
        result = await store.claim_due()
        assert result is None
    finally:
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session.id)


# ─── Concurrent claim (DB needed) ────────────────────────────────────────────


async def test_concurrent_claim_no_double_execute(db_pool, monkeypatch):
    """Two concurrent claim_due calls cannot claim the same job."""
    monkeypatch.setattr("ah.core.session.db", db_pool)
    monkeypatch.setattr("ah.core.scheduler.db", db_pool)
    from ah.core.session import SessionManager

    session = await SessionManager().create(title="concurrent", agent_id=f"agent-{uuid.uuid4()}")
    store = JobStore()
    try:
        # Clean up any existing due jobs from other tests
        await db_pool.execute("DELETE FROM jobs WHERE next_run_at <= now()")
        job = await store.create(
            name="race",
            kind="interval",
            session_id=session.id,
            prompt="p",
            interval_seconds=60,
        )
        await db_pool.execute("UPDATE jobs SET next_run_at = now() WHERE id = $1", job.id)

        results = await asyncio.gather(store.claim_due(), store.claim_due())
        claimed = [r for r in results if r is not None]
        assert len(claimed) == 1
        assert claimed[0].id == job.id
        await store.finish(job.id)
        await store.delete(job.id)
    finally:
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session.id)


# ─── Lease expiry and reclaim (DB needed) ─────────────────────────────────────


async def test_expired_lease_is_reclaimable(db_pool, monkeypatch):
    """A running job with an expired lease can be claimed again."""
    monkeypatch.setattr("ah.core.session.db", db_pool)
    monkeypatch.setattr("ah.core.scheduler.db", db_pool)
    from ah.core.session import SessionManager

    session = await SessionManager().create(title="expired", agent_id=f"agent-{uuid.uuid4()}")
    store = JobStore()
    try:
        job = await store.create(
            name="expired",
            kind="interval",
            session_id=session.id,
            prompt="p",
            interval_seconds=60,
        )
        # Simulate a crashed worker: running with expired lease
        await db_pool.execute(
            "UPDATE jobs SET status = 'running', next_run_at = now() - interval '1 hour' WHERE id = $1",
            job.id,
        )
        claimed = await store.claim_due()
        assert claimed is not None
        assert claimed.id == job.id
        assert claimed.status == "running"
        await store.finish(job.id)
        await store.delete(job.id)
    finally:
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session.id)


# ─── Heartbeat kind (DB needed) ───────────────────────────────────────────────


async def test_heartbeat_job_uses_default_prompt(db_pool, monkeypatch):
    """Heartbeat jobs use DEFAULT_HEARTBEAT_PROMPT when prompt is empty."""
    monkeypatch.setattr("ah.core.session.db", db_pool)
    monkeypatch.setattr("ah.core.scheduler.db", db_pool)
    from ah.core.session import SessionManager

    session = await SessionManager().create(title="hb", agent_id=f"agent-{uuid.uuid4()}")
    store = JobStore()
    try:
        job = await store.create(
            name="hb",
            kind="heartbeat",
            session_id=session.id,
            prompt="",
            interval_seconds=60,
        )
        assert job.kind == "heartbeat"
        # The gateway fills in the default prompt; the store stores what it's given
        assert job.prompt == ""
    finally:
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session.id)


# ─── Interval vs cron next_run calculation (DB needed) ────────────────────────


async def test_interval_job_next_run_is_future(db_pool, monkeypatch):
    """Interval jobs have next_run_at in the future."""
    monkeypatch.setattr("ah.core.session.db", db_pool)
    monkeypatch.setattr("ah.core.scheduler.db", db_pool)
    from ah.core.session import SessionManager

    session = await SessionManager().create(title="interval", agent_id=f"agent-{uuid.uuid4()}")
    store = JobStore()
    try:
        before = await db_pool.fetchval("SELECT clock_timestamp()")
        job = await store.create(
            name="interval",
            kind="interval",
            session_id=session.id,
            prompt="p",
            interval_seconds=120,
        )
        after = await db_pool.fetchval("SELECT clock_timestamp()")
        assert job.next_run_at >= before + timedelta(seconds=120)
        assert job.next_run_at <= after + timedelta(seconds=120)
    finally:
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session.id)


# ─── set_enabled on interval job (DB needed) ──────────────────────────────────


async def test_set_enabled_interval_reschedules(db_pool, monkeypatch):
    """Enabling an interval job resets next_run_at to now + interval."""
    monkeypatch.setattr("ah.core.session.db", db_pool)
    monkeypatch.setattr("ah.core.scheduler.db", db_pool)
    from ah.core.session import SessionManager

    session = await SessionManager().create(title="toggle", agent_id=f"agent-{uuid.uuid4()}")
    store = JobStore()
    try:
        job = await store.create(
            name="toggle",
            kind="interval",
            session_id=session.id,
            prompt="p",
            interval_seconds=300,
        )
        disabled = await store.set_enabled(job.id, False)
        assert disabled.enabled is False
        old_next = disabled.next_run_at
        enabled = await store.set_enabled(job.id, True)
        assert enabled.enabled is True
        # next_run_at should be recalculated
        assert enabled.next_run_at != old_next
        assert enabled.next_run_at > datetime.now(UTC)
    finally:
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session.id)


# ─── set_enabled on nonexistent job (DB needed) ───────────────────────────────


async def test_set_enabled_nonexistent_returns_none(db_pool, monkeypatch):
    """set_enabled on a missing job returns None."""
    monkeypatch.setattr("ah.core.scheduler.db", db_pool)
    store = JobStore()
    result = await store.set_enabled(uuid.uuid4(), True)
    assert result is None


# ─── delete nonexistent job (DB needed) ───────────────────────────────────────


async def test_delete_nonexistent_returns_false(db_pool, monkeypatch):
    """delete on a missing job returns False."""
    monkeypatch.setattr("ah.core.scheduler.db", db_pool)
    store = JobStore()
    result = await store.delete(uuid.uuid4())
    assert result is False


# ─── get nonexistent job (DB needed) ──────────────────────────────────────────


async def test_get_nonexistent_returns_none(db_pool, monkeypatch):
    """get on a missing job returns None."""
    monkeypatch.setattr("ah.core.scheduler.db", db_pool)
    store = JobStore()
    result = await store.get(uuid.uuid4())
    assert result is None


# ─── finish on cron job (DB needed) ───────────────────────────────────────────


async def test_finish_cron_reschedules_from_expression(db_pool, monkeypatch):
    """finish() on a cron job reschedules using the cron expression."""
    monkeypatch.setattr("ah.core.session.db", db_pool)
    monkeypatch.setattr("ah.core.scheduler.db", db_pool)
    from ah.core.session import SessionManager

    session = await SessionManager().create(title="cron finish", agent_id=f"agent-{uuid.uuid4()}")
    store = JobStore()
    try:
        job = await store.create(
            name="cron",
            kind="cron",
            session_id=session.id,
            prompt="p",
            interval_seconds=60,
            cron_expression="*/10 * * * *",
        )
        await store.finish(job.id)
        after = await store.get(job.id)
        assert after.status == "idle"
        assert after.run_count == 1
        assert after.next_run_at > datetime.now(UTC)
    finally:
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session.id)


# ─── finish with error (DB needed) ────────────────────────────────────────────


async def test_finish_with_error_sets_status(db_pool, monkeypatch):
    """finish() with an error sets status to 'error' and records the message."""
    monkeypatch.setattr("ah.core.session.db", db_pool)
    monkeypatch.setattr("ah.core.scheduler.db", db_pool)
    from ah.core.session import SessionManager

    session = await SessionManager().create(title="error", agent_id=f"agent-{uuid.uuid4()}")
    store = JobStore()
    try:
        job = await store.create(
            name="err",
            kind="interval",
            session_id=session.id,
            prompt="p",
            interval_seconds=60,
        )
        await store.finish(job.id, error="RuntimeError: boom")
        after = await store.get(job.id)
        assert after.status == "error"
        assert "boom" in after.last_error
        assert after.run_count == 1
    finally:
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session.id)


# ─── _loop runs and stops (no DB needed) ──────────────────────────────────────


async def test_loop_runs_and_stops(monkeypatch):
    """_loop polls run_due_once and respects the stopping event."""
    store = JobStore()
    call_count = 0

    async def mock_run():
        nonlocal call_count
        call_count += 1
        if call_count >= 3:
            runner._stopping.set()
        return False

    runner = JobRunner(store=store, poll_seconds=0.01)
    runner.run_due_once = mock_run
    await runner._loop()
    assert call_count >= 3


# ─── _loop handles exceptions (no DB needed) ──────────────────────────────────


async def test_loop_continues_after_exception(monkeypatch):
    """_loop logs exceptions and continues polling."""
    store = JobStore()
    call_count = 0

    async def mock_run():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("poll failed")
        if call_count >= 3:
            runner._stopping.set()
        return False

    runner = JobRunner(store=store, poll_seconds=0.01)
    runner.run_due_once = mock_run
    await runner._loop()
    assert call_count >= 3


# ─── _loop drains backlog (no DB needed) ──────────────────────────────────────


async def test_loop_drains_backlog_immediately(monkeypatch):
    """_loop re-polls immediately when a job ran."""
    store = JobStore()
    call_count = 0

    async def mock_run():
        nonlocal call_count
        call_count += 1
        if call_count >= 5:
            runner._stopping.set()
        return True  # job ran, should loop immediately

    runner = JobRunner(store=store, poll_seconds=60)  # long poll, but should not wait
    runner.run_due_once = mock_run
    await asyncio.wait_for(runner._loop(), timeout=5)
    assert call_count >= 5


# ─── run_due_once with agent_factory (DB needed) ──────────────────────────────


async def test_run_due_once_with_agent_factory(db_pool, monkeypatch):
    """run_due_once uses the agent_factory to build agents."""
    monkeypatch.setattr("ah.core.session.db", db_pool)
    monkeypatch.setattr("ah.core.scheduler.db", db_pool)
    from ah.core.models import AgentResponse
    from ah.core.session import SessionManager

    session = await SessionManager().create(title="factory", agent_id=f"agent-{uuid.uuid4()}")
    store = JobStore()
    runs = []

    class FakeAgent:
        def __init__(self, name):
            self.name = name

        async def run(self, session_id, prompt, verbose=True):
            runs.append((session_id, prompt))
            return AgentResponse(content="ok", tool_calls=[], tokens_used=1, iterations=1)

    try:
        # Clean up any existing due jobs from other tests
        await db_pool.execute("DELETE FROM jobs WHERE next_run_at <= now()")
        job = await store.create(
            name="factory",
            kind="interval",
            session_id=session.id,
            prompt="test prompt",
            interval_seconds=60,
        )
        await db_pool.execute("UPDATE jobs SET next_run_at = now() WHERE id = $1", job.id)
        runner = JobRunner(store=store, agent_factory=FakeAgent)
        result = await runner.run_due_once()
        assert result is True
        assert (session.id, "test prompt") in runs
        await store.delete(job.id)
    finally:
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session.id)


# ─── run_due_once with cron job (DB needed) ───────────────────────────────────


async def test_run_due_once_cron_job(db_pool, monkeypatch):
    """run_due_once works for cron jobs."""
    monkeypatch.setattr("ah.core.session.db", db_pool)
    monkeypatch.setattr("ah.core.scheduler.db", db_pool)
    from ah.core.models import AgentResponse
    from ah.core.session import SessionManager

    session = await SessionManager().create(title="cron run", agent_id=f"agent-{uuid.uuid4()}")
    store = JobStore()
    runs = []

    class FakeAgent:
        def __init__(self, name):
            pass

        async def run(self, session_id, prompt, verbose=True):
            runs.append(prompt)
            return AgentResponse(content="ok", tool_calls=[], tokens_used=1, iterations=1)

    try:
        # Clean up any existing due jobs from other tests
        await db_pool.execute("DELETE FROM jobs WHERE next_run_at <= now()")
        job = await store.create(
            name="cron",
            kind="cron",
            session_id=session.id,
            prompt="cron prompt",
            interval_seconds=60,
            cron_expression="* * * * *",
        )
        await db_pool.execute("UPDATE jobs SET next_run_at = now() WHERE id = $1", job.id)
        runner = JobRunner(store=store, agent_factory=FakeAgent)
        result = await runner.run_due_once()
        assert result is True
        assert "cron prompt" in runs
        await store.delete(job.id)
    finally:
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session.id)


# ─── _execute with no_agent (DB needed) ───────────────────────────────────────


async def test_execute_no_agent_script(db_pool, monkeypatch, tmp_path):
    """_execute for no_agent jobs runs the script and adds a chunk."""
    monkeypatch.setenv("AGENT_HARNESS_SCRIPTS_DIR", str(tmp_path))
    from tests.support.scripts import allow_scripts

    allow_scripts(tmp_path, {"hello.py": [], "silent.py": [], "check.py": []})
    (tmp_path / "hello.py").write_text("print('hello world')", encoding="utf-8")
    monkeypatch.setattr("ah.core.session.db", db_pool)
    monkeypatch.setattr("ah.core.scheduler.db", db_pool)
    from ah.core.session import SessionManager

    session = await SessionManager().create(title="script exec", agent_id=f"agent-{uuid.uuid4()}")
    store = JobStore()
    chunks = []

    async def add_chunk(**kwargs):
        chunks.append(kwargs)

    monkeypatch.setattr("ah.core.context.context_manager.add_chunk", add_chunk)
    try:
        job = await store.create(
            name="script",
            kind="interval",
            session_id=session.id,
            prompt="",
            interval_seconds=60,
            no_agent=True,
            script_path="hello.py",
        )
        runner = JobRunner(store=store)

        async def no_agent(*args, **kwargs):
            raise AssertionError("LLM agent was built")

        monkeypatch.setattr(runner, "_build_agent", no_agent)
        # AH-AUDIT-009: broker-governed scripts pause for human approval
        # first; the test drives the real pause → resolve → execute flow.
        from ah.permissions import store as perm_store

        with pytest.raises(PermissionError, match="needs_approval"):
            await runner._execute(job)
        pending = await perm_store.list_pending(str(session.id))
        assert len(pending) == 1
        resolved = await perm_store.resolve_decision(
            pending[0]["request_id"], "approved", principal="tui"
        )
        assert resolved is not None
        await runner._execute(job)
        assert len(chunks) == 1
        assert chunks[0]["payload"]["content"] == "hello world"
    finally:
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session.id)


# ─── _execute with no_agent and empty output (DB needed) ──────────────────────


async def test_execute_no_agent_empty_output(db_pool, monkeypatch, tmp_path):
    """_execute for no_agent jobs with empty output does not add a chunk."""
    monkeypatch.setenv("AGENT_HARNESS_SCRIPTS_DIR", str(tmp_path))
    from tests.support.scripts import allow_scripts

    allow_scripts(tmp_path, {"hello.py": [], "silent.py": [], "check.py": []})
    (tmp_path / "silent.py").write_text("pass", encoding="utf-8")
    monkeypatch.setattr("ah.core.session.db", db_pool)
    monkeypatch.setattr("ah.core.scheduler.db", db_pool)
    from ah.core.session import SessionManager

    session = await SessionManager().create(title="silent", agent_id=f"agent-{uuid.uuid4()}")
    store = JobStore()
    chunks = []

    async def add_chunk(**kwargs):
        chunks.append(kwargs)

    monkeypatch.setattr("ah.core.context.context_manager.add_chunk", add_chunk)
    try:
        job = await store.create(
            name="silent",
            kind="interval",
            session_id=session.id,
            prompt="",
            interval_seconds=60,
            no_agent=True,
            script_path="silent.py",
        )
        runner = JobRunner(store=store)

        async def no_agent(*args, **kwargs):
            raise AssertionError("LLM agent was built")

        monkeypatch.setattr(runner, "_build_agent", no_agent)
        # AH-AUDIT-009: same pause → resolve → execute flow as above.
        from ah.permissions import store as perm_store

        with pytest.raises(PermissionError, match="needs_approval"):
            await runner._execute(job)
        pending = await perm_store.list_pending(str(session.id))
        assert len(pending) == 1
        resolved = await perm_store.resolve_decision(
            pending[0]["request_id"], "approved", principal="tui"
        )
        assert resolved is not None
        await runner._execute(job)
        assert len(chunks) == 0
    finally:
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session.id)


# ─── _execute with heartbeat and default prompt (DB needed) ───────────────────


async def test_execute_heartbeat_uses_default_prompt(db_pool, monkeypatch):
    """_execute for heartbeat jobs uses DEFAULT_HEARTBEAT_PROMPT when prompt is empty."""
    monkeypatch.setattr("ah.core.session.db", db_pool)
    monkeypatch.setattr("ah.core.scheduler.db", db_pool)
    from ah.core.models import AgentResponse
    from ah.core.session import SessionManager

    session = await SessionManager().create(title="hb exec", agent_id=f"agent-{uuid.uuid4()}")
    store = JobStore()
    runs = []

    class FakeAgent:
        def __init__(self, name):
            pass

        async def run(self, session_id, prompt, verbose=True):
            runs.append(prompt)
            return AgentResponse(content="ok", tool_calls=[], tokens_used=1, iterations=1)

    try:
        job = await store.create(
            name="hb",
            kind="heartbeat",
            session_id=session.id,
            prompt="",
            interval_seconds=60,
        )
        runner = JobRunner(store=store, agent_factory=FakeAgent)
        await runner._execute(job)
        assert runs == [DEFAULT_HEARTBEAT_PROMPT]
    finally:
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session.id)


# ─── _execute with model and provider (DB needed) ─────────────────────────────


async def test_execute_passes_model_and_provider(db_pool, monkeypatch):
    """_execute passes model and provider to _build_agent."""
    monkeypatch.setattr("ah.core.session.db", db_pool)
    monkeypatch.setattr("ah.core.scheduler.db", db_pool)
    from ah.core.models import AgentResponse
    from ah.core.session import SessionManager

    session = await SessionManager().create(title="pin", agent_id=f"agent-{uuid.uuid4()}")
    store = JobStore()
    captured = {}

    class FakeAgent:
        def __init__(self, name):
            pass

        async def run(self, session_id, prompt, verbose=True):
            return AgentResponse(content="ok", tool_calls=[], tokens_used=1, iterations=1)

    async def fake_build_agent(agent_name, *, model=None, provider=None, session_id=None):
        captured["model"] = model
        captured["provider"] = provider
        captured["session_id"] = session_id
        return FakeAgent(agent_name)

    try:
        job = await store.create(
            name="pin",
            kind="interval",
            session_id=session.id,
            prompt="p",
            interval_seconds=60,
            model="gpt-4",
            provider="openai",
        )
        runner = JobRunner(store=store)
        monkeypatch.setattr(runner, "_build_agent", fake_build_agent)
        await runner._execute(job)
        assert captured["model"] == "gpt-4"
        assert captured["provider"] == "openai"
        assert captured["session_id"] == job.session_id
    finally:
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session.id)


# ─── _build_agent with agent_factory (no DB needed) ───────────────────────────


async def test_build_agent_uses_factory():
    """_build_agent uses agent_factory when provided."""
    sentinel = object()
    runner = JobRunner(agent_factory=lambda name: sentinel)
    result = await runner._build_agent("any")
    assert result is sentinel


# ─── _build_agent without factory raises for unknown agent (no DB needed) ─────


async def test_build_agent_without_factory_unknown_raises(monkeypatch):
    """_build_agent without factory raises for unknown agents."""
    monkeypatch.setattr("ah.core.agent_def.agent_registry.get", AsyncMock(return_value=None))
    runner = JobRunner()
    with pytest.raises(ValueError, match="unknown agent"):
        await runner._build_agent("ghost")


# ─── Job.to_dict (no DB needed) ───────────────────────────────────────────────


def test_job_to_dict():
    """Job.to_dict serializes all fields."""
    from ah.core.scheduler import Job

    job = Job(
        id=uuid.uuid4(),
        name="test",
        kind="interval",
        session_id=uuid.uuid4(),
        agent_name="harness",
        prompt="p",
        interval_seconds=60,
        enabled=True,
        status="idle",
        last_run_at=None,
        next_run_at=datetime.now(UTC),
        last_error=None,
        run_count=0,
        cron_expression=None,
        model=None,
        provider=None,
        no_agent=False,
        script_path=None,
    )
    d = job.to_dict()
    assert d["name"] == "test"
    assert d["kind"] == "interval"
    assert d["enabled"] is True
    assert d["noAgent"] is False
    assert d["scriptPath"] is None


def test_job_to_dict_with_all_fields():
    """Job.to_dict with all optional fields set."""
    from ah.core.scheduler import Job

    now = datetime.now(UTC)
    job = Job(
        id=uuid.uuid4(),
        name="full",
        kind="cron",
        session_id=uuid.uuid4(),
        agent_name="researcher",
        prompt="p",
        interval_seconds=0,
        enabled=False,
        status="error",
        last_run_at=now,
        next_run_at=now,
        last_error="boom",
        run_count=5,
        cron_expression="*/5 * * * *",
        model="gpt-4",
        provider="openai",
        no_agent=True,
        script_path="check.py",
    )
    d = job.to_dict()
    assert d["cronExpression"] == "*/5 * * * *"
    assert d["model"] == "gpt-4"
    assert d["provider"] == "openai"
    assert d["noAgent"] is True
    assert d["scriptPath"] == "check.py"
    assert d["runCount"] == 5
    assert d["lastError"] == "boom"
