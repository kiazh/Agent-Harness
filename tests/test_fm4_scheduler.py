"""FM4 M3 + m8: JobRunner shutdown and timeout-race semantics.

RED before fix:
- stop() left quarantined effects' lease-renewal loops running forever.
- a job that finished exactly as the run timeout fired was recorded as
  "job timed out ..." instead of its clean outcome.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

from machina.core.scheduler import Job, JobRunner


def _job(name: str = "fm4-job") -> Job:
    return Job(
        id=uuid.uuid4(),
        name=name,
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
        claim_token=uuid.uuid4(),
    )


async def test_stop_cancels_lease_renewals_and_drains_quarantined(monkeypatch):
    """M3: stop() must not leave a quarantined effect renewing its lease."""
    monkeypatch.setattr("machina.core.scheduler.RUN_LEASE_SECONDS", 0.05)
    job = _job()
    started = asyncio.Event()
    cancel_seen = asyncio.Event()
    release = asyncio.Event()

    class Store:
        def __init__(self) -> None:
            self.claimed = False
            self.renewals = 0
            self.finishes: list[str | None] = []

        async def claim_due(self):
            if self.claimed:
                return None
            self.claimed = True
            return job

        async def renew_lease(self, job_id, claim_token=None):
            self.renewals += 1
            return True

        async def finish(self, job_id, *, error=None, claim_token=None, paused_for=None):
            self.finishes.append(error)
            return True

    store = Store()

    async def resistant_execute(claimed_job, ownership_lost=None):
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancel_seen.set()
            while not release.is_set():
                try:
                    await asyncio.sleep(0.02)
                except asyncio.CancelledError:
                    cancel_seen.set()

    runner = JobRunner(store=store, poll_seconds=0.01)
    runner._execute = resistant_execute
    runner.start()
    try:
        await asyncio.wait_for(started.wait(), 1)
        await asyncio.sleep(0.03)  # let the lease loop arm
        await asyncio.wait_for(runner.stop(), 2)
        assert cancel_seen.is_set(), "stop() must cancel the quarantined effect"
        renewals_at_stop = store.renewals
        await asyncio.sleep(0.25)
        assert store.renewals == renewals_at_stop, "lease renewals outlived stop()"
        leases = list(getattr(runner, "_leases", ()))
        assert all(task.done() for task in leases), "stop() must join lease renewals"
        release.set()
        deadline = asyncio.get_running_loop().time() + 1.0
        while not store.finishes and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        await asyncio.gather(*getattr(runner, "_finalizers", ()), return_exceptions=True)
        assert store.finishes == ["cancelled"]
    finally:
        release.set()
        for task in list(getattr(runner, "_quarantined", ())):
            task.cancel()
        await asyncio.gather(*list(getattr(runner, "_quarantined", ())), return_exceptions=True)
        await runner.stop()


class _WaitTimeoutShim:
    """Deterministic replay of the timeout race (m8).

    The first ``asyncio.wait`` call reports "timed out" (empty done set) while
    the effect actually completes during that same window. Everything else
    delegates to the real asyncio module.
    """

    def __init__(self, real, finished: asyncio.Event):
        self.real = real
        self.finished = finished
        self.calls = 0

    def __getattr__(self, name):
        return getattr(self.real, name)

    async def wait(self, fs, *, timeout=None, return_when=None):
        self.calls += 1
        if self.calls == 1:
            await self.finished.wait()
            await self.real.sleep(0)
            return set(), set(fs)
        if return_when is None:
            return await self.real.wait(fs, timeout=timeout)
        return await self.real.wait(fs, timeout=timeout, return_when=return_when)


async def test_timeout_race_with_clean_finish_is_not_error(monkeypatch):
    """m8: finishing as the timeout fires records the real outcome."""
    import machina.core.scheduler as sched

    job = _job("race-job")
    finished = asyncio.Event()

    class Store:
        def __init__(self) -> None:
            self.finishes: list[str | None] = []

        async def claim_due(self):
            return job

        async def renew_lease(self, job_id, claim_token=None):
            return True

        async def finish(self, job_id, *, error=None, claim_token=None, paused_for=None):
            self.finishes.append(error)
            return True

    store = Store()

    async def quick_execute(claimed_job, ownership_lost=None):
        await asyncio.sleep(0)
        finished.set()

    runner = JobRunner(store=store, poll_seconds=0.01)
    runner._execute = quick_execute
    shim = _WaitTimeoutShim(sched.asyncio, finished)
    monkeypatch.setattr(sched, "asyncio", shim)
    try:
        assert await runner.run_due_once() is True
    finally:
        monkeypatch.setattr(sched, "asyncio", shim.real)
    assert finished.is_set()
    assert store.finishes == [None], (
        "a job that finished as the timeout fired must not be recorded as timed out"
    )


async def test_real_timeout_still_records_timeout(monkeypatch):
    """m8 guard: a genuinely hung effect still records the timeout error."""
    import machina.core.scheduler as sched

    monkeypatch.setattr(sched, "RUN_LEASE_SECONDS", 0.05)
    job = _job("hung-job")

    class Store:
        def __init__(self) -> None:
            self.finishes: list[str | None] = []

        async def claim_due(self):
            return job

        async def renew_lease(self, job_id, claim_token=None):
            return True

        async def finish(self, job_id, *, error=None, claim_token=None, paused_for=None):
            self.finishes.append(error)
            return True

    store = Store()
    started = asyncio.Event()

    async def hang(claimed_job, ownership_lost=None):
        started.set()
        await asyncio.sleep(30)

    runner = JobRunner(store=store, poll_seconds=0.01)
    runner._execute = hang
    assert await runner.run_due_once() is True
    assert started.is_set()
    assert store.finishes and "timed out" in str(store.finishes[0])
