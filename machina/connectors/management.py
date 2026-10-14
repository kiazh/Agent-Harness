"""Agent-facing MCP server management core (add/connect/remove).

The agent tools (machina/tools/mcp.py) and their permission mapping
(machina/permissions/tools.py) come through this module; the human /mcp
gateway RPCs keep using ConnectorService directly (configure/preview/
connect_authorized/remove). No approval is decided here: the agent loop's
broker guard runs first and stamps a session-bound execution snapshot; every
entry point rebuilds the identical canonical request from the same arguments
and requires the snapshot digest to match before any store write or launch.

Composite flows hold the service's configuration lock across
configure → connect → compensation so no concurrent worker observes a
half-configured server. Failure compensates the store write: a newly added
server is removed, a replaced server's prior fields are restored.
"""

from __future__ import annotations

import json
import math
import shutil
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from machina.connectors.models import (
    ConfigError,
    ConnectorConfig,
    ConnectorToolError,
    validate_connector_id,
)
from machina.connectors.service import ConnectorService, _state_only
from machina.connectors.store import ConnectorStore
from machina.observability.diagnostics import record_failure
from machina.permissions.broker import get_execution_context
from machina.permissions.policy import build_request

# Deletion may wait for one connection's bounded shutdown.
_DISCONNECT_TIMEOUT = 30
# Review capability reused from REVIEW_CAPABILITIES: adding or removing a
# server grants/revokes external reach (tools, network, processes) and must
# never be implicit in ask/workspace/sandbox modes.
_REVIEW_CAPABILITIES = ["mcp_external_review"]


def _string(args: dict[str, Any], key: str, default: str = "") -> str:
    value = args.get(key, default)
    if value is None:
        value = default
    if not isinstance(value, str):
        raise ConfigError(f"{key} must be a string")
    return value.strip()


def _string_list(args: dict[str, Any], key: str) -> list[str]:
    value = args.get(key)
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ConfigError(f"{key} must be a list of strings")
    return list(value)


def _normalize_connect(args: dict[str, Any]) -> tuple[dict[str, Any], ConnectorConfig, str]:
    """Validate tool arguments into store fields (+ resolved stdio executable).

    Reuses the store's own validator: scheme/credential/command/allowlist/
    timeout policy is exactly the one the human /mcp surface enforces. The
    stdio executable is resolved once here so the approved digest and the
    launched binary bind the same absolute path.
    """
    if not isinstance(args, dict):
        raise ConfigError("mcp_connect arguments must be an object")
    name = _string(args, "name")
    if not name:
        raise ConfigError("name is required (a short server id, e.g. 'docs')")
    url = _string(args, "url")
    command = _string(args, "command")
    if bool(url) == bool(command):
        raise ConfigError("provide exactly one of url (HTTP) or command (stdio)")
    local_access = args.get("local_access", False)
    if not isinstance(local_access, bool):
        raise ConfigError("local_access must be a boolean")
    fields: dict[str, Any] = {
        "id": name,
        "display_name": name,
        "transport": "streamable_http" if url else "stdio",
        "endpoint": url,
        "command": command,
        "argv": _string_list(args, "argv"),
        "trust": _string(args, "trust", "untrusted"),
        "tool_allowlist": _string_list(args, "tool_allowlist"),
        "env_allowlist": _string_list(args, "env_allowlist"),
        "local_access": local_access,
    }
    for key in ("connect_timeout", "call_timeout", "discovery_timeout"):
        value = args.get(key)
        if value is not None:
            fields[key] = value
    config = ConnectorStore.validate_input(fields)
    executable = ""
    if config.transport == "stdio":
        found = shutil.which(config.command)
        if not found:
            raise ConfigError(f"executable not found on PATH: {config.command}")
        executable = str(Path(found).absolute())
    return fields, config, executable


def connect_spec(args: dict[str, Any]) -> dict[str, Any]:
    """Canonical broker kwargs for one mcp_connect request.

    Deterministic from the same arguments at guard and execution time, so the
    approved digest binds the exact server name, transport destination or
    executable, trust, allowlists and timeouts the model asked to define.
    """
    _, config, executable = _normalize_connect(args)
    content = json.dumps(
        {
            "id": config.id,
            "transport": config.transport,
            "endpoint": config.endpoint,
            "command": executable,
            "argv": list(config.argv),
            "trust": config.trust,
            "tool_allowlist": list(config.tool_allowlist),
            "env_allowlist": list(config.env_allowlist),
            "local_access": config.local_access,
            "connect_timeout": config.connect_timeout,
            "call_timeout": config.call_timeout,
            "discovery_timeout": config.discovery_timeout,
        },
        sort_keys=True,
    )
    return {
        "operation": "tool.mcp_connect",
        "targets": [],
        "content": content,
        "argv": [executable, *config.argv] if config.transport == "stdio" else [],
        "network": [config.endpoint] if config.endpoint else [],
        "capabilities": list(_REVIEW_CAPABILITIES),
        "timeout": int(math.ceil(config.connect_timeout + config.discovery_timeout)),
    }


