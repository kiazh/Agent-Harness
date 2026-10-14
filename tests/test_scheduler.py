"""Phase 6a scheduler: JobStore, atomic claim, JobRunner, and gateway job methods."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

import pytest

from machina.core.models import AgentResponse
from machina.gateway.server import INVALID_PARAMS, NOT_FOUND
from tests.test_gateway import Harness

TEST_DSN = os.environ.get("MACHINA_TEST_DATABASE_URL", "")
needs_db = pytest.mark.skipif(not TEST_DSN, reason="MACHINA_TEST_DATABASE_URL not set")


@pytest.fixture
async def connected_db():
    from machina.db.connection import db

    await db.connect()
    yield
    await db.close()


async def a_session() -> uuid.UUID:
    from machina.core.session import session_manager

    return (await session_manager.create(title="scheduler test")).id


async def _claim_specific(job_id: uuid.UUID):
    """Claim due jobs until *job_id* is claimed (returning it) or none remain.

    The jobs table is shared across tests, so a plain claim_due() may return
    another test's job; finish those and keep going.
    """
    from machina.core.scheduler import job_store

    for _ in range(100):
        claimed = await job_store.claim_due()
        if claimed is None:
            return None
        if claimed.id == job_id:
            return claimed
        await job_store.finish(claimed.id)
    return None


async def _drain_until_run(runner, job_id: uuid.UUID) -> None:
    """Run due jobs until *job_id* has executed once (draining siblings)."""
    from machina.core.scheduler import job_store

    for _ in range(100):
        if not await runner.run_due_once():
            break
        job = await job_store.get(job_id)
        if job is not None and job.run_count >= 1:
            return
    raise AssertionError("target job never ran")


class RecordingAgent:
    """Fake agent that records which sessions and prompts it was run with."""

    runs: list[tuple[uuid.UUID, str]] = []

    def __init__(self, agent_name: str) -> None:
        self.agent_name = agent_name

    async def run(self, session_id, user_message, verbose=True) -> AgentResponse:
        RecordingAgent.runs.append((session_id, user_message))
        return AgentResponse(content="done", tool_calls=[], tokens_used=5, iterations=1)


# ─── validation (no DB) ────────────────────────────────────────────────────────


def test_job_create_validates_kind_and_interval():
    import asyncio

    from machina.core.scheduler import JobStore

    store = JobStore()

    async def check():
        # kind/interval are validated before any DB call.
        with pytest.raises(ValueError, match="kind"):
            await store.create(
                name="x", kind="bogus", session_id=uuid.uuid4(), prompt="p", interval_seconds=60
            )
        with pytest.raises(ValueError, match="at least"):
            await store.create(
                name="x", kind="interval", session_id=uuid.uuid4(), prompt="p", interval_seconds=5
            )

    asyncio.run(check())


async def test_failed_agent_outcome_is_recorded_as_error(monkeypatch):
    """A non-completed typed AgentResponse outcome is a failed job, not an
    idle success: finish() must record an error naming the outcome."""
    from machina.core import turns
    from machina.core.scheduler import Job, JobRunner

    async def fake_begin(session_id):
        return "turn-token"

    async def fake_end(session_id, token):
        return None

    monkeypatch.setattr(turns, "try_begin_turn", fake_begin)
    monkeypatch.setattr(turns, "end_turn", fake_end)

    job = Job(
        id=uuid.uuid4(),
        name="failed outcome",
        kind="interval",
        session_id=uuid.uuid4(),
        agent_name="harness",
        prompt="work",
        interval_seconds=60,
        enabled=True,
        status="running",
        last_run_at=None,
        next_run_at=datetime.now(UTC),
        last_error=None,
        run_count=0,
        claim_token=uuid.uuid4(),
    )
    finished: dict = {}

    class Store:
        async def claim_due(self):
            return job

        async def renew_lease(self, job_id, claim_token):
            return True

        async def finish(self, job_id, *, error, claim_token, paused_for):
            finished["error"] = error
            finished["paused_for"] = paused_for
            return True

    class Agent:
        async def run(self, session_id, prompt, verbose):
            return AgentResponse(content="task could not be completed", outcome="failed")

    runner = JobRunner(store=Store(), agent_factory=lambda _name: Agent())
    assert await runner.run_due_once() is True
    assert finished["error"] is not None
    assert "failed" in finished["error"]
    assert finished["paused_for"] is None


# ─── store + runner (real DB) ──────────────────────────────────────────────────


@needs_db
@pytest.mark.usefixtures("connected_db")
class TestJobStore:
    async def test_create_list_toggle_delete(self):
        from machina.core.scheduler import job_store

        sid = await a_session()
        job = await job_store.create(
            name="nightly",
            kind="interval",
            session_id=sid,
            prompt="run nightly",
            interval_seconds=3600,
        )
        assert job.enabled is True and job.status == "idle" and job.run_count == 0
        assert job.next_run_at > datetime.now(UTC)

        listed = await job_store.list(session_id=sid)
        assert [j.id for j in listed] == [job.id]

        disabled = await job_store.set_enabled(job.id, False)
        assert disabled.enabled is False
        assert await job_store.delete(job.id) is True
        assert await job_store.get(job.id) is None

    async def test_claim_due_is_atomic_and_respects_enabled(self):
        from machina.core.scheduler import job_store

        sid = await a_session()
        job = await job_store.create(
            name="due", kind="interval", session_id=sid, prompt="go", interval_seconds=60
        )
        # Not due yet (next_run is a minute out).
        assert await _claim_specific(job.id) is None

        from machina.db.connection import db

        # Sort ahead of any older due jobs left by prior tests in the shared DB.
        await db.execute(
            "UPDATE jobs SET next_run_at = now() - interval '100 years' WHERE id = $1",
            job.id,
        )
        claimed = await _claim_specific(job.id)
        assert claimed is not None and claimed.status == "running"
        # Already running: our job cannot be claimed again.
        assert await _claim_specific(job.id) is None

        # Strict fencing: the exact claim token is required to finish claimed work.
        assert await job_store.finish(job.id) is False
        assert await job_store.finish(job.id, claim_token=claimed.claim_token) is True
        done = await job_store.get(job.id)
        # finish() reschedules from the real wall clock (now + interval).
        assert done.status == "idle" and done.run_count == 1
        assert done.next_run_at > datetime.now(UTC)
        await job_store.delete(job.id)

    async def test_disabled_jobs_are_not_claimed(self):
        from machina.core.scheduler import job_store

        sid = await a_session()
        job = await job_store.create(
            name="off", kind="interval", session_id=sid, prompt="go", interval_seconds=60
        )
        await job_store.set_enabled(job.id, False)
        from machina.db.connection import db

        # Force due; a disabled job must never be claimed.
        await db.execute("UPDATE jobs SET next_run_at = now() WHERE id = $1", job.id)
        assert await _claim_specific(job.id) is None
        await job_store.delete(job.id)

    async def test_expired_running_job_is_reclaimed(self):
        from machina.core.scheduler import job_store
        from machina.db.connection import db

        sid = await a_session()
        job = await job_store.create(
            name="recover", kind="interval", session_id=sid, prompt="go", interval_seconds=60
        )
        await db.execute(
            "UPDATE jobs SET status = 'running', next_run_at = now() - interval '365 days' WHERE id = $1",
            job.id,
        )
        claimed = await _claim_specific(job.id)
        assert claimed is not None and claimed.status == "running"
        assert claimed.next_run_at > datetime.now(UTC)
        await job_store.finish(job.id)
        await job_store.delete(job.id)


@needs_db
@pytest.mark.usefixtures("connected_db")
class TestJobRunner:
    async def test_run_due_once_executes_and_reschedules(self):
        from machina.core.scheduler import JobRunner, job_store

        RecordingAgent.runs = []
        sid = await a_session()
        job = await job_store.create(
            name="interval",
            kind="interval",
            session_id=sid,
            prompt="do the thing",
            interval_seconds=30,
        )
        from machina.db.connection import db

        await db.execute("UPDATE jobs SET next_run_at = now() WHERE id = $1", job.id)

        await _drain_until_run(JobRunner(agent_factory=RecordingAgent), job.id)
        assert (sid, "do the thing") in RecordingAgent.runs
        after = await job_store.get(job.id)
        assert after.status == "idle" and after.run_count == 1
        await job_store.delete(job.id)

    async def test_heartbeat_uses_default_prompt(self):
        from machina.core.scheduler import DEFAULT_HEARTBEAT_PROMPT, JobRunner, job_store

        RecordingAgent.runs = []
        sid = await a_session()
        job = await job_store.create(
            name="hb",
            kind="heartbeat",
            session_id=sid,
            prompt=DEFAULT_HEARTBEAT_PROMPT,
            interval_seconds=30,
        )
        from machina.db.connection import db

        await db.execute("UPDATE jobs SET next_run_at = now() WHERE id = $1", job.id)
        await _drain_until_run(JobRunner(agent_factory=RecordingAgent), job.id)
        assert (sid, DEFAULT_HEARTBEAT_PROMPT) in RecordingAgent.runs
        await job_store.delete(job.id)

    async def test_runner_records_errors(self):
        from machina.core.scheduler import JobRunner, job_store

        sid = await a_session()
        job = await job_store.create(
            name="boom", kind="interval", session_id=sid, prompt="x", interval_seconds=30
        )
        from machina.db.connection import db

        await db.execute("UPDATE jobs SET next_run_at = now() WHERE id = $1", job.id)

        class Exploding:
            def __init__(self, name):
                pass

            async def run(self, *a, **k):
                raise RuntimeError("kaboom")

        await _drain_until_run(JobRunner(agent_factory=Exploding), job.id)
        after = await job_store.get(job.id)
        assert after.status == "error" and "kaboom" in after.last_error
        await job_store.delete(job.id)


# ─── R12: session-claim renewal across effects + quarantine ─────────────────────


def _renewal_job(session_id: uuid.UUID):
    from machina.core.scheduler import Job

    return Job(
        id=uuid.uuid4(),
        name="claim-renewal",
        kind="interval",
        session_id=session_id,
        agent_name="harness",
        prompt="work",
        interval_seconds=60,
        enabled=True,
        status="running",
        last_run_at=None,
        next_run_at=datetime.now(UTC),
        last_error=None,
        run_count=0,
        claim_token=uuid.uuid4(),
    )


@needs_db
@pytest.mark.usefixtures("connected_db")
async def test_quarantined_job_keeps_its_session_claim_alive(monkeypatch):
    """R12: the session claim is renewed while a resistant effect is quarantined.

    Compressed clocks: 1s claim TTL, 0.15s job timeout. Pre-fix the claim
    expired ~1s in and a second ``try_begin_turn`` succeeded while the
    quarantined effect was still active.
    """
    import asyncio

    from machina.core import scheduler as scheduler_module
    from machina.core import turns
    from machina.core.scheduler import JobRunner
    from machina.core.session import session_manager

    session = await session_manager.create(title="R12 claim renewal")
    release = asyncio.Event()
    resisted = asyncio.Event()

    class Agent:
        async def run(self, *args, **kwargs):
            try:
                await release.wait()
            except asyncio.CancelledError:
                resisted.set()
                await release.wait()
            return AgentResponse(content="done")

    class Store:
        def __init__(self, job):
            self.job = job

        async def claim_due(self):
            job, self.job = self.job, None
            return job

        async def renew_lease(self, *args):
            return True

        async def finish(self, *args, **kwargs):
            return True

    original_begin = turns.try_begin_turn

    async def short_claim(session_id, **kwargs):
        # Same claim contract, compressed TTL for the test clock.
        return await original_begin(session_id, ttl_s=1)

    runner = JobRunner(store=Store(_renewal_job(session.id)), agent_factory=lambda _n: Agent())
    monkeypatch.setattr(scheduler_module, "RUN_LEASE_SECONDS", 0.15)
    monkeypatch.setattr(turns, "try_begin_turn", short_claim)
    try:
        assert await runner.run_due_once() is True
        assert resisted.is_set()
        assert runner._quarantined
        await asyncio.sleep(1.05)
        # The quarantined effect still owns the session: no other turn can
        # claim it while the old job is active.
        second = await original_begin(session.id)
        try:
            assert second is None
        finally:
            if second:
                await turns.end_turn(session.id, second)
        release.set()
        await asyncio.gather(*tuple(runner._quarantined), return_exceptions=True)
    finally:
        release.set()
        await runner.stop()
        await session_manager.delete(session.id)


async def test_quarantined_effects_are_fenced_when_claim_renewal_fails(monkeypatch):
    """R12: losing the session claim cancels effects still in quarantine."""
    import asyncio
    from unittest.mock import AsyncMock

    from machina.core import scheduler as scheduler_module
    from machina.core import turns
    from machina.core.scheduler import JobRunner
    from machina.db.connection import db as live_db

    release = asyncio.Event()
    first_cancel, fenced = asyncio.Event(), asyncio.Event()
    allow_loss = asyncio.Event()

    class Agent:
        async def run(self, *args, **kwargs):
            try:
                await release.wait()
            except asyncio.CancelledError:
                first_cancel.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    fenced.set()
                    raise
            return AgentResponse(content="done")

    class Store:
        def __init__(self, job):
            self.job = job

        async def claim_due(self):
            job, self.job = self.job, None
            return job

        async def renew_lease(self, *args):
            return True

        async def finish(self, *args, **kwargs):
            return True

    async def renew_session_claim(session_id, token):
        assert token == "job-turn"
        return not allow_loss.is_set()

    end_turn = AsyncMock(return_value=True)
    runner = JobRunner(store=Store(_renewal_job(uuid.uuid4())), agent_factory=lambda _n: Agent())
    monkeypatch.setattr(scheduler_module, "RUN_LEASE_SECONDS", 0.15)
    monkeypatch.setattr(turns, "try_begin_turn", AsyncMock(return_value="job-turn"))
    monkeypatch.setattr(turns, "end_turn", end_turn)
    monkeypatch.setattr(turns, "renew_turn", renew_session_claim)
    # Arm the DB-backed renewal path without a real pool: renewal loss is
    # simulated through the patched renew_turn above.
    monkeypatch.setattr(live_db, "_pool", object())

    try:
        run = asyncio.create_task(runner.run_due_once())
        await asyncio.wait_for(first_cancel.wait(), 1)
        await asyncio.wait_for(run, 1)
        assert runner._quarantined
        allow_loss.set()
        # The next renewal reports loss; the fence must cancel the effect.
        await asyncio.wait_for(fenced.wait(), 2)
        deadline = asyncio.get_running_loop().time() + 1
        while runner._quarantined and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        assert not runner._quarantined
        assert end_turn.await_count == 1
    finally:
        release.set()
        allow_loss.set()
        for task in list(runner._quarantined):
            task.cancel()
        await asyncio.gather(*list(runner._quarantined), return_exceptions=True)
        await runner.stop()


# ─── gateway methods (real DB) ──────────────────────────────────────────────────


@needs_db
class TestGatewayJobs:
    @pytest.fixture
    async def h(self):
        harness = Harness()
        assert "result" in await harness.call("initialize")
        yield harness
        await harness.gateway.close()

    async def test_create_list_toggle_delete(self, h):
        sid = (await h.call("session.create", {"title": "jobs"}))["result"]["session"]["id"]
        created = (
            await h.call(
                "jobs.create",
                {
                    "sessionId": sid,
                    "kind": "interval",
                    "name": "nightly",
                    "prompt": "run it",
                    "intervalSeconds": 60,
                },
            )
        )["result"]["job"]
        assert created["kind"] == "interval" and created["enabled"] is True

        jobs = (await h.call("jobs.list", {"sessionId": sid}))["result"]["jobs"]
        assert created["id"] in {j["id"] for j in jobs}

        toggled = (await h.call("jobs.setEnabled", {"id": created["id"], "enabled": False}))[
            "result"
        ]["job"]
        assert toggled["enabled"] is False

        assert (await h.call("jobs.delete", {"id": created["id"]}))["result"] == {"deleted": True}
        assert (await h.call("jobs.delete", {"id": created["id"]}))["error"]["code"] == NOT_FOUND

    async def test_heartbeat_defaults_prompt(self, h):
        sid = (await h.call("session.create"))["result"]["session"]["id"]
        job = (
            await h.call(
                "jobs.create", {"sessionId": sid, "kind": "heartbeat", "intervalSeconds": 600}
            )
        )["result"]["job"]
        assert job["kind"] == "heartbeat" and job["prompt"]

    async def test_validation(self, h):
        sid = (await h.call("session.create"))["result"]["session"]["id"]
        assert (await h.call("jobs.create", {"sessionId": sid, "kind": "bogus", "prompt": "x"}))[
            "error"
        ]["code"] == INVALID_PARAMS
        short = await h.call(
            "jobs.create",
            {"sessionId": sid, "kind": "interval", "prompt": "x", "intervalSeconds": 2},
        )
        assert short["error"]["code"] == INVALID_PARAMS
        assert (await h.call("jobs.setEnabled", {"id": str(uuid.uuid4()), "enabled": True}))[
            "error"
        ]["code"] == NOT_FOUND


# ─── _build_agent session-read boundary ────────────────────────────────────────


async def test_build_agent_records_session_read_failure(monkeypatch):
    """W7: a failed session read during job-agent build is diagnosed."""
    from unittest.mock import AsyncMock

    from machina.core import scheduler as scheduler_module
    from machina.core.metrics import metrics

    runner = scheduler_module.JobRunner()
    monkeypatch.setattr(
        "machina.core.session.session_manager.get_fresh",
        AsyncMock(side_effect=RuntimeError("session store unavailable")),
    )
    sentinel = object()
    monkeypatch.setattr(
        "machina.core.agent_factory.build_agent_for_session",
        AsyncMock(return_value=sentinel),
    )
    before = metrics.get_counter("diagnostic.failures")
    built = await runner._build_agent("harness", session_id=uuid.uuid4())
    assert built is sentinel
    assert metrics.get_counter("diagnostic.failures") == before + 1
