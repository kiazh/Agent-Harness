"""Connector inventory and collision-safe namespaced tool lookup.

In-memory for this release (durable PostgreSQL state arrives with the
management surfaces). Every mutation bumps a revision counter so caches
built on discovery (catalogs, schemas) can invalidate.
"""

from __future__ import annotations

import re

from machina.connectors.models import (
    MAX_CATALOG_TOOLS,
    MAX_DESCRIPTION_CHARS,
    MAX_TOOL_NAME_CHARS,
    ConfigError,
    ConnectorConfig,
    DiscoveredTool,
)

_NAME_SANITIZE_RE = re.compile(r"[^A-Za-z0-9_]")


def namespaced_tool_name(connector_id: str, remote_name: str) -> str:
    """Stable model-facing name; originals stay on the DiscoveredTool."""
    budget = MAX_TOOL_NAME_CHARS - len(connector_id) - 2
    clean = _NAME_SANITIZE_RE.sub("_", remote_name)[:budget] or "tool"
    return f"{connector_id}__{clean}"


class ConnectorRegistry:
    """Validated connector inventory with a namespaced tool index."""

    def __init__(self) -> None:
        self._connectors: dict[str, ConnectorConfig] = {}
        self._tools: dict[str, DiscoveredTool] = {}
        self.revision = 0

    def _bump(self) -> None:
        self.revision += 1

    def add(self, config: ConnectorConfig) -> ConnectorConfig:
        """Validate and store a connector; duplicate ids are rejected."""
        config.validate()
        if config.id in self._connectors:
            raise ConfigError(f"connector already registered: {config.id}")
        stored = ConnectorConfig(**{**config.__dict__})
        stored.revision = 0
        self._connectors[stored.id] = stored
        self._bump()
        return stored

    def update(self, connector_id: str, **fields: object) -> ConnectorConfig:
        """Validate and replace one connector; unknown fields rejected."""
        current = self._connectors.get(connector_id)
        if current is None:
            raise ConfigError(f"unknown connector: {connector_id}")
        if unexpected := set(fields) - set(ConnectorConfig.__dataclass_fields__):
            raise ConfigError(f"unsupported connector fields: {sorted(unexpected)}")
        if "id" in fields and fields["id"] != connector_id:
            raise ConfigError("connector id is immutable")
        updated = ConnectorConfig(
            **{**current.__dict__, **fields, "revision": current.revision + 1}
        )
        updated.validate()
        self._connectors[connector_id] = updated
        self._drop_tools(connector_id)
        self._bump()
        return updated

    def remove(self, connector_id: str) -> bool:
        """Remove a connector and its tools; returns False when absent."""
        if self._connectors.pop(connector_id, None) is None:
            return False
        self._drop_tools(connector_id)
        self._bump()
        return True

    def get(self, connector_id: str) -> ConnectorConfig | None:
        return self._connectors.get(connector_id)

    def list(self) -> list[ConnectorConfig]:
        return sorted(self._connectors.values(), key=lambda c: c.id)

    def set_tools(self, connector_id: str, tools: list[DiscoveredTool]) -> list[DiscoveredTool]:
        """Replace one connector's catalog (bounded); other connectors untouched."""
        if connector_id not in self._connectors:
            raise ConfigError(f"unknown connector: {connector_id}")
        if len(tools) > MAX_CATALOG_TOOLS:
            raise ConfigError(f"catalog exceeds {MAX_CATALOG_TOOLS} tools")
        stored = []
        names: set[str] = set()
        for tool in tools:
            name = namespaced_tool_name(connector_id, tool.remote_name)
            existing = self._tools.get(name)
            if name in names or (existing is not None and existing.connector_id != connector_id):
                raise ConfigError("tool name collision in connector catalog")
            names.add(name)
            description = (tool.description or "")[:MAX_DESCRIPTION_CHARS]
            stored.append(
                DiscoveredTool(
                    connector_id=connector_id,
                    name=name,
                    remote_name=tool.remote_name,
                    description=description,
                    schema=dict(tool.schema or {}),
                    schema_revision=tool.schema_revision,
                )
            )
        self._drop_tools(connector_id)
        for tool in stored:
            self._tools[tool.name] = tool
        self._bump()
        return stored

    def _drop_tools(self, connector_id: str) -> None:
        for name in [n for n, t in self._tools.items() if t.connector_id == connector_id]:
            del self._tools[name]

    def lookup(self, name: str) -> DiscoveredTool | None:
        """Resolve a namespaced tool name to its catalog entry (or None)."""
        return self._tools.get(name)

    def tools_for(self, connector_id: str) -> list[DiscoveredTool]:
        return sorted(
            (t for t in self._tools.values() if t.connector_id == connector_id),
            key=lambda t: t.name,
        )