def _normalize_disconnect(args: dict[str, Any]) -> tuple[str, bool]:
    if not isinstance(args, dict):
        raise ConfigError("mcp_disconnect arguments must be an object")
    name = _string(args, "name")
    if not name:
        raise ConfigError("name is required (the server id to remove)")
    validate_connector_id(name)
    remove = args.get("remove", True)
    if not isinstance(remove, bool):
        raise ConfigError("remove must be a boolean")
    return name, remove


def disconnect_spec(args: dict[str, Any]) -> dict[str, Any]:
    """Canonical broker kwargs for one mcp_disconnect request."""
    name, remove = _normalize_disconnect(args)
    return {
        "operation": "tool.mcp_disconnect",
        "targets": [],
        "content": json.dumps({"id": name, "remove": remove}, sort_keys=True),
        "capabilities": list(_REVIEW_CAPABILITIES),
        "timeout": _DISCONNECT_TIMEOUT,
    }


def _approved_request(spec: dict[str, Any], *, tool: str, agent_id: str, session_id: str) -> None:
    """Require the broker-stamped snapshot for exactly this rebuilt request."""
    snapshot = get_execution_context()
    if not snapshot or snapshot.get("session_id") != str(session_id):
        raise ConnectorToolError(f"{tool} requires an approved active session")
    request = build_request(
        **spec,
        tool=tool,
        mode=str(snapshot.get("mode") or "ask"),
        backend=str(snapshot.get("backend") or "host"),
        agent_id=agent_id,
        session_id=str(session_id),
    )
    if snapshot.get("digest") != request.digest:
        raise ConnectorToolError(f"{tool} request changed after approval; ask again")


def _fields_of(config: ConnectorConfig) -> dict[str, Any]:
    """Restorable fields for one stored config (authority fields removed)."""
    fields = asdict(config)
    fields.pop("owner", None)
    fields.pop("revision", None)
    return fields


def _owner_ok(config: ConnectorConfig, agent_id: str) -> bool:
    # Durable-store configs are ownerless by construction; registry-only
    # configs may carry an owner that another agent must not overwrite.
    return not config.owner or config.owner == agent_id


def _view(service: ConnectorService, connector_id: str) -> dict[str, Any]:
    config = service._config(connector_id)
    return {**config.public_view(), **_state_only(service.manager.status(connector_id))}


async def _compensate(
    service: ConnectorService, connector_id: str, prior: dict[str, Any] | None
) -> str:
    """Undo a failed add: remove a new server, restore a replaced one."""
    try:
        if prior is None:
            await service._remove_locked(connector_id)
            return "configuration removed"
        await service._configure_locked(prior)
        return "previous configuration restored"
    except Exception as exc:
        record_failure("connector_management.compensate", exc)
        return "compensation failed; inspect with mcp_list"


async def add_and_connect(
    service: ConnectorService, args: dict[str, Any], *, agent_id: str, session_id: str
) -> dict[str, Any]:
    """Add (or replace) a server, launch it, and report the discovered tools.

    Validation and approval-identity checks run before any write; the
    configure → connect → discover sequence runs under one configuration
    lock and compensates the store on connect/discovery failure. A cancelled
    call keeps the stored config (visible in mcp_list; remove via
    mcp_disconnect) because cancellation is not a connect failure.
    """
    fields, config, executable = _normalize_connect(args)
    _approved_request(
        connect_spec(args), tool="mcp_connect", agent_id=agent_id, session_id=session_id
    )
    launch_config = replace(config, command=executable) if executable else config
    async with service._configuration_lock:
        await service._refresh_locked()
        existing = service.registry.get(config.id)
        if existing is not None and not _owner_ok(existing, agent_id):
            raise ConnectorToolError("connector belongs to another agent")
        prior = _fields_of(existing) if existing is not None else None
        await service._configure_locked(fields)
        try:
            tools = await service.connect_and_discover(config.id, launch_config=launch_config)
        except Exception as exc:
            compensated = await _compensate(service, config.id, prior)
            return {
                "connected": False,
                "server": config.id,
                "error": str(exc) or type(exc).__name__,
                "store": compensated,
            }
        return {
            "connected": True,
            "server": _view(service, config.id),
            "tools": [tool.name for tool in tools],
        }


async def remove_server(
    service: ConnectorService, args: dict[str, Any], *, agent_id: str, session_id: str
) -> dict[str, Any]:
    """Remove a server (or only close its connection when remove is false).

    Uses the same retirement path as the human /mcp remove flow: the live
    connection is closed, OAuth grants are invalidated, and the durable
    configuration is deleted.
    """
    name, remove = _normalize_disconnect(args)
    _approved_request(
        disconnect_spec(args), tool="mcp_disconnect", agent_id=agent_id, session_id=session_id
    )
    async with service._configuration_lock:
        await service._refresh_locked()
        config = service.registry.get(name)
        if config is None:
            return {"removed": False, "error": f"unknown MCP server: {name}"}
        if not _owner_ok(config, agent_id):
            raise ConnectorToolError("connector belongs to another agent")
        if remove:
            await service._remove_locked(name)
            return {"removed": True, "server": name}
        disconnected = await service.manager.disconnect(name)
        return {"removed": False, "disconnected": bool(disconnected), "server": name}
