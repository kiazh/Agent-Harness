"""Task 9: authenticated native transport + approval binding, no replay."""

import asyncio
import os
import uuid
from unittest.mock import AsyncMock

import pytest

from machina.connectors.execution import connect_authorized, execute_call, prepare_connect
from machina.connectors.models import ConnectorConfig
from machina.connectors.oauth import OAuthCoordinator, OAuthOwner
from machina.connectors.oauth_models import OAuthBinding
from machina.connectors.oauth_store import OAuthCredentialStore
from machina.connectors.registry import ConnectorRegistry
from machina.connectors.service import ConnectorService
from machina.permissions.broker import clear_execution_context, set_approval_handler
from tests.support.mcp_oauth_fixture import OAuthFixture
from tests.test_connectors_oauth_flow import _drive_browser, _wait_state

pytestmark = pytest.mark.skipif(
    not os.environ.get("MACHINA_TEST_DATABASE_URL"),
    reason="MACHINA_TEST_DATABASE_URL not set",
)


def _service(fixture, *, tmp_path):
    registry = ConnectorRegistry()
    registry.add(
        ConnectorConfig(
            id="remote",
            transport="streamable_http",
            endpoint=fixture.endpoint,
            local_access=True,
            oauth={"client_mode": "auto"},
            discovery_timeout=10,
            connect_timeout=10,
            call_timeout=10,
        )
    )
    creds = OAuthCredentialStore(tmp_path / "oauth", key_provider=lambda: b"k" * 32)
    coordinator = OAuthCoordinator(credential_store=creds)
    service = ConnectorService(registry=registry, oauth_coordinator=coordinator)
    coordinator._service = service
    return service, coordinator


async def _login(service, coordinator, fixture, owner):
    preview = await coordinator.preview("remote", owner)
    started = await coordinator.start(
        "remote", owner, preview_id=preview["previewId"], digest=preview["digest"]
    )
    operation_id = started["operation"]["operationId"]
    await _wait_state(coordinator, "remote", owner, operation_id, "awaiting_browser", timeout=30)
    await _drive_browser(coordinator, owner, operation_id)
    await _wait_state(coordinator, "remote", owner, operation_id, "complete", timeout=30)


def _stamp(service, name, arguments, session_id, agent_id):
    """Stamp an approved active-session context for direct execute_call."""
    from machina.connectors.execution import call_spec
    from machina.permissions.broker import _execution_context
    from machina.permissions.policy import build_request

    req = build_request(
        **call_spec(service, {"name": name, "arguments": arguments}),
        tool="mcp_call",
        mode="ask",
        backend="host",
        session_id=session_id,
        agent_id=agent_id,
    )
    return _execution_context.set(
        {"session_id": session_id, "mode": "ask", "backend": "host", "digest": req.digest}
    )


def _reset(token):
    from machina.permissions.broker import _execution_context

    _execution_context.reset(token)


