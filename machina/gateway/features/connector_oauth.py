"""Authenticated human OAuth management routes (control plane, not model tools)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from machina.connectors.oauth import OAuthError, OAuthOwner
from machina.gateway.errors import INVALID_PARAMS, NOT_FOUND, OPERATION_FAILED, UNAUTHORIZED, RpcError
from machina.gateway.features._common import _str

if TYPE_CHECKING:
    from machina.gateway.server import Gateway


def _coordinator(gw: Gateway):
    coordinator = getattr(gw.runtime.get_connector_service(), "oauth", None)
    if coordinator is None:
        raise RpcError(OPERATION_FAILED, "oauth is unavailable in this runtime")
    return coordinator


async def _authorized_owner(gw: Gateway, params: dict[str, Any], connector_id: str) -> OAuthOwner:
    """Bind every OAuth action to the current configuration's actor authority."""
    _str(params, "sessionId", max_len=64)
    session = await gw.get_session(params)
    service = gw.runtime.get_connector_service()
    await service.refresh()
    config = service.registry.get(connector_id)
    if config is None:
        raise RpcError(NOT_FOUND, "connector not found")
    if config.owner and config.owner != session.agent_id:
        raise RpcError(UNAUTHORIZED, "connector belongs to another agent")
    return OAuthOwner(session_id=str(session.id), agent_id=session.agent_id)


def _map_error(exc: OAuthError) -> RpcError:
    reason = getattr(exc, "reason", "")
    message = str(exc) or reason or "oauth operation failed"
    if reason in ("oauth_operation_not_found",):
        return RpcError(NOT_FOUND, message)
    if reason in (
        "oauth_not_configured",
        "oauth_scope_not_allowed",
        "oauth_callback_invalid",
        "oauth_config_changed",
    ):
        return RpcError(INVALID_PARAMS, message)
    if "not authorized" in message.lower() or "owner" in message.lower():
        return RpcError(UNAUTHORIZED, message)
    return RpcError(OPERATION_FAILED, message)


