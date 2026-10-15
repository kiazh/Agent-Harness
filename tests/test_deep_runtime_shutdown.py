"""Runtime cleanup deadlines must survive services that suppress cancellation."""

import asyncio

import pytest

from machina.core.runtime import RuntimeServices
from machina.observability.audit import AuditPersistence


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
    monkeypatch.setattr("machina.observability.audit.audit_persistence.stop", boundary.stop_audit)
    monkeypatch.setattr("machina.db.connection.db.close", boundary.close_database)
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
    from machina.core import runtime as runtime_module

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
    monkeypatch.setattr("machina.db.connection.db", database)
    monkeypatch.setattr("machina.observability.audit.db", database)
    monkeypatch.setattr("machina.observability.audit.audit_persistence", writer)
    monkeypatch.setattr(runtime_module, "_detect_providers", lambda: {})
    monkeypatch.setattr(runtime_module, "_detect_sandbox", lambda: {})
    monkeypatch.setattr(runtime_module, "_detect_toggles", lambda: {})
    monkeypatch.setattr("machina.skills.registry.skill_registry.load_all", lambda: None)
    monkeypatch.setattr("machina.skills.registry.skill_registry.list_skills", lambda: [])
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


async def test_shutdown_service_failures_log_tracebacks(monkeypatch, caplog):
    """W7: shutdown service failures log the traceback, not just a type name."""
    import logging

    runtime = RuntimeServices(_started=True)

    class Manager:
        async def shutdown_all(self):
            raise RuntimeError("connector boom")

    class Connectors:
        manager = Manager()

    runtime._connector_service = Connectors()

    async def fail_close():
        raise RuntimeError("db boom")

    monkeypatch.setattr("machina.db.connection.db.close", fail_close)
    with caplog.at_level(logging.ERROR, logger="machina.core.runtime"):
        await runtime.shutdown(timeout=1)

    by_message = {record.getMessage(): record for record in caplog.records}
    # The safe-logging factory moves exc_info into a sanitized traceback
    # (class + frames): its presence is the exc_info/logger.exception pin.
    assert "RuntimeError" in by_message["connector shutdown failed"].exc_text
    assert "RuntimeError" in by_message["database shutdown failed"].exc_text
    assert not runtime._started


# ─── H19: shutdown releases the active RAG generation completely ────────────


async def test_shutdown_closes_every_component_of_the_active_rag_pipeline(monkeypatch):
    """H19: shutdown closed the active pipeline's embedder but left its
    reranker (HTTP/Cohere client) open and kept the closed generation cached;
    releasing the active pipeline must close all owned components and reset
    the recorded generation."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from machina.core import runtime as runtime_module
    from machina.observability import audit as audit_mod
    from machina.tools import rag as rag_tools

    embedder = SimpleNamespace(close=AsyncMock())
    reranker = SimpleNamespace(close=AsyncMock())
    monkeypatch.setattr(
        rag_tools,
        "_rag_pipeline",
        SimpleNamespace(_embedder=embedder, _reranker=reranker),
    )
    monkeypatch.setattr(rag_tools, "_rag_pipeline_identity", ((0,), (0,), (0.0, 0.0)))
    monkeypatch.setattr(runtime_module, "drain_background_tasks", AsyncMock())
    monkeypatch.setattr(audit_mod.audit_persistence, "stop", AsyncMock())
    monkeypatch.setattr("machina.rag.embedder.close_shared_embedder", AsyncMock())
    monkeypatch.setattr("machina.db.connection.db.close", AsyncMock())

    runtime = RuntimeServices(_started=True)
    await runtime.shutdown(timeout=1.0)

    embedder.close.assert_awaited_once()
    reranker.close.assert_awaited_once()
    assert rag_tools._rag_pipeline is None
    assert rag_tools._rag_pipeline_identity is None


async def test_close_active_rag_pipeline_is_one_lifecycle_seam(monkeypatch):
    """H19: one helper owns the full release — every owned component closes
    (isolated failures never mask the others) and a second call is a no-op."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from machina.tools import rag as rag_tools

    embedder = SimpleNamespace(close=AsyncMock(side_effect=RuntimeError("embedder boom")))
    reranker = SimpleNamespace(close=AsyncMock())
    monkeypatch.setattr(
        rag_tools,
        "_rag_pipeline",
        SimpleNamespace(_embedder=embedder, _reranker=reranker),
    )
    monkeypatch.setattr(rag_tools, "_rag_pipeline_identity", ((0,), (0,), (0.0, 0.0)))

    await rag_tools.close_active_rag_pipeline()
    embedder.close.assert_awaited_once()
    reranker.close.assert_awaited_once()
    assert rag_tools._rag_pipeline is None
    assert rag_tools._rag_pipeline_identity is None

    await rag_tools.close_active_rag_pipeline()
    embedder.close.assert_awaited_once()
    reranker.close.assert_awaited_once()
