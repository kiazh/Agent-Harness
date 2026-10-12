"""Refresh eligibility and the remote call share one absolute call deadline."""

import asyncio

from machina.connectors import execution
from machina.connectors.manager import ConnectorManager
from machina.connectors.models import CallOutcome, ConnectionState, ConnectorConfig, DiscoveredTool
from machina.connectors.registry import ConnectorRegistry
from machina.connectors.service import ConnectorService
from machina.permissions.broker import _execution_context
from machina.permissions.policy import build_request


async def test_refresh_time_is_deducted_from_remote_call_deadline(monkeypatch):
    registry = ConnectorRegistry()
    registry.add(
        ConnectorConfig(
            id="remote",
            transport="streamable_http",
            endpoint="https://example.com/mcp",
            oauth={"client_mode": "auto"},
            call_timeout=0.12,
        )
    )
    registry.set_tools(
        "remote",
        [
            DiscoveredTool(
                "remote",
                "",
                "echo",
                schema={"type": "object"},
            )
        ],
    )

    class Connection:
        state = ConnectionState.CONNECTED

        async def call_tool(self, name, arguments):
            await asyncio.sleep(0.08)
            return CallOutcome(ok=True, text="late success")

    connection = Connection()
    manager = ConnectorManager(registry)
    monkeypatch.setattr(manager, "connection", lambda _id: connection)
    service = ConnectorService(registry, manager)

    async def preflight(*args, **kwargs):
        await asyncio.sleep(0.08)

    monkeypatch.setattr(execution, "_oauth_preflight", preflight)
    request = build_request(
        **execution.call_spec(service, {"name": "remote__echo", "arguments": {}}),
        tool="mcp_call",
        mode="ask",
        backend="host",
        session_id="test",
        agent_id="harness",
    )
    token = _execution_context.set(
        {"session_id": "test", "mode": "ask", "backend": "host", "digest": request.digest}
    )
    try:
        outcome = await execution.execute_call(
            service, "remote__echo", {}, session_id="test", agent_id="harness"
        )
        assert outcome.outcome_unknown is True
        assert outcome.ok is False
    finally:
        _execution_context.reset(token)
