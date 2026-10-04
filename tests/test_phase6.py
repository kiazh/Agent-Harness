"""Production API, plugin, scheduler, and observability integration."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from ah.api.app import create_app
from ah.core.cron import next_cron_time
from ah.core.metrics import MetricsCollector
from ah.observability.audit import AuditPersistence
from ah.observability.metrics import prometheus_text
from ah.plugins.registry import PluginRegistry
from ah.security.secrets import get_secret


@pytest.mark.asyncio
async def test_database_passes_full_dsn_and_does_not_replace_a_live_pool(monkeypatch):
    from ah.db.connection import Database

    dsn = "postgresql://user:p%40ss@localhost/example?sslmode=require"
    pool = AsyncMock()
    create_pool = AsyncMock(return_value=pool)
    monkeypatch.setattr("ah.db.connection.asyncpg.create_pool", create_pool)
    database = Database(dsn=dsn)
    await database.connect()
    await database.connect()
    create_pool.assert_awaited_once()
    assert create_pool.await_args.kwargs["dsn"] == dsn
    await database.close()


def test_cron_next_time_and_validation():
    start = datetime(2026, 10, 3, 10, 14, 30, tzinfo=UTC)
    assert next_cron_time("*/15 10 * * *", start) == datetime(2026, 10, 3, 10, 15, tzinfo=UTC)
    assert next_cron_time("0 9 * * 0", start) == datetime(2026, 10, 4, 9, 0, tzinfo=UTC)
    assert next_cron_time("0 9 * * 7", start) == datetime(2026, 10, 4, 9, 0, tzinfo=UTC)
    assert next_cron_time("5/10 10 * * *", start) == datetime(2026, 10, 3, 10, 15, tzinfo=UTC)
    assert next_cron_time("0 9 1-31 * 1", start) == datetime(2026, 10, 4, 9, 0, tzinfo=UTC)
    assert next_cron_time("0 0 29 2 *", start) == datetime(2028, 2, 29, 0, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="five fields"):
        next_cron_time("* * *", start)
    with pytest.raises(ValueError, match="out of range"):
        next_cron_time("60 * * * *", start)
    with pytest.raises(ValueError, match="no occurrence"):
        next_cron_time("0 0 31 2 *", start)


def test_prometheus_export_uses_cumulative_totals():
    collector = MetricsCollector()
    collector.increment_counter("agent.run.calls", 2)
    collector.record_latency("agent.run", 20)
    collector.record_tokens("private-session-a", 4, 3)
    collector.record_tokens("private-session-b", 2, 1)
    output = prometheus_text(collector)
    assert 'ah_operations_total{operation="agent.run.calls"} 2' in output
    assert 'ah_duration_milliseconds_sum{operation="agent.run"} 20.0' in output
    assert 'ah_tokens_total{kind="total_tokens"} 10' in output
    assert "private-session-a" not in output and "private-session-b" not in output


def test_audit_sanitizes_sensitive_field_names():
    from ah.core.provider import _sanitize_value

    payload = {
        "apiKey": "unusual-secret-value",
        "nested": {"Authorization": "plain-value"},
        "total_tokens": 7,
    }
    assert _sanitize_value(payload) == {
        "apiKey": "[REDACTED]",
        "nested": {"Authorization": "[REDACTED]"},
        "total_tokens": 7,
    }


def test_mounted_secret_and_vault_lookup(monkeypatch, tmp_path):
    name = "PHASE6_SECRET_TEST"
    file_path = tmp_path / "api_key"
    file_path.write_text("mounted-key\n", encoding="utf-8")
    monkeypatch.setenv(f"{name}_FILE", str(file_path))
    assert get_secret(name) == "mounted-key"
    monkeypatch.delenv(f"{name}_FILE")
    monkeypatch.setenv("AGENT_HARNESS_SECRET_BACKEND", "vault")
    monkeypatch.setenv("AGENT_HARNESS_VAULT_ADDR", "https://vault.example")
    monkeypatch.setenv("AGENT_HARNESS_VAULT_PATH", "secret/data/phase6")
    monkeypatch.setenv("AGENT_HARNESS_VAULT_TOKEN", "token")

    from ah.security import secrets

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["timeout"] == 5.0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def get(self, url, headers):
            assert url == "https://vault.example/v1/secret/data/phase6"
            assert headers["X-Vault-Token"] == "token"
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {
                "data": {"data": {name: "vault-key"}}
            })

    monkeypatch.setattr(secrets.httpx, "Client", Client)
    assert get_secret(name) == "vault-key"


def test_aws_secret_lookup_uses_named_field(monkeypatch):
    name = "PHASE6_AWS_SECRET_TEST"
    monkeypatch.setenv("AGENT_HARNESS_SECRET_BACKEND", "aws")
    monkeypatch.setenv("AGENT_HARNESS_AWS_SECRET_ID", "phase6-id")

    def client(service):
        assert service == "secretsmanager"
        return SimpleNamespace(get_secret_value=lambda **kwargs: {
            "SecretString": '{"PHASE6_AWS_SECRET_TEST":"aws-key"}'
        })

    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(client=client))
    assert get_secret(name) == "aws-key"


@pytest.mark.asyncio
async def test_plugin_dispatch_isolates_failures():
    registry = PluginRegistry()
    called = []

    class Broken:
        name = "broken"

        async def on_tool_result(self, name, result):
            raise RuntimeError("failed")

    class Working:
        name = "working"

        def on_tool_result(self, name, result):
            called.append((name, result))

    registry.register(Broken())
    registry.register(Working())
    await registry.dispatch("on_tool_result", "read_file", "ok")
    assert called == [("read_file", "ok")]
    registry.unregister("working")
    assert registry.list() == ["broken"]


@pytest.mark.asyncio
async def test_slow_plugin_does_not_block_other_hooks():
    registry = PluginRegistry(timeout_seconds=0.01)
    seen = []

    class Slow:
        name = "slow"

        async def pre_agent_run(self, _session, _message):
            await asyncio.sleep(1)

    class Fast:
        name = "fast"

        async def pre_agent_run(self, _session, message):
            seen.append(message)

    registry.register(Slow())
    registry.register(Fast())
    await registry.dispatch("pre_agent_run", object(), "hello")
    assert seen == ["hello"]


@pytest.mark.asyncio
async def test_agent_lifecycle_and_tool_hooks(monkeypatch):
    from ah.core import agent as agent_module
    from ah.core.agent import ReActAgent
    from ah.core.models import LLMResponse
    from ah.plugins.registry import plugin_registry

    session_id = uuid.uuid4()
    session = SimpleNamespace(id=session_id, context_budget=8000, goal=None)
    seen = []

    class Recorder:
        name = "phase6-recorder"

        async def pre_agent_run(self, _session, message):
            seen.append(("pre", message))

        async def on_tool_call(self, name, _args):
            seen.append(("call", name))

        async def on_tool_result(self, name, result):
            seen.append(("result", name, result))

        async def post_agent_run(self, _session, response):
            seen.append(("post", response.content))

    calls = [
        LLMResponse(content="", model="test", tool_calls=[{
            "id": "one", "function": {"name": "list_agents", "arguments": "{}"}
        }]),
        LLMResponse(content="finished", model="test"),
    ]

    class Provider:
        async def complete(self, **_kwargs):
            return calls.pop(0)

    monkeypatch.setattr(agent_module.session_manager, "get", AsyncMock(return_value=session))
    monkeypatch.setattr(agent_module.session_manager, "update_activity", AsyncMock())
    monkeypatch.setattr(agent_module.context_manager, "add_chunk", AsyncMock())
    monkeypatch.setattr(agent_module.context_manager, "get_recent_context", AsyncMock(return_value=[]))
    monkeypatch.setattr(agent_module.registry, "execute", AsyncMock(return_value="agents listed"))
    agent = ReActAgent(provider=Provider(), max_iterations=2)
    agent.memory_retriever.retrieve = AsyncMock(return_value=[])
    plugin_registry.register(Recorder())
    try:
        response = await agent.run(session_id, "delegate work", verbose=False)
    finally:
        plugin_registry.unregister("phase6-recorder")
    assert response.content == "finished"
    assert seen == [
        ("pre", "delegate work"),
        ("call", "list_agents"),
        ("result", "list_agents", "agents listed"),
        ("post", "finished"),
    ]


@pytest.mark.asyncio
async def test_api_lifespan_starts_and_stops_scheduler(monkeypatch):
    from ah.api.app import lifespan
    from ah.core import scheduler
    from ah.db.connection import db
    from ah.observability.audit import audit_persistence

    events = []

    async def connect():
        events.append("db-connect")

    async def close():
        events.append("db-close")

    class Runner:
        def start(self):
            events.append("runner-start")

        async def stop(self):
            events.append("runner-stop")

    monkeypatch.setattr(db, "connect", connect)
    monkeypatch.setattr(db, "close", close)
    monkeypatch.setattr(scheduler, "JobRunner", Runner)
    monkeypatch.setattr(audit_persistence, "start", lambda: events.append("audit-start"))
    monkeypatch.setattr(audit_persistence, "stop", AsyncMock(side_effect=lambda: events.append("audit-stop")))
    async with lifespan(create_app()):
        assert events == ["db-connect", "audit-start", "runner-start"]
    assert events[-3:] == ["runner-stop", "audit-stop", "db-close"]


def test_plugin_loader_requires_named_entry_points(monkeypatch):
    from ah.plugins import loader

    called = []

    class Entry:
        name = "sample"

        def load(self):
            called.append("loaded")
            return SimpleNamespace(name="sample")

    monkeypatch.setattr(loader, "entry_points", lambda **_kwargs: [Entry()])
    registry = PluginRegistry()
    assert loader.load_plugins([], registry) == []
    assert called == []
    assert loader.load_plugins(["sample"], registry) == ["sample"]
    assert registry.list() == ["sample"]


@pytest.mark.asyncio
async def test_audit_events_are_flushed_before_shutdown(monkeypatch):
    from ah.observability import audit

    execute = AsyncMock()
    monkeypatch.setattr(audit.db, "execute", execute)
    writer = AuditPersistence()
    writer.start()
    writer.submit({"event": "agent_run_start", "session_id": "test"})
    await writer.stop()
    assert execute.await_count == 1
    assert "agent_run_start" in execute.await_args.args


@pytest.mark.asyncio
async def test_versioned_api_metrics_rate_limit_and_health(monkeypatch):
    monkeypatch.setenv("AGENT_HARNESS_API_KEY", "phase6-test")
    monkeypatch.setenv("AGENT_HARNESS_HTTP_RATE_LIMIT", "2")
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/health")).status_code == 200
        assert (await client.get("/ready")).status_code == 503
        assert (await client.get("/metrics")).status_code == 401
        headers = {"Authorization": "Bearer phase6-test"}
        first = await client.get("/metrics", headers=headers)
        assert first.status_code == 200
        assert "ah_operations_total" in first.text
        assert (await client.get("/api/v1/sessions", headers=headers)).status_code == 429
        assert (await client.get("/health")).status_code == 200


@pytest.mark.asyncio
async def test_readiness_requires_initialized_schema(monkeypatch):
    from ah.db.connection import db

    monkeypatch.setattr(type(db), "connected", property(lambda _self: True))
    fetchval = AsyncMock(return_value=False)
    monkeypatch.setattr(db, "fetchval", fetchval)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/ready")).status_code == 503
        fetchval.return_value = True
        assert (await client.get("/ready")).json() == {"status": "ready"}


@pytest.mark.asyncio
async def test_versioned_memory_and_document_routes(monkeypatch):
    from ah.core.session import session_manager
    from ah.memory.store import memory_store
    from ah.tools import rag

    monkeypatch.setenv("AGENT_HARNESS_API_KEY", "phase6-test")
    monkeypatch.setenv("AGENT_HARNESS_HTTP_RATE_LIMIT", "100")
    session_id = uuid.uuid4()
    monkeypatch.setattr(session_manager, "get", AsyncMock(return_value=SimpleNamespace(id=session_id)))
    memory = SimpleNamespace(id=uuid.uuid4(), content="saved", category="fact")
    add_memory = AsyncMock(return_value=memory)
    monkeypatch.setattr(memory_store, "add", add_memory)
    pipeline = SimpleNamespace(index_document=AsyncMock(return_value=[object(), object()]))
    monkeypatch.setattr(rag, "get_rag_pipeline", AsyncMock(return_value=pipeline))
    app = create_app()
    headers = {"Authorization": "Bearer phase6-test"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post(
            "/api/v1/memory", headers=headers,
            json={"content": "saved", "sessionId": str(session_id)},
        )
        assert created.status_code == 200
        assert add_memory.await_args.kwargs["session_id"] == session_id
        indexed = await client.post(
            "/api/v1/documents", headers=headers,
            json={"sessionId": str(session_id), "source": "notes", "content": "hello"},
        )
        assert indexed.status_code == 200
        assert indexed.json()["chunksIndexed"] == 2
        assert pipeline.index_document.await_args.kwargs["session_id"] == session_id


@pytest.mark.asyncio
async def test_versioned_session_update_and_delete(monkeypatch):
    from ah.core.session import session_manager

    monkeypatch.setenv("AGENT_HARNESS_API_KEY", "phase6-test")
    monkeypatch.setenv("AGENT_HARNESS_HTTP_RATE_LIMIT", "100")
    session_id = uuid.uuid4()
    session = SimpleNamespace(id=session_id, title="Renamed", status="active", model="m",
                              provider="p", goal=None, agent_id="harness", context_budget=8000,
                              created_at=None, last_activity=None)
    monkeypatch.setattr(session_manager, "get", AsyncMock(return_value=session))
    set_title = AsyncMock()
    monkeypatch.setattr(session_manager, "set_title", set_title)
    monkeypatch.setattr(session_manager, "delete", AsyncMock(return_value=True))
    app = create_app()
    headers = {"Authorization": "Bearer phase6-test"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        updated = await client.patch(
            f"/api/v1/sessions/{session_id}", headers=headers, json={"title": "Renamed"}
        )
        assert updated.status_code == 200
        set_title.assert_awaited_once_with(session_id, "Renamed")
        deleted = await client.delete(f"/api/v1/sessions/{session_id}", headers=headers)
        assert deleted.json() == {"deleted": True}


@pytest.mark.skipif(
    not os.environ.get("AGENT_HARNESS_TEST_DATABASE_URL"), reason="test database not configured"
)
@pytest.mark.asyncio
async def test_cron_job_persists_and_reschedules():
    from ah.core.scheduler import job_store
    from ah.core.session import session_manager
    from ah.db.connection import db

    await db.connect()
    try:
        session = await session_manager.create(title="cron phase 6")
        job = await job_store.create(
            name="cron phase 6", kind="cron", session_id=session.id,
            prompt="continue", interval_seconds=300, cron_expression="*/5 * * * *",
        )
        assert job.kind == "cron" and job.cron_expression == "*/5 * * * *"
        assert job.next_run_at > datetime.now(UTC)
        await job_store.finish(job.id)
        rescheduled = await job_store.get(job.id)
        assert rescheduled.run_count == 1
        assert rescheduled.next_run_at > datetime.now(UTC)
        await job_store.delete(job.id)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_docker_terminal_isolated_command_and_path_check(monkeypatch, tmp_path):
    from ah.tools import terminal as terminal_module
    from ah.tools.terminal import _validate_workdir

    workdir = tmp_path / "workspace"
    workdir.mkdir()
    sibling = tmp_path / "workspace-other"
    sibling.mkdir()
    monkeypatch.setattr(terminal_module, "ALLOWED_WORKDIR_PREFIXES", (str(workdir),))
    with pytest.raises(ValueError, match="not within allowed"):
        _validate_workdir(str(sibling))

    monkeypatch.setenv("AGENT_HARNESS_TERMINAL_SANDBOX", "docker")
    seen = []

    def fake_run(args, **kwargs):
        seen.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="ok", stderr="")

    monkeypatch.setattr(terminal_module.subprocess, "run", fake_run)
    assert await terminal_module.terminal("git status", workdir=str(workdir)) == "ok"
    args = seen[0]
    assert args[:2] == ["docker", "run"]
    assert "--network" in args and "none" in args
    assert "--read-only" in args and "--cap-drop" in args
    assert args[-2:] == ["git", "status"]
