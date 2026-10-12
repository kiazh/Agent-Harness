"""Management and catalog contracts with injected connections, no SDK."""

import pytest

from machina.connectors.manager import ConnectorManager
from machina.connectors.models import (
    ConfigError,
    ConnectionFailed,
    ConnectionState,
    ConnectorConfig,
    ConnectorToolError,
    DiscoveredTool,
)
from machina.connectors.registry import ConnectorRegistry
from machina.connectors.service import ConnectorService


class CatalogClient:
    def __init__(self, config):
        self.config = config
        self.state = ConnectionState.DISCONNECTED
        self.server_name = "fixture"
        self.server_version = "1"
        self.protocol_version = "negotiated"
        self.last_error = ""
        self.truncated = False

    async def connect(self, _secrets):
        self.state = ConnectionState.CONNECTED

    async def disconnect(self):
        self.state = ConnectionState.DISCONNECTED

    async def list_tools(self):
        return [
            DiscoveredTool(
                connector_id=self.config.id,
                name="",
                remote_name="echo",
                description="Untrusted echo",
                schema={"type": "object", "properties": {"text": {"type": "string"}}},
            )
        ], self.truncated


def setup_service(manager_only=False):
    registry = ConnectorRegistry()
    registry.add(
        ConnectorConfig(
            id="alpha", command="fixture", trust="trusted_read", tool_allowlist=["echo"]
        )
    )
    registry.add(ConnectorConfig(id="beta", command="fixture"))
    clients = []

    def factory(config):
        client = CatalogClient(config)
        clients.append(client)
        return client

    manager = ConnectorManager(registry, factory)
    service = (
        ConnectorService(manager=manager) if manager_only else ConnectorService(registry, manager)
    )
    return service, clients


async def test_inventory_and_status_do_not_launch_connections():
    service, clients = setup_service()
    inventory = await service.list()
    assert [entry["id"] for entry in inventory] == ["alpha", "beta"]
    assert (await service.status("alpha"))["state"] == "disconnected"
    assert clients == []


async def test_service_uses_injected_managers_inventory():
    service, _clients = setup_service(manager_only=True)
    assert [entry["id"] for entry in await service.list()] == ["alpha", "beta"]
    assert (await service.status("alpha"))["id"] == "alpha"


def test_mismatched_inventory_is_refused():
    first, second = ConnectorRegistry(), ConnectorRegistry()
    with pytest.raises(ConfigError):
        ConnectorService(first, ConnectorManager(second, CatalogClient))


async def test_connect_describe_and_disconnect_contracts():
    service, _clients = setup_service()
    assert (await service.connect("alpha"))["state"] == "connected"
    out = await service.tools_list("alpha")
    assert out["truncated"] is False
    tools = out["tools"]
    assert tools == [
        {
            "name": "alpha__echo",
            "remote_name": "echo",
            "description": "Untrusted echo",
            "verdict": "trusted_read",
        }
    ]
    assert "schema" not in tools[0]
    described = await service.tools_describe("alpha__echo")
    assert described["schema"]["properties"]["text"]["type"] == "string"
    assert described["schema_revision"] == 0
    assert await service.disconnect("alpha") == {"id": "alpha", "disconnected": True}
    assert await service.disconnect("alpha") == {"id": "alpha", "disconnected": False}


@pytest.mark.parametrize("method", ["status", "connect", "disconnect", "tools_list"])
async def test_unknown_ids_are_errors(method):
    service, clients = setup_service()
    with pytest.raises(ConnectionFailed, match="unknown connector"):
        await getattr(service, method)("missing")
    assert clients == []


async def test_not_connected_and_unknown_tool_refusals():
    service, _clients = setup_service()
    with pytest.raises(ConnectionFailed, match="not connected"):
        await service.tools_list("alpha")
    with pytest.raises(ConnectorToolError, match="unknown tool"):
        await service.tools_describe("alpha__missing")


async def test_remote_disconnect_makes_catalog_discovery_unavailable():
    service, clients = setup_service()
    await service.connect("alpha")
    await service.tools_list("alpha")
    await clients[0].disconnect()  # simulate the remote transport closing
    with pytest.raises(ConnectionFailed, match="not connected"):
        await service.tools_list("alpha")
    with pytest.raises(ConnectionFailed, match="not connected"):
        await service.tools_describe("alpha__echo")


async def test_duplicate_remote_tool_names_remain_distinct_across_connectors():
    service, _clients = setup_service()
    await service.connect("alpha")
    await service.connect("beta")
    alpha = (await service.tools_list("alpha"))["tools"]
    beta = (await service.tools_list("beta"))["tools"]
    assert alpha[0]["name"] == "alpha__echo" and beta[0]["name"] == "beta__echo"
    assert (await service.tools_describe("alpha__echo"))["verdict"] == "trusted_read"
    assert (await service.tools_describe("beta__echo"))["verdict"] == "needs_approval"


async def test_catalog_truncation_is_reported_without_schemas():
    service, clients = setup_service()
    await service.connect("alpha")
    clients[0].truncated = True
    out = await service.tools_list("alpha")
    assert out["truncated"] is True
    assert all("schema" not in item for item in out["tools"])
    assert all(
        set(item) == {"name", "remote_name", "description", "verdict"} for item in out["tools"]
    )
