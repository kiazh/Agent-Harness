"""Exact native-tool authorization and human-reviewed connector launch.

Configuration is never a model tool. A management connect consumes a reviewed
preview through the same broker, with the human decision durably recorded.
"""

from __future__ import annotations

import asyncio
import json
import math
import shutil
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from machina.connectors.models import CallOutcome, ConfigError, ConnectorToolError
from machina.connectors.policy import classify_tool, validate_arguments
from machina.permissions.policy import build_request


def _identity(config, service=None) -> dict:
    identity = asdict(config)
    if config.transport == "stdio":
        executable = shutil.which(config.command)
        if not executable:
            raise ConfigError("connector executable was not found")
        # Keep the absolute launcher path: resolving a venv Python symlink
        # changes its environment and can drop installed SDK dependencies.
        path = Path(executable).absolute()
        stat = path.stat()
        identity["executable_identity"] = [
            str(path),
            stat.st_dev,
            stat.st_ino,
            stat.st_size,
            stat.st_mtime_ns,
        ]
    oauth_identity = _service_auth_identity(config, service)
    if oauth_identity is not None:
        identity["oauth"] = oauth_identity
    return identity


def _service_auth_identity(config, service=None) -> dict | None:
    """Non-secret grant binding for broker proposals; never token material."""
    if service is None:
        try:
            from machina.core.runtime import runtime_services

            service = runtime_services.get_connector_service()
        except Exception:
            return None
    reader = getattr(service, "auth_identity", None)
    if reader is None:
        return None
    try:
        if getattr(config, "oauth", None):
            return reader(config.id)
    except Exception:
        return None
    return None


def _owner(config, agent_id: str) -> None:
    if config.owner and config.owner != agent_id:
        raise ConnectorToolError("connector belongs to another agent")
    if not config.enabled:
        raise ConnectorToolError("connector is disabled")


def call_spec(service, arguments: dict[str, Any]) -> dict:
    name = arguments.get("name")
    tool = service.registry.lookup(name) if isinstance(name, str) else None
    if tool is None:
        raise ConnectorToolError("unknown MCP tool; list and describe it first")
    config = service._config(tool.connector_id)
    supplied = arguments.get("arguments")
    validate_arguments(tool.remote_name, supplied, tool.schema)
    content = json.dumps(
        {
            "connector": _identity(config, service),
            "remote_tool": tool.remote_name,
            "schema": tool.schema,
            "schema_revision": tool.schema_revision,
            "arguments": supplied,
        },
        sort_keys=True,
        allow_nan=False,
    )
    return {
        "operation": "tool.mcp_call",
        "targets": [],
        "content": content,
        "timeout": math.ceil(config.call_timeout),
        "capabilities": []
        if classify_tool(config, tool) == "trusted_read"
        else ["mcp_external_review"],
    }


async def _connect_request(
    service, connector_id: str, session_id: str, agent_id: str, *, refresh: bool = True
):
    from machina.core.execution_context import resolve_execution_context

    if refresh:
        await service.refresh()
    config = service._config(connector_id)
    _owner(config, agent_id)
    context = await resolve_execution_context(session_id, "mcp_connect")
    if config.transport == "stdio" and context.mode == "sandbox":
        raise ConnectorToolError("stdio MCP runs on the host; switch mode explicitly to connect")
    identity = _identity(config, service)
    return build_request(
        operation="tool.mcp_connect",
        tool="mcp_connect",
        content=json.dumps(identity, sort_keys=True),
        argv=[identity["executable_identity"][0], *config.argv]
        if config.transport == "stdio"
        else [],
        cwd=config.cwd or str(context.workspace),
        network=[config.endpoint] if config.endpoint else [],
        capabilities=["mcp_external_review"],
        mode=context.mode,
        backend="host",
        timeout=math.ceil(config.connect_timeout),
        agent_id=agent_id,
        session_id=session_id,
    )


async def prepare_connect(service, connector_id: str, session_id: str, agent_id: str) -> dict:
    from machina.permissions.store import proposal_of

    request = await _connect_request(service, connector_id, session_id, agent_id)
    return {
        "digest": request.digest,
        "proposal": {**proposal_of(request), "contentPreview": request.content_preview},
    }


async def connect_authorized(
    service, connector_id: str, session_id: str, agent_id: str, expected_digest: str
) -> dict:
    from machina.permissions import broker, store

    request = await _connect_request(service, connector_id, session_id, agent_id)
    if not expected_digest or request.digest != expected_digest:
        raise ConnectorToolError("connector changed; review a fresh connection preview")

    # Only the authenticated human management route supplies this confirmation.
    # Neither a model tool nor server-initiated callbacks can reach this helper;
    # the model-facing mcp_connect tool goes through its own broker guard.
    async def reviewed(card):
        record = await store.resolve_decision(card["request_id"], "approved", "mcp-management")
        return "approved" if record is not None else "denied"

    tokens = [
        (broker._approval_handler, broker._approval_handler.set(reviewed)),
        (broker._principal, broker._principal.set("mcp-management")),
        (broker._execution_context, broker._execution_context.set(None)),
    ]
    outcome = "failed"
    try:
        await broker.permission_broker.guard(request)
        # Hold the same configuration lock as replacement/removal, and dispatch
        # using the reviewed config directly. A refresh inside service.connect
        # must never substitute an unreviewed destination after validation.
        async with service._configuration_lock:
            if service.store is not None:
                await service._refresh_locked()
            current = await _connect_request(
                service, connector_id, session_id, agent_id, refresh=False
            )
            if current.digest != request.digest:
                raise ConnectorToolError("connector changed during approval; review again")
            config = service._config(connector_id)
            launch_config = (
                replace(config, command=request.argv[0], cwd=request.cwd)
                if config.transport == "stdio"
                else replace(config)
            )
            await service.connect_and_discover(connector_id, launch_config=launch_config)
            outcome = "completed"
            return {**config.public_view(), **service.manager.status(connector_id)}
    finally:
        try:
            if request.approval_id:
                await broker.permission_broker.complete(request.approval_id, outcome)
        finally:
            for variable, token in reversed(tokens):
                variable.reset(token)