@pytest.mark.db
async def test_reviewed_connect_and_echo_round_trip(db_pool, monkeypatch, tmp_path):
    pytest.importorskip("mcp")
    from machina.core.session import session_manager
    from machina.db.connection import db

    monkeypatch.setattr(db, "_pool", db_pool.pool)
    from machina.core.runtime import runtime_services as _runtime_services

    monkeypatch.setattr(_runtime_services, "_connector_service", None)
    monkeypatch.setattr(
        "machina.core.execution_context.resolve_execution_context",
        AsyncMock(return_value=AsyncMock(mode="ask", backend="host", workspace=tmp_path)),
    )
    fixture = OAuthFixture()
    await fixture.start()
    try:
        service, coordinator = _service(fixture, tmp_path=tmp_path)
        monkeypatch.setattr(_runtime_services, "_connector_service", service)
        owner = f"agent-{uuid.uuid4().hex[:8]}"
        session = await session_manager.create(title="oauth-exec", agent_id=owner)
        try:
            oauth_owner = OAuthOwner(session_id=str(session.id), agent_id=owner)
            await _login(service, coordinator, fixture, oauth_owner)
            preview = await prepare_connect(
                service, "remote", session_id=str(session.id), agent_id=owner
            )
            connected = await connect_authorized(
                service,
                "remote",
                session_id=str(session.id),
                agent_id=owner,
                expected_digest=preview["digest"],
            )
            assert connected["state"] == "connected"
            tools = await service.tools_list("remote")
            names = {t["remote_name"]: t["name"] for t in tools["tools"]}
            assert "echo" in names
            described = await service.tools_describe(names["echo"])
            assert described["remote_name"] == "echo"
            # Agent pipeline: full-mode pause/resume sends bearer, returns echo.
            from machina.core.agent import BaseReActAgent
            from machina.core.models import LLMResponse
            from machina.permissions import store

            response = LLMResponse(
                content="",
                model="fixture",
                tool_calls=[
                    {
                        "id": "call1",
                        "function": {
                            "name": "mcp_call",
                            "arguments": {
                                "name": names["echo"],
                                "arguments": {"text": "hello-oauth"},
                            },
                        },
                    }
                ],
            )
            agent = BaseReActAgent(provider=AsyncMock(), agent_id=owner, allowed_tools=["mcp_call"])
            set_approval_handler(None)
            made = []
            async for _event in agent._execute_tool_calls_stream(response, [], made, session.id):
                pass
            assert made[0].get("needs_approval"), made
            await store.resolve_decision(
                made[0]["needs_approval"]["request_id"], "approved", "test-human"
            )

            async def approve(card):
                await store.resolve_decision(card["request_id"], "approved", "test-human")
                return "approved"

            set_approval_handler(approve, principal="test-human")
            made = []
            completed = [
                event
                async for event in agent._execute_tool_calls_stream(response, [], made, session.id)
            ]
            assert any("hello-oauth" in str(event.tool_result) for event in completed)
            assert not made[0].get("needs_approval"), made
            assert fixture.counters["tool_calls"] >= 1
        finally:
            set_approval_handler(None)
            clear_execution_context()
            await session_manager.delete(session.id)
            await service.manager.shutdown_all()
            await coordinator.shutdown()
    finally:
        await fixture.stop()


@pytest.mark.db
@pytest.mark.parametrize("auth_status", [401, 403])
async def test_mutation_auth_failure_never_replays(db_pool, monkeypatch, tmp_path, auth_status):
    """One counted effect, then transport 401/403: unknown, no replay."""
    pytest.importorskip("mcp")
    from machina.connectors.models import ConnectorToolError
    from machina.core.session import session_manager
    from machina.db.connection import db

    monkeypatch.setattr(db, "_pool", db_pool.pool)
    from machina.core.runtime import runtime_services as _runtime_services

    monkeypatch.setattr(_runtime_services, "_connector_service", None)
    monkeypatch.setattr(
        "machina.core.execution_context.resolve_execution_context",
        AsyncMock(return_value=AsyncMock(mode="ask", backend="host", workspace=tmp_path)),
    )
    fixture = OAuthFixture()
    fixture.switches["revoke_on_mutate"] = True
    fixture.switches["auth_failure_status"] = auth_status
    await fixture.start()
    try:
        service, coordinator = _service(fixture, tmp_path=tmp_path)
        monkeypatch.setattr(_runtime_services, "_connector_service", service)
        owner = f"agent-{uuid.uuid4().hex[:8]}"
        session = await session_manager.create(title="oauth-noreplay", agent_id=owner)
        try:
            oauth_owner = OAuthOwner(session_id=str(session.id), agent_id=owner)
            await _login(service, coordinator, fixture, oauth_owner)
            preview = await prepare_connect(
                service, "remote", session_id=str(session.id), agent_id=owner
            )
            await connect_authorized(
                service,
                "remote",
                session_id=str(session.id),
                agent_id=owner,
                expected_digest=preview["digest"],
            )
            tools = await service.tools_list("remote")
            names = {t["remote_name"]: t["name"] for t in tools["tools"]}
            connection = service.manager.connection("remote")
            first = await connection.call_tool("mutate", {"payload": "x"})
            assert first.ok, first
            assert fixture.counters["mutate_effects"] == 1
            # Next transport request fails with a real HTTP auth status.
            second = await connection.call_tool("mutate", {"payload": "y"})
            assert second.outcome_unknown, second
            # Exactly one side effect: no login/open/refresh/retry reinvoked it.
            assert fixture.counters["mutate_effects"] == 1
            assert fixture.counters["exchange"] == 1
            assert fixture.counters["refresh"] == 0
            # The managed call path refuses without dispatching again.
            token = _stamp(service, names["mutate"], {"payload": "z"}, str(session.id), owner)
            try:
                outcome = await execute_call(
                    service,
                    names["mutate"],
                    {"payload": "z"},
                    session_id=str(session.id),
                    agent_id=owner,
                )
                assert outcome.outcome_unknown, outcome
            except ConnectorToolError:
                pass
            assert fixture.counters["mutate_effects"] == 1
            _reset(token)
        finally:
            clear_execution_context()
            await session_manager.delete(session.id)
            await service.manager.shutdown_all()
            await coordinator.shutdown()
    finally:
        await fixture.stop()