async def mcp_oauth_preview(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    from machina.connectors.models import ConnectionFailed

    if not isinstance(params, dict):
        raise RpcError(INVALID_PARAMS, "params must be an object")
    allowed = {"id", "sessionId", "issuer", "token"}
    if set(params) - allowed:
        raise RpcError(INVALID_PARAMS, "unsupported oauth preview parameters")
    connector_id = _str(params, "id", max_len=64)
    issuer = params.get("issuer")
    if issuer is not None and (not isinstance(issuer, str) or not issuer.strip()):
        raise RpcError(INVALID_PARAMS, "issuer must be a non-empty string")
    owner = await _authorized_owner(gw, params, connector_id)
    try:
        return await _coordinator(gw).preview(
            connector_id, owner, issuer=issuer.strip() if isinstance(issuer, str) else None
        )
    except OAuthError as exc:
        raise _map_error(exc) from None
    except ConnectionFailed as exc:
        raise RpcError(NOT_FOUND, str(exc)) from None


async def mcp_oauth_start(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise RpcError(INVALID_PARAMS, "params must be an object")
    allowed = {"id", "sessionId", "previewId", "digest", "token"}
    if set(params) - allowed:
        raise RpcError(INVALID_PARAMS, "unsupported oauth start parameters")
    connector_id = _str(params, "id", max_len=64)
    preview_id = _str(params, "previewId", max_len=64)
    digest = _str(params, "digest", max_len=128)
    owner = await _authorized_owner(gw, params, connector_id)
    try:
        return await _coordinator(gw).start(
            connector_id, owner, preview_id=preview_id, digest=digest
        )
    except OAuthError as exc:
        raise _map_error(exc) from None


async def mcp_oauth_status(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise RpcError(INVALID_PARAMS, "params must be an object")
    allowed = {"id", "sessionId", "operationId", "token"}
    if set(params) - allowed:
        raise RpcError(INVALID_PARAMS, "unsupported oauth status parameters")
    connector_id = _str(params, "id", max_len=64)
    operation_id = params.get("operationId")
    if operation_id is not None and (not isinstance(operation_id, str) or not operation_id.strip()):
        raise RpcError(INVALID_PARAMS, "operationId must be a non-empty string")
    owner = await _authorized_owner(gw, params, connector_id)
    try:
        return await _coordinator(gw).status(
            connector_id,
            owner,
            operation_id=operation_id.strip() if isinstance(operation_id, str) else None,
        )
    except OAuthError as exc:
        raise _map_error(exc) from None


async def mcp_oauth_url(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise RpcError(INVALID_PARAMS, "params must be an object")
    allowed = {"id", "sessionId", "operationId", "token"}
    if set(params) - allowed:
        raise RpcError(INVALID_PARAMS, "unsupported oauth url parameters")
    connector_id = _str(params, "id", max_len=64)
    operation_id = _str(params, "operationId", max_len=64)
    owner = await _authorized_owner(gw, params, connector_id)
    try:
        url = await _coordinator(gw).authorization_url(
            operation_id, owner, connector_id=connector_id
        )
    except OAuthError as exc:
        raise _map_error(exc) from None
    return {"authorizationUrl": url}


async def mcp_oauth_open(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise RpcError(INVALID_PARAMS, "params must be an object")
    allowed = {"id", "sessionId", "operationId", "token"}
    if set(params) - allowed:
        raise RpcError(INVALID_PARAMS, "unsupported oauth open parameters")
    connector_id = _str(params, "id", max_len=64)
    operation_id = _str(params, "operationId", max_len=64)
    owner = await _authorized_owner(gw, params, connector_id)
    try:
        opened = await _coordinator(gw).open_browser(operation_id, owner, connector_id=connector_id)
    except OAuthError as exc:
        raise _map_error(exc) from None
    return {"opened": bool(opened)}


async def mcp_oauth_cancel(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise RpcError(INVALID_PARAMS, "params must be an object")
    allowed = {"id", "sessionId", "previewId", "operationId", "token"}
    if set(params) - allowed:
        raise RpcError(INVALID_PARAMS, "unsupported oauth cancel parameters")
    connector_id = _str(params, "id", max_len=64)
    preview_id = params.get("previewId")
    operation_id = params.get("operationId")
    for value, name in ((preview_id, "previewId"), (operation_id, "operationId")):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise RpcError(INVALID_PARAMS, f"{name} must be a non-empty string")
    owner = await _authorized_owner(gw, params, connector_id)
    try:
        cancelled = await _coordinator(gw).cancel(
            owner,
            connector_id=connector_id,
            preview_id=preview_id.strip() if isinstance(preview_id, str) else None,
            operation_id=operation_id.strip() if isinstance(operation_id, str) else None,
        )
    except OAuthError as exc:
        raise _map_error(exc) from None
    return {"cancelled": bool(cancelled)}


async def mcp_oauth_forget(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise RpcError(INVALID_PARAMS, "params must be an object")
    allowed = {"id", "sessionId", "expectedGeneration", "revoke", "token"}
    if set(params) - allowed:
        raise RpcError(INVALID_PARAMS, "unsupported oauth forget parameters")
    connector_id = _str(params, "id", max_len=64)
    expected = params.get("expectedGeneration")
    if type(expected) is not int or expected < 0:
        raise RpcError(INVALID_PARAMS, "expectedGeneration must be a non-negative integer")
    revoke = params.get("revoke", False)
    if type(revoke) is not bool:
        raise RpcError(INVALID_PARAMS, "revoke must be a boolean")
    owner = await _authorized_owner(gw, params, connector_id)
    try:
        return await _coordinator(gw).forget(
            connector_id, owner, expected_generation=expected, revoke=revoke
        )
    except OAuthError as exc:
        raise _map_error(exc) from None
