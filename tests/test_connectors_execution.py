"""MCP calls use the real native tool pipeline and exact approval boundary."""

import sys
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from machina.connectors.models import ConnectorConfig, DiscoveredTool
from machina.connectors.service import ConnectorService
from machina.permissions.policy import build_request, decide


def test_native_mcp_tools_are_lazy():
    """Native MCP tools register lazily; the cache is gone entirely (R4-096),
    so "uncached" is structural — every call executes fresh (pinned by
    tests/test_bugfixes_tool_cache_redos.py)."""
    from machina.tools.base import registry

    assert {"mcp_list", "mcp_describe", "mcp_call"} <= set(registry.list_tools())
    # Agent-callable management tools declare mutating effects (always
    # review-gated, never cached); see tests/test_mcp_manage_tool.py.
    assert {"mcp_connect", "mcp_disconnect"} <= set(registry.list_tools())


def test_full_grant_allows_mcp_calls_without_approval():
    from machina.connectors.execution import call_spec

    service = ConnectorService()
    service.registry.add(ConnectorConfig(id="fixture", command=sys.executable))
    service.registry.set_tools(
        "fixture",
        [
            DiscoveredTool(
                "fixture",
                "",
                "echo",
                schema={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            )
        ],
    )
    args = {"name": "fixture__echo", "arguments": {"text": "hello"}}
    req = build_request(**call_spec(service, args), tool="mcp_call", mode="full")
    # Full mode is explicit no-approval authority: a live session grant
    # allows the call without an approval card.
    assert (
        decide(
            req, [{"mode": "full", "capability": "session", "digest": "full-mode-session"}]
        ).verdict
        == "allowed"
    )
    service.registry.update("fixture", argv=["--changed"])
    service.registry.set_tools(
        "fixture",
        [
            DiscoveredTool(
                "fixture",
                "",
                "echo",
                schema={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            )
        ],
    )
    changed = build_request(**call_spec(service, args), tool="mcp_call", mode="full")
    assert req.digest != changed.digest


@pytest.mark.parametrize(
    "remote, arguments, marker, expected",
    [
        ("echo", {"text": "hello"}, "hello", "completed"),
        ("slow", {"seconds": 2}, "outcome_unknown", "outcome_unknown"),
        ("boom", {}, "boom", "failed"),
        ("big", {"size": 10000}, "truncated", "completed"),
        ("ask_user", {"question": "do you approve?"}, "Elicitation not supported", "failed"),
    ],
)
async def test_sdk_remote_call_runs_through_agent_approval_pipeline(
    db_pool, monkeypatch, tmp_path, remote, arguments, marker, expected
):
    pytest.importorskip("mcp")
    from machina.connectors.execution import connect_authorized, prepare_connect
    from machina.core.agent import BaseReActAgent
    from machina.core.models import LLMResponse
    from machina.core.runtime import runtime_services
    from machina.core.session import session_manager
    from machina.db.connection import db
    from machina.permissions import store
    from machina.permissions.broker import clear_execution_context, set_approval_handler

    monkeypatch.setattr(db, "_pool", db_pool.pool)
    service = ConnectorService()
    monkeypatch.setattr(runtime_services, "_connector_service", service)
    owner = "mcp-test-" + uuid.uuid4().hex
    session = await session_manager.create(agent_id=owner)
    fixture = Path(__file__).parent / "support" / "mcp_fixture_server.py"
    service.registry.add(
        ConnectorConfig(
            id="fixture",
            command=sys.executable,
            argv=[str(fixture.resolve()), "--name", uuid.uuid4().hex],
            cwd=str(tmp_path),
            max_retries=0,
            call_timeout=0.1 if remote == "slow" else 5,
            result_limit=256,
        )
    )
    preview = await prepare_connect(service, "fixture", session_id=str(session.id), agent_id=owner)
    await connect_authorized(
        service,
        "fixture",
        session_id=str(session.id),
        agent_id=owner,
        expected_digest=preview["digest"],
    )
    try:
        tools = await service.tools_list("fixture")
        name = next(t["name"] for t in tools["tools"] if t["remote_name"] == remote)
        response = LLMResponse(
            content="",
            model="fixture",
            tool_calls=[
                {
                    "id": "call1",
                    "function": {
                        "name": "mcp_call",
                        "arguments": {"name": name, "arguments": arguments},
                    },
                }
            ],
        )
        agent = BaseReActAgent(provider=AsyncMock(), agent_id=owner, allowed_tools=["mcp_call"])
        set_approval_handler(None)
        made = []
        events = [
            event
            async for event in agent._execute_tool_calls_stream(response, [], made, session.id)
        ]
        assert made[0].get("needs_approval"), made
        assert not any("hello" == str(getattr(e, "tool_result", None)) for e in events)

        async def approve(card):
            await store.resolve_decision(card["request_id"], "approved", "test-human")
            return "approved"

        # Resume the already-created exact pending request through durable broker.
        await store.resolve_decision(
            made[0]["needs_approval"]["request_id"], "approved", "test-human"
        )
        set_approval_handler(approve, principal="test-human")
        made = []
        completed = [
            event
            async for event in agent._execute_tool_calls_stream(response, [], made, session.id)
        ]
        assert any(marker in event.tool_result for event in completed), (marker, made)
        assert made[0]["outcome"] == expected, made
        if expected == "outcome_unknown":
            assert any(event.tool_outcome == "outcome_unknown" for event in completed)
        assert not made[0].get("needs_approval"), made
    finally:
        set_approval_handler(None)
        clear_execution_context()
        await service.manager.shutdown_all()


async def test_connect_launches_reviewed_absolute_executable_and_effective_cwd(
    monkeypatch, tmp_path
):
    from machina.connectors.execution import connect_authorized, prepare_connect
    from machina.connectors.models import ConnectionState
    from machina.core.execution_context import ExecutionContext
    from machina.permissions import store
    from machina.permissions.broker import clear_execution_context

    monkeypatch.setattr(
        "machina.core.execution_context.resolve_execution_context",
        AsyncMock(return_value=ExecutionContext("ask", "host", tmp_path)),
    )
    monkeypatch.setattr(store, "_db", AsyncMock(return_value=None))
    launched = []

    class Fake:
        def __init__(self, config):
            self.config = config
            self.state = ConnectionState.DISCONNECTED
            self.server_name = self.server_version = self.protocol_version = self.last_error = ""

        async def connect(self, secrets):
            launched.append((self.config.command, self.config.cwd))
            self.state = ConnectionState.CONNECTED

        async def list_tools(self):
            return [], False

        async def disconnect(self):
            self.state = ConnectionState.DISCONNECTED

    from machina.connectors.manager import ConnectorManager

    service = ConnectorService()
    service.manager = ConnectorManager(service.registry, client_factory=Fake)
    service.registry.add(ConnectorConfig(id="fixture", command="python3", max_retries=0))
    preview = await prepare_connect(service, "fixture", "session", "harness")
    try:
        await connect_authorized(service, "fixture", "session", "harness", preview["digest"])
        assert launched == [(preview["proposal"]["argv"][0], str(tmp_path))]
    finally:
        await service.manager.shutdown_all()
        clear_execution_context()


async def test_http_transport_uses_explicit_client_without_redirects(monkeypatch):
    pytest.importorskip("mcp")
    from contextlib import AsyncExitStack, asynccontextmanager

    from machina.connectors.mcp_client import MCPConnection

    seen = []

    @asynccontextmanager
    async def transport(url, **kwargs):
        client = kwargs.get("http_client")
        assert client is not None, "SDK default client can follow unapproved redirects"
        assert not client.follow_redirects
        seen.append(client)
        yield (object(), object())

    monkeypatch.setattr("mcp.client.streamable_http.streamable_http_client", transport)
    connection = MCPConnection(
        ConnectorConfig(
            id="http",
            transport="streamable_http",
            endpoint="http://127.0.0.1:8999/mcp",
            local_access=True,
        )
    )
    connection._new_session = AsyncMock()
    async with AsyncExitStack() as stack:
        connection._stack = stack
        await connection._connect_http({})
    assert seen[0].is_closed


@pytest.mark.parametrize("change", ["args", "schema", "config"])
async def test_changed_call_refuses_dispatch_and_has_no_unbrokered_fallback(monkeypatch, change):
    from types import SimpleNamespace

    from machina.connectors.execution import call_spec, execute_call
    from machina.connectors.models import ConnectionState, ConnectorToolError
    from machina.permissions.broker import _execution_context

    service = ConnectorService()
    service.registry.add(ConnectorConfig(id="fixture", command=sys.executable))
    schema = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
    service.registry.set_tools("fixture", [DiscoveredTool("fixture", "", "echo", schema=schema)])
    client = SimpleNamespace(state=ConnectionState.CONNECTED, call_tool=AsyncMock())
    monkeypatch.setattr(service.manager, "connection", lambda _: client)
    args = {"name": "fixture__echo", "arguments": {"text": "original"}}
    req = build_request(
        **call_spec(service, args), tool="mcp_call", mode="ask", session_id="session"
    )
    token = _execution_context.set(
        {"session_id": "session", "mode": "ask", "backend": "host", "digest": req.digest}
    )
    try:
        if change == "args":
            args["arguments"]["text"] = "changed"
        elif change == "schema":
            service.registry.set_tools(
                "fixture",
                [
                    DiscoveredTool(
                        "fixture", "", "echo", schema={**schema, "additionalProperties": False}
                    )
                ],
            )
        else:
            service.registry.update("fixture", result_limit=100)
            service.registry.set_tools(
                "fixture", [DiscoveredTool("fixture", "", "echo", schema=schema)]
            )
        with pytest.raises(ConnectorToolError, match="changed after approval"):
            await execute_call(service, **args, session_id="session", agent_id="harness")
        client.call_tool.assert_not_called()
    finally:
        _execution_context.reset(token)
    with pytest.raises(ConnectorToolError, match="approved active session"):
        await execute_call(service, **args, session_id="session", agent_id="harness")
    client.call_tool.assert_not_called()


async def test_trusted_read_is_explicit_allowlist_only():
    from machina.connectors.execution import call_spec

    service = ConnectorService()
    service.registry.add(
        ConnectorConfig(
            id="fixture", command=sys.executable, trust="trusted_read", tool_allowlist=["echo"]
        )
    )
    service.registry.set_tools(
        "fixture",
        [
            DiscoveredTool("fixture", "", name, schema={"type": "object", "properties": {}})
            for name in ["echo", "boom"]
        ],
    )
    trusted = build_request(
        **call_spec(service, {"name": "fixture__echo", "arguments": {}}), tool="mcp_call"
    )
    unknown = build_request(
        **call_spec(service, {"name": "fixture__boom", "arguments": {}}), tool="mcp_call"
    )
    assert decide(trusted).verdict == "allowed"
    assert decide(unknown).verdict == "pending"


async def test_launch_keeps_approved_snapshot_if_other_worker_changes_config_during_secret_resolution(
    tmp_path, monkeypatch
):
    """A concurrent durable edit cannot substitute an unreviewed launch."""
    from machina.connectors.execution import connect_authorized, prepare_connect
    from machina.connectors.manager import ConnectorManager
    from machina.connectors.models import ConnectionState
    from machina.connectors.registry import ConnectorRegistry
    from machina.connectors.store import ConnectorStore
    from machina.core.execution_context import ExecutionContext
    from machina.permissions import broker

    store = ConnectorStore(tmp_path / "mcp.json")
    registry = ConnectorRegistry()
    launched = []

    class Client:
        def __init__(self, config):
            self.config = config
            self.state = ConnectionState.DISCONNECTED
            self.server_name = self.server_version = self.protocol_version = self.last_error = ""

        async def connect(self, secrets):
            launched.append(self.config)
            self.state = ConnectionState.CONNECTED

        async def disconnect(self):
            self.state = ConnectionState.DISCONNECTED

        async def list_tools(self):
            return [], False

    service = ConnectorService(registry, ConnectorManager(registry, Client), store)
    original = {
        "id": "fixture",
        "command": sys.executable,
        "argv": ["approved.py"],
        "secret_refs": ["TOKEN"],
    }
    await service.configure(original)
    monkeypatch.setattr(
        "machina.core.execution_context.resolve_execution_context",
        AsyncMock(return_value=ExecutionContext("ask", "host", tmp_path)),
    )
    monkeypatch.setattr(broker.permission_broker, "guard", AsyncMock())

    def resolve_secret(_name):
        ConnectorStore(store.path).configure({**original, "argv": ["unapproved.py"]})
        return "fixture-token"

    monkeypatch.setattr("machina.security.secrets.get_secret", resolve_secret)
    preview = await prepare_connect(service, "fixture", "session", "agent")
    result = await connect_authorized(service, "fixture", "session", "agent", preview["digest"])
    assert len(launched) == 1
    assert launched[0].argv == ["approved.py"]
    assert launched[0].command == preview["proposal"]["argv"][0]
    assert launched[0].cwd == str(tmp_path)
    assert result["revision"] == 1
    assert store.load()[1][0].argv == ["unapproved.py"]


async def test_configuration_wait_is_bounded_before_dispatch(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from machina.connectors.execution import call_spec, execute_call
    from machina.connectors.models import ConnectionState, ConnectorToolError
    from machina.permissions.broker import _execution_context

    service = ConnectorService()
    service.registry.add(ConnectorConfig(id="fixture", command=sys.executable, call_timeout=0.03))
    service.registry.set_tools("fixture", [DiscoveredTool("fixture", "", "echo", schema={})])
    client = SimpleNamespace(state=ConnectionState.CONNECTED, call_tool=AsyncMock())
    monkeypatch.setattr(service.manager, "connection", lambda _: client)
    args = {"name": "fixture__echo", "arguments": {}}
    request = build_request(**call_spec(service, args), tool="mcp_call", session_id="session")
    token = _execution_context.set(
        {"session_id": "session", "mode": "ask", "backend": "host", "digest": request.digest}
    )
    gate = asyncio.Event()
    monkeypatch.setattr(service, "refresh", gate.wait)
    try:
        with pytest.raises(ConnectorToolError, match="not dispatched"):
            await asyncio.wait_for(
                execute_call(service, **args, session_id="session", agent_id="harness"), timeout=0.5
            )
        client.call_tool.assert_not_called()
    finally:
        _execution_context.reset(token)


async def test_authenticated_gateway_configure_connect_discover_disconnect_restart(
    db_pool, monkeypatch, tmp_path
):
    pytest.importorskip("mcp")
    from machina.connectors.store import ConnectorStore
    from machina.core.runtime import RuntimeServices
    from machina.core.session import session_manager
    from machina.db.connection import db
    from tests.test_gateway_connectors import WireHarness

    monkeypatch.setattr(db, "_pool", db_pool.pool)
    service = ConnectorService(store=ConnectorStore(tmp_path / "mcp.json"))
    runtime = RuntimeServices(_connector_service=service)
    wire = WireHarness(runtime)
    wire.gateway._auth_token = "fixture-human-token"
    session = await session_manager.create(agent_id="harness")
    fixture = (Path(__file__).parent / "support" / "mcp_fixture_server.py").resolve()

    async def call(method, **params):
        response = await wire.call(method, {"token": "fixture-human-token", **params})
        assert "error" not in response, response
        return response["result"]

    try:
        saved = await call(
            "mcp.configure",
            config={
                "id": "fixture",
                "command": sys.executable,
                "argv": [str(fixture), "--name", uuid.uuid4().hex],
                "cwd": str(tmp_path),
                "max_retries": 0,
            },
        )
        assert saved["server"]["state"] == "disconnected"
        assert service.manager.connection("fixture") is None
        preview = await call("mcp.preview", id="fixture", sessionId=str(session.id))
        assert service.manager.connection("fixture") is None
        connected = await call(
            "mcp.connect", id="fixture", sessionId=str(session.id), digest=preview["digest"]
        )
        assert connected["server"]["state"] == "connected"
        tools = await call("mcp.tools", id="fixture")
        assert any(t["name"] == "fixture__echo" for t in tools["tools"])
        assert (await call("mcp.disconnect", id="fixture"))["server"]["state"] == "disconnected"
        restarted = ConnectorService(store=ConnectorStore(tmp_path / "mcp.json"))
        assert (await restarted.list())[0]["id"] == "fixture"
        assert restarted.manager.connection("fixture") is None
        assert (await call("mcp.remove", id="fixture"))["removed"]
        assert not (await call("mcp.list"))["servers"]
    finally:
        await service.manager.shutdown_all()
