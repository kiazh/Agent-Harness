"""Shared connector operations (management + bounded discovery).

This release covers listing, status, connect/disconnect, and tool
catalog reads. Executing remote tools through the agent pipeline
(Phase C: exact broker approval, ownership, durable resume) is deferred
and documented in docs/plans/MCP_CONNECTORS_PROGRESS.md — there is
deliberately no `tools.call` here yet.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ah.connectors.manager import ConnectorManager
from ah.connectors.models import ConfigError, ConnectionFailed, ConnectionState, ConnectorToolError
from ah.connectors.policy import classify_tool
from ah.connectors.registry import ConnectorRegistry


class ConnectorService:
    """One service object shared by future CLI/TUI/HTTP adapters."""

    def __init__(
        self,
        registry: ConnectorRegistry | None = None,
        manager: ConnectorManager | None = None,
    ) -> None:
        if registry is not None and manager is not None and manager.registry is not registry:
            raise ConfigError("service and manager must share one connector inventory")
        self.registry = registry or (
            manager.registry if manager is not None else ConnectorRegistry()
        )
        self.manager = manager or ConnectorManager(self.registry)

    async def list(self) -> list[dict[str, Any]]:
        """Sanitized inventory with live state; launches nothing."""
        return [
            {**config.public_view(), **_state_only(self.manager.status(config.id))}
            for config in self.registry.list()
        ]

    async def status(self, connector_id: str) -> dict[str, Any]:
        """State of one connector without launching anything."""
        config = self.registry.get(connector_id)
        if config is None:
            raise ConnectionFailed(f"unknown connector: {connector_id}")
        return {**config.public_view(), **_state_only(self.manager.status(connector_id))}

    async def connect(
        self, connector_id: str, secrets: Mapping[str, str] | None = None
    ) -> dict[str, Any]:
        """Connect with bounded retries; disabled servers are refused."""
        return await self.manager.connect(connector_id, secrets)

    async def disconnect(self, connector_id: str) -> dict[str, Any]:
        """Disconnect; reports whether a connection existed."""
        self._config(connector_id)
        return {"id": connector_id, "disconnected": await self.manager.disconnect(connector_id)}

    async def tools_list(self, connector_id: str) -> dict[str, Any]:
        """Bounded catalog (names + descriptions); requires a live connection.

        Schemas stay server-side until tools_describe: discovery must not
        dump every schema into every consumer.
        """
        connection = self._live_connection(connector_id)
        tools, truncated = await connection.list_tools()
        stored = self.manager.registry.set_tools(connector_id, tools)
        return {
            "tools": [
                {
                    "name": tool.name,
                    "remote_name": tool.remote_name,
                    "description": tool.description,
                    "verdict": classify_tool(self._config(connector_id), tool),
                }
                for tool in stored
            ],
            "truncated": bool(truncated),
        }

    async def tools_describe(self, name: str) -> dict[str, Any]:
        """Full schema + policy verdict for one namespaced tool."""
        tool = self.manager.registry.lookup(name)
        if tool is None:
            raise ConnectorToolError(f"unknown tool (discover first): {name}")
        self._live_connection(tool.connector_id)
        return {
            "name": tool.name,
            "remote_name": tool.remote_name,
            "description": tool.description,
            "schema": tool.schema,
            "schema_revision": tool.schema_revision,
            "verdict": classify_tool(self._config(tool.connector_id), tool),
        }

    def _config(self, connector_id: str):
        config = self.registry.get(connector_id)
        if config is None:
            raise ConnectionFailed(f"unknown connector: {connector_id}")
        return config

    def _live_connection(self, connector_id: str):
        self._config(connector_id)
        connection = self.manager.connection(connector_id)
        if connection is None or connection.state != ConnectionState.CONNECTED:
            raise ConnectionFailed("not connected; connect first (listing never auto-launches)")
        return connection


def _state_only(status: dict[str, Any]) -> dict[str, Any]:
    return {
        key: status.get(key)
        for key in ("state", "server_name", "server_version", "protocol_version", "error")
    }
