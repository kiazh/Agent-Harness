"""Read-only inspection of this harness runtime's configured MCP servers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ah.connectors.models import ConnectionFailed
from ah.gateway.errors import NOT_FOUND, RpcError
from ah.gateway.features._common import _str

if TYPE_CHECKING:
    from ah.gateway.server import Gateway


async def mcp_list(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """List sanitized inventory and current state; never connect or discover."""
    return {"servers": await gw.runtime.get_connector_service().list()}


async def mcp_status(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Inspect one server from the same runtime inventory as mcp.list."""
    connector_id = _str(params, "id", max_len=64)
    try:
        server = await gw.runtime.get_connector_service().status(connector_id)
    except ConnectionFailed:
        raise RpcError(NOT_FOUND, f"MCP server {connector_id!r} not found") from None
    return {"server": server}