async def execute_call(service, name: str, arguments: dict, *, session_id: str, agent_id: str):
    from machina.permissions.broker import get_execution_context

    tool = service.registry.lookup(name)
    if tool is None:
        raise ConnectorToolError("unknown MCP tool; list and describe it first")
    config = service._config(tool.connector_id)
    _owner(config, agent_id)
    started = asyncio.get_running_loop().time()
    try:
        async with asyncio.timeout(config.call_timeout):
            await service.refresh()
    except TimeoutError as exc:
        raise ConnectorToolError(
            "MCP configuration wait timed out; call was not dispatched"
        ) from exc
    tool = service.registry.lookup(name)
    if tool is None:
        raise ConnectorToolError("MCP catalog changed before dispatch")
    config = service._config(tool.connector_id)
    _owner(config, agent_id)
    snapshot = get_execution_context()
    if not snapshot or snapshot.get("session_id") != session_id:
        raise ConnectorToolError("MCP execution requires an approved active session")
    if config.transport == "stdio" and snapshot.get("mode") == "sandbox":
        raise ConnectorToolError("host stdio MCP is unavailable in sandbox mode")
    request = build_request(
        **call_spec(service, {"name": name, "arguments": arguments}),
        tool="mcp_call",
        mode=snapshot["mode"],
        backend=snapshot["backend"],
        session_id=session_id,
        agent_id=agent_id,
    )
    if snapshot.get("digest") != request.digest:
        raise ConnectorToolError("MCP configuration, schema or arguments changed after approval")
    connection = service._live_connection(tool.connector_id)
    # Bound concurrency per connection without resetting the semaphore each call.
    semaphore = getattr(connection, "_harness_call_limit", None)
    if semaphore is None:
        semaphore = connection._harness_call_limit = asyncio.Semaphore(config.concurrency)
    try:
        await asyncio.wait_for(
            semaphore.acquire(),
            timeout=max(0, config.call_timeout - (asyncio.get_running_loop().time() - started)),
        )
    except TimeoutError as exc:
        raise ConnectorToolError("MCP concurrency wait timed out; call was not dispatched") from exc
    try:
        remaining = config.call_timeout - (asyncio.get_running_loop().time() - started)
        try:
            async with asyncio.timeout(max(0, remaining)):
                await service.refresh()
        except TimeoutError as exc:
            raise ConnectorToolError(
                "MCP configuration wait timed out; call was not dispatched"
            ) from exc
        current = build_request(
            **call_spec(service, {"name": name, "arguments": arguments}),
            tool="mcp_call",
            mode=snapshot["mode"],
            backend=snapshot["backend"],
            session_id=session_id,
            agent_id=agent_id,
        )
        if (
            current.digest != request.digest
            or service.manager.connection(tool.connector_id) is not connection
        ):
            raise ConnectorToolError("MCP changed while waiting; obtain fresh approval")
        remaining = config.call_timeout - (asyncio.get_running_loop().time() - started)
        if remaining <= 0:
            raise ConnectorToolError("MCP deadline expired before dispatch")
        if getattr(config, "oauth", None):
            await _oauth_preflight(service, config, remaining=remaining)
        remaining = config.call_timeout - (asyncio.get_running_loop().time() - started)
        if remaining <= 0:
            raise ConnectorToolError("MCP deadline expired before dispatch")
        try:
            async with asyncio.timeout(remaining):
                return await connection.call_tool(tool.remote_name, arguments)
        except TimeoutError:
            return CallOutcome(
                ok=False,
                outcome_unknown=True,
                error="MCP call deadline expired; do not retry automatically",
            )
    finally:
        semaphore.release()


async def _oauth_preflight(service, config, *, remaining: float) -> None:
    """Bounded eligibility check before dispatch; never opens a browser.

    Raises ConnectorToolError (known not dispatched) when no usable token
    can be produced. No refresh failure, 401, or expiry replays the call.
    """
    from machina.connectors.models import AuthRequired

    coordinator = getattr(service, "oauth", None)
    if coordinator is None:
        raise ConnectorToolError("oauth authorization is required; authorize via /mcp")
    identity = service.auth_identity(config.id)
    if identity is None:
        raise ConnectorToolError("oauth authorization is required; authorize via /mcp")
    from machina.connectors.oauth_models import OAuthBinding

    binding = OAuthBinding(
        connector_id=config.id,
        credential_key=str(identity.get("credentialKey", "")),
        resource=str(identity.get("resource", "")),
        issuer=str(identity.get("issuer", "")),
        client_id=str(identity.get("clientId", "")),
    )
    deadline = asyncio.get_running_loop().time() + max(0.0, remaining)
    try:
        token = await coordinator.ensure_token(
            binding, expected_grant=identity, deadline=deadline, config=config
        )
    except AuthRequired as exc:
        raise ConnectorToolError(str(exc) or "authorization required") from exc
    except Exception as exc:
        raise ConnectorToolError(
            str(exc) or "oauth authorization is required; authorize via /mcp"
        ) from exc
    connection = service.manager.connection(config.id)
    if connection is not None and token:
        connection.register_oauth_token(token)
