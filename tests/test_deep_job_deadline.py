"""Resistant job effects retain ownership without blocking other scheduled jobs."""

import asyncio
import uuid

import pytest

from ah.core.models import AgentResponse
from ah.core.scheduler import JobRunner
from tests.test_deep_scheduler import _job


class JobBoundary:
    def __init__(self, cause):
        self.cause = cause
        self.first = _job()
        self.other = _job()
        self.pending = [self.first, self.other]
        self.claims = {job.id: job.claim_token for job in self.pending}
        self.held_sessions = set()
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.release = asyncio.Event()
        self.finished_first = asyncio.Event()
        self.renewed_late = asyncio.Event()
        self.after_deadline = False
        self.first_renewals = 0
        self.finishes = []
        self.finish_attempts = []
        self.workers = []

    async def claim_due(self):
        return self.pending.pop(0) if self.pending else None

    async def renew_lease(self, job_id, claim_token):
        if job_id == self.first.id:
            self.first_renewals += 1
            if self.cause == "ownership_loss":
                self.claims[job_id] = uuid.uuid4()
                return False
            if self.after_deadline:
                self.renewed_late.set()
        return self.claims[job_id] == claim_token

    async def finish(self, job_id, *, error, claim_token, paused_for):
        job = self.first if job_id == self.first.id else self.other
        assert job.session_id not in self.held_sessions, "active effects lost their job claim"
        self.finish_attempts.append((job_id, claim_token, error))
        if job_id == self.first.id:
            self.finished_first.set()
        if self.claims[job_id] != claim_token:
            return False
        self.finishes.append((job_id, error))
        return True

    async def begin_turn(self, session_id):
        if session_id in self.held_sessions:
            return None
        self.held_sessions.add(session_id)
        return "owned-turn"

    async def end_turn(self, session_id, token):
        assert token == "owned-turn"
        self.held_sessions.remove(session_id)

    async def run(self, session_id, prompt, verbose):
        assert (prompt, verbose) == ("work", False)
        if session_id != self.first.session_id:
            return AgentResponse(content="other job finished")
        self.workers.append(asyncio.current_task())
        self.started.set()
        while not self.release.is_set():
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
        return AgentResponse(content="resistant job finished")


@pytest.fixture
def boundary(monkeypatch, request):
    state = JobBoundary(request.param)
    monkeypatch.setattr(
        "ah.core.scheduler.RUN_LEASE_SECONDS", 0.1 if request.param == "ownership_loss" else 0.01
    )
    monkeypatch.setattr("ah.core.turns.try_begin_turn", state.begin_turn)
    monkeypatch.setattr("ah.core.turns.end_turn", state.end_turn)
    return state


async def _finish_test(runner, boundary, run):
    boundary.release.set()
    await asyncio.gather(run, *boundary.workers, return_exceptions=True)
    await asyncio.sleep(0)
    await asyncio.gather(*getattr(runner, "_finalizers", ()), return_exceptions=True)


@pytest.mark.parametrize("boundary", ["timeout", "ownership_loss", "cancelled"], indirect=True)
async def test_job_deadline_quarantines_active_effects_without_releasing_the_claim(boundary):
    runner = JobRunner(store=boundary, agent_factory=lambda _name: boundary)
    run = asyncio.create_task(runner.run_due_once())
    await asyncio.wait_for(boundary.started.wait(), 1)
    if boundary.cause == "cancelled":
        run.cancel()
    try:
        done, _ = await asyncio.wait({run}, timeout=0.3)
        assert run in done, "resistant job execution exceeded its cancellation grace period"
        if boundary.cause == "cancelled":
            with pytest.raises(asyncio.CancelledError):
                await run
        else:
            assert await run
        await asyncio.wait_for(boundary.cancelled.wait(), 1)
        assert not boundary.finish_attempts
        assert boundary.first.session_id in boundary.held_sessions

        boundary.after_deadline = True
        if boundary.cause != "ownership_loss":
            await asyncio.wait_for(boundary.renewed_late.wait(), 1)
        renewals = boundary.first_renewals
        assert await runner.run_due_once()
        assert boundary.finishes == [(boundary.other.id, None)]
        assert boundary.first.session_id in boundary.held_sessions
        if boundary.cause == "ownership_loss":
            assert boundary.first_renewals == renewals

        boundary.release.set()
        await asyncio.wait_for(boundary.finished_first.wait(), 1)
        assert boundary.first.session_id not in boundary.held_sessions
        first_attempt = next(row for row in boundary.finish_attempts if row[0] == boundary.first.id)
        assert first_attempt[1] == boundary.first.claim_token
        if boundary.cause == "timeout":
            assert first_attempt[2] == "job timed out after 0.01s"
        elif boundary.cause == "ownership_loss":
            assert first_attempt[2] == "ownership lost; execution cancelled"
            assert boundary.finishes == [(boundary.other.id, None)]
        else:
            assert first_attempt[2] == "cancelled"
    finally:
        await _finish_test(runner, boundary, run)


@pytest.mark.parametrize("boundary", ["cancelled"], indirect=True)
async def test_runner_stop_does_not_wait_indefinitely_for_resistant_effects(boundary):
    boundary.pending = [boundary.first]
    runner = JobRunner(store=boundary, agent_factory=lambda _name: boundary)
    runner.start()
    await asyncio.wait_for(boundary.started.wait(), 1)
    stop = asyncio.create_task(runner.stop())
    try:
        done, _ = await asyncio.wait({stop}, timeout=0.3)
        assert stop in done, "runner stop waited indefinitely for resistant execution"
        await stop
        assert not boundary.finish_attempts
        assert boundary.first.session_id in boundary.held_sessions
        boundary.release.set()
        await asyncio.wait_for(boundary.finished_first.wait(), 1)
        assert boundary.finishes == [(boundary.first.id, "cancelled")]
    finally:
        await _finish_test(runner, boundary, stop)
