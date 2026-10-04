"""Phase 6a scheduler: JobStore, atomic claim, JobRunner, and gateway job methods."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

import pytest

from ah.core.models import AgentResponse
from ah.gateway.server import INVALID_PARAMS, NOT_FOUND
from tests.test_gateway import Harness

TEST_DSN = os.environ.get("AGENT_HARNESS_TEST_DATABASE_URL", "")
needs_db = pytest.mark.skipif(not TEST_DSN, reason="AGENT_HARNESS_TEST_DATABASE_URL not set")


@pytest.fixture
async def connected_db():
    from ah.db.connection import db

    await db.connect()
    yield
    await db.close()


async def a_session() -> uuid.UUID:
    from ah.core.session import session_manager

    return (await session_manager.create(title="scheduler test")).id


async def _claim_specific(job_id: uuid.UUID):
    """Claim due jobs until *job_id* is claimed (returning it) or none remain.

    The jobs table is shared across tests, so a plain claim_due() may return
    another test's job; finish those and keep going.
    """
    from ah.core.scheduler import job_store

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
    from ah.core.scheduler import job_store

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

    from ah.core.scheduler import JobStore

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


# ─── store + runner (real DB) ──────────────────────────────────────────────────


@needs_db
@pytest.mark.usefixtures("connected_db")
class TestJobStore:
    async def test_create_list_toggle_delete(self):
        from ah.core.scheduler import job_store

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
        from ah.core.scheduler import job_store

        sid = await a_session()
        job = await job_store.create(
            name="due", kind="interval", session_id=sid, prompt="go", interval_seconds=60
        )
        # Not due yet (next_run is a minute out).
        assert await _claim_specific(job.id) is None

        from ah.db.connection import db

        # Sort ahead of any older due jobs left by prior tests in the shared DB.
        await db.execute(
            "UPDATE jobs SET next_run_at = now() - interval '100 years' WHERE id = $1",
            job.id,
        )
        claimed = await _claim_specific(job.id)
        assert claimed is not None and claimed.status == "running"
        # Already running: our job cannot be claimed again.
        assert await _claim_specific(job.id) is None

        await job_store.finish(job.id)
        done = await job_store.get(job.id)
        # finish() reschedules from the real wall clock (now + interval).
        assert done.status == "idle" and done.run_count == 1
        assert done.next_run_at > datetime.now(UTC)
        await job_store.delete(job.id)

    async def test_disabled_jobs_are_not_claimed(self):
        from ah.core.scheduler import job_store

        sid = await a_session()
        job = await job_store.create(
            name="off", kind="interval", session_id=sid, prompt="go", interval_seconds=60
        )
        await job_store.set_enabled(job.id, False)
        from ah.db.connection import db

        # Force due; a disabled job must never be claimed.
        await db.execute("UPDATE jobs SET next_run_at = now() WHERE id = $1", job.id)
        assert await _claim_specific(job.id) is None
        await job_store.delete(job.id)

    async def test_expired_running_job_is_reclaimed(self):
        from ah.core.scheduler import job_store
        from ah.db.connection import db

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
        from ah.core.scheduler import JobRunner, job_store

        RecordingAgent.runs = []
        sid = await a_session()
        job = await job_store.create(
            name="interval",
            kind="interval",
            session_id=sid,
            prompt="do the thing",
            interval_seconds=30,
        )
        from ah.db.connection import db

        await db.execute("UPDATE jobs SET next_run_at = now() WHERE id = $1", job.id)

        await _drain_until_run(JobRunner(agent_factory=RecordingAgent), job.id)
        assert (sid, "do the thing") in RecordingAgent.runs
        after = await job_store.get(job.id)
        assert after.status == "idle" and after.run_count == 1
        await job_store.delete(job.id)

    async def test_heartbeat_uses_default_prompt(self):
        from ah.core.scheduler import DEFAULT_HEARTBEAT_PROMPT, JobRunner, job_store

        RecordingAgent.runs = []
        sid = await a_session()
        job = await job_store.create(
            name="hb",
            kind="heartbeat",
            session_id=sid,
            prompt=DEFAULT_HEARTBEAT_PROMPT,
            interval_seconds=30,
        )
        from ah.db.connection import db

        await db.execute("UPDATE jobs SET next_run_at = now() WHERE id = $1", job.id)
        await _drain_until_run(JobRunner(agent_factory=RecordingAgent), job.id)
        assert (sid, DEFAULT_HEARTBEAT_PROMPT) in RecordingAgent.runs
        await job_store.delete(job.id)

    async def test_runner_records_errors(self):
        from ah.core.scheduler import JobRunner, job_store

        sid = await a_session()
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

        await _drain_until_run(JobRunner(agent_factory=Exploding), job.id)
        after = await job_store.get(job.id)
        assert after.status == "error" and "kaboom" in after.last_error
        await job_store.delete(job.id)


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