@pytest.mark.db
async def test_mutation_timeout_is_unknown_without_retry(db_pool, monkeypatch, tmp_path):
    pytest.importorskip("mcp")
    from machina.core.session import session_manager
    from machina.db.connection import db

    monkeypatch.setattr(db, "_pool", db_pool.pool)
    from machina.core.runtime import runtime_services as _runtime_services

    monkeypatch.setattr(_runtime_services, "_connector_service", None)
    monkeypatch.setattr(
        "machina.core.execution_context.resolve_execution_context",
        AsyncMock(return_value=AsyncMock(mode="ask", backend="host", workspace=tmp_path)),
    )
    fixture = OAuthFixture()
    fixture.switches["timeout_after_effect"] = True
    await fixture.start()
    try:
        service, coordinator = _service(fixture, tmp_path=tmp_path)
        monkeypatch.setattr(_runtime_services, "_connector_service", service)
        owner = f"agent-{uuid.uuid4().hex[:8]}"
        session = await session_manager.create(title="oauth-timeout", agent_id=owner)
        try:
            oauth_owner = OAuthOwner(session_id=str(session.id), agent_id=owner)
            await _login(service, coordinator, fixture, oauth_owner)
            preview = await prepare_connect(
                service, "remote", session_id=str(session.id), agent_id=owner
            )
            await connect_authorized(
                service,
                "remote",
                session_id=str(session.id),
                agent_id=owner,
                expected_digest=preview["digest"],
            )
            connection = service.manager.connection("remote")
            outcome = await connection.call_tool("mutate", {"payload": "x"})
            assert outcome.outcome_unknown, outcome
            assert fixture.counters["mutate_effects"] == 1
            assert fixture.counters["exchange"] == 1
            assert fixture.counters["refresh"] == 0
        finally:
            await session_manager.delete(session.id)
            await service.manager.shutdown_all()
            await coordinator.shutdown()
    finally:
        await fixture.stop()


