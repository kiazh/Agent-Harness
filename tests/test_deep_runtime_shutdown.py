"""Runtime cleanup deadlines must survive services that suppress cancellation."""

import asyncio

import pytest

from ah.core.runtime import RuntimeServices
from ah.observability.audit import AuditPersistence


class CleanupBoundary:
    """Control only the audit/database cleanup operations owned by the runtime."""

    def __init__(self, resistant):
        self.resistant = resistant
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = []
        self.tasks = []

    async def cleanup(self, service):
        self.calls.append(service)
        if service != self.resistant:
            return
        self.tasks.append(asyncio.current_task())
        self.started.set()
        while not self.release.is_set():
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.set()

    async def stop_audit(self):
        await self.cleanup("audit")

    async def close_database(self):
        await self.cleanup("database")

    async def finish(self, shutdown):
        self.release.set()
        await asyncio.gather(shutdown, *self.tasks, return_exceptions=True)


@pytest.fixture
def cleanup_boundary(monkeypatch, request):
    boundary = CleanupBoundary(request.param)
    monkeypatch.setattr("ah.observability.audit.audit_persistence.stop", boundary.stop_audit)
    monkeypatch.setattr("ah.db.connection.db.close", boundary.close_database)
    return boundary


@pytest.mark.parametrize("cleanup_boundary", ["audit", "database"], indirect=True)
async def test_shutdown_finishes_when_a_service_suppresses_timeout_cancellation(cleanup_boundary):
    # wait_for joining a cancelled service indefinitely must fail this watchdog.
    runtime = RuntimeServices(_started=True)
    shutdown = asyncio.create_task(runtime.shutdown(timeout=0.01))
    await asyncio.wait_for(cleanup_boundary.started.wait(), timeout=1)
    try:
        done, _ = await asyncio.wait({shutdown}, timeout=0.2)
        assert shutdown in done, "runtime shutdown exceeded the service cleanup deadline"
        await shutdown
        assert cleanup_boundary.calls == ["audit", "database"]
        await asyncio.wait_for(cleanup_boundary.cancelled.wait(), timeout=0.2)
        assert not runtime._started
        assert not runtime._shutting_down
    finally:
        await cleanup_boundary.finish(shutdown)


@pytest.mark.parametrize("cleanup_boundary", ["audit", "database"], indirect=True)
async def test_shutdown_propagates_caller_cancellation_during_service_cleanup(cleanup_boundary):
    runtime = RuntimeServices(_started=True)
    shutdown = asyncio.create_task(runtime.shutdown(timeout=60))
    await asyncio.wait_for(cleanup_boundary.started.wait(), timeout=1)
    shutdown.cancel()
    try:
        done, _ = await asyncio.wait({shutdown}, timeout=0.2)
        assert shutdown in done, "caller cancellation waited for resistant service cleanup"
        with pytest.raises(asyncio.CancelledError):
            await shutdown
        await asyncio.wait_for(cleanup_boundary.cancelled.wait(), timeout=0.2)
        assert not runtime._shutting_down
    finally:
        await cleanup_boundary.finish(shutdown)


@pytest.mark.parametrize("phase", ["background", "audit"])
async def test_cancelled_shutdown_releases_audit_ownership_before_runtime_restart(
    monkeypatch, phase
):
    from ah.core import runtime as runtime_module

    started, cancelling, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Database:
        connected = False

        async def connect(self):
            self.connected = True

        async def close(self):
            self.connected = False

        async def execute(self, *_args):
            started.set()
            await release.wait()

    database = Database()
    writer = AuditPersistence()
    monkeypatch.setattr("ah.db.connection.db", database)
    monkeypatch.setattr("ah.observability.audit.db", database)
    monkeypatch.setattr("ah.observability.audit.audit_persistence", writer)
    monkeypatch.setattr(runtime_module, "_detect_providers", lambda: {})
    monkeypatch.setattr(runtime_module, "_detect_sandbox", lambda: {})
    monkeypatch.setattr(runtime_module, "_detect_toggles", lambda: {})
    monkeypatch.setattr("ah.skills.registry.skill_registry.load_all", lambda: None)
    monkeypatch.setattr("ah.skills.registry.skill_registry.list_skills", lambda: [])
    runtime = RuntimeServices()
    await runtime.startup()
    tasks = [writer._task]
    original_stop = writer.stop

    async def background():
        started.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelling.set()

    if phase == "background":
        tasks.append(runtime.track(asyncio.create_task(background())))
    else:
        writer.submit({"event": "pending-write"})

        async def stop():
            cancelling.set()
            await original_stop()

        monkeypatch.setattr(writer, "stop", stop)

    await asyncio.wait_for(started.wait(), timeout=1)
    shutdown = asyncio.create_task(runtime.shutdown(timeout=60))
    tasks.append(shutdown)
    await asyncio.wait_for(cancelling.wait(), timeout=1)
    shutdown.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(shutdown, timeout=0.5)
        # A cancelled attempt must not claim it stopped while retaining an
        # audit ownership reference that startup would acquire a second time.
        assert writer._owners == 0
        assert not database.connected

        await runtime.startup()
        tasks.append(writer._task)
        assert writer._owners == 1
        await runtime.shutdown(timeout=0.05)
        assert writer._owners == 0
        assert writer._task is None
        assert not database.connected
    finally:
        release.set()
        while writer._owners:
            await original_stop(timeout=0.05)
        await asyncio.gather(*tasks, return_exceptions=True)