@pytest.mark.db
async def test_reauth_invalidates_pending_approval_and_predispatch_refuses(
    db_pool, monkeypatch, tmp_path
):
    pytest.importorskip("mcp")
    from machina.connectors.models import ConnectorToolError
    from machina.core.session import session_manager
    from machina.db.connection import db

    monkeypatch.setattr(db, "_pool", db_pool.pool)
    from machina.core.runtime import runtime_services as _runtime_services

    monkeypatch.setattr(_runtime_services, "_connector_service", None)
    monkeypatch.setattr(
        "machina.core.execution_context.resolve_execution_context",
        AsyncMock(return_value=AsyncMock(mode="ask", backend="host", workspace=tmp_path)),
    )
    fixture = OAuthFixture()
    await fixture.start()
    try:
        service, coordinator = _service(fixture, tmp_path=tmp_path)
        monkeypatch.setattr(_runtime_services, "_connector_service", service)
        owner = f"agent-{uuid.uuid4().hex[:8]}"
        session = await session_manager.create(title="oauth-reauth", agent_id=owner)
        try:
            oauth_owner = OAuthOwner(session_id=str(session.id), agent_id=owner)
            await _login(service, coordinator, fixture, oauth_owner)
            preview = await prepare_connect(
                service, "remote", session_id=str(session.id), agent_id=owner
            )
            await connect_authorized(
                service,
                "remote",
                session_id=str(session.id),
                agent_id=owner,
                expected_digest=preview["digest"],
            )
            tools = await service.tools_list("remote")
            names = {t["remote_name"]: t["name"] for t in tools["tools"]}
            # Expire with no refresh token: pre-dispatch refuses, counter zero.
            identity = coordinator.auth_identity("remote")
            binding = OAuthBinding(
                connector_id="remote",
                credential_key=identity["credentialKey"],
                resource=identity["resource"],
                issuer=identity["issuer"],
                client_id=identity["clientId"],
            )
            record = await coordinator._creds.load(binding)
            record.expires_at_utc = "2020-01-01T00:00:00+00:00"
            record.refresh_token = ""
            await coordinator._creds.save(record, expected_generation=record.generation)
            before = fixture.counters["tool_calls"]
            token = _stamp(service, names["echo"], {"text": "never"}, str(session.id), owner)
            try:
                with pytest.raises(ConnectorToolError):
                    await execute_call(
                        service,
                        names["echo"],
                        {"text": "never"},
                        session_id=str(session.id),
                        agent_id=owner,
                    )
            finally:
                _reset(token)
            assert fixture.counters["tool_calls"] == before
            # Forget, then re-login: old grant cannot dispatch anymore.
            # (Forget also retires the catalog, so even lookup refuses.)
            await coordinator.forget(
                "remote", oauth_owner, expected_generation=identity["generation"], revoke=False
            )
            with pytest.raises(ConnectorToolError):
                token = _stamp(service, names["echo"], {"text": "never"}, str(session.id), owner)
                try:
                    await execute_call(
                        service,
                        names["echo"],
                        {"text": "never"},
                        session_id=str(session.id),
                        agent_id=owner,
                    )
                finally:
                    _reset(token)
            assert fixture.counters["tool_calls"] == before
        finally:
            clear_execution_context()
            await session_manager.delete(session.id)
            await service.manager.shutdown_all()
            await coordinator.shutdown()
    finally:
        await fixture.stop()


@pytest.mark.db
async def test_adapter_yields_once_and_repeated_cycles_release(db_pool, monkeypatch, tmp_path):
    pytest.importorskip("mcp")
    from machina.core.session import session_manager
    from machina.db.connection import db

    monkeypatch.setattr(db, "_pool", db_pool.pool)
    from machina.core.runtime import runtime_services as _runtime_services

    monkeypatch.setattr(_runtime_services, "_connector_service", None)
    monkeypatch.setattr(
        "machina.core.execution_context.resolve_execution_context",
        AsyncMock(return_value=AsyncMock(mode="ask", backend="host", workspace=tmp_path)),
    )
    fixture = OAuthFixture()
    await fixture.start()
    try:
        service, coordinator = _service(fixture, tmp_path=tmp_path)
        monkeypatch.setattr(_runtime_services, "_connector_service", service)
        owner = f"agent-{uuid.uuid4().hex[:8]}"
        session = await session_manager.create(title="oauth-cycles", agent_id=owner)
        try:
            oauth_owner = OAuthOwner(session_id=str(session.id), agent_id=owner)
            await _login(service, coordinator, fixture, oauth_owner)
            for _ in range(3):
                preview = await prepare_connect(
                    service, "remote", session_id=str(session.id), agent_id=owner
                )
                connected = await connect_authorized(
                    service,
                    "remote",
                    session_id=str(session.id),
                    agent_id=owner,
                    expected_digest=preview["digest"],
                )
                assert connected["state"] == "connected"
                tools = await service.tools_list("remote")
                assert tools["tools"]
                assert await service.manager.disconnect("remote") is True
            tasks = [t for t in asyncio.all_tasks() if "mcp-owner" in (t.get_name() or "")]
            assert tasks == [], [t.get_name() for t in tasks]
        finally:
            await session_manager.delete(session.id)
            await service.manager.shutdown_all()
            await coordinator.shutdown()
    finally:
        await fixture.stop()
