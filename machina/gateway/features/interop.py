"""Interop self-setup handlers: detect harnesses and register Machina.

Delegates directly to ``machina.interop`` in this process (that module is
stdlib-only and import-light; the child-process leaf binds lazily). Detection
and enable/disable spawn platform CLIs under bounded deadlines, so the
blocking work runs on a worker thread — the gateway event loop stays
responsive for turns while a probe is in flight.

``interop.enable`` / ``interop.disable`` return the full ``ActionResult``
payload. A platform-side failure (``ok=False``) is reported honestly instead
of raised, so the UI can show the message, steps, and verification; only
client input errors (unknown platform, bad scope) use the shared
INVALID_PARAMS style.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from machina.gateway.errors import INVALID_PARAMS, RpcError
from machina.gateway.features._common import _bool, _str
from machina.interop import (
    DetectResult,
    PlatformSpec,
    UnknownPlatform,
    detect,
    disable,
    enable,
    resolve_platform,
)

if TYPE_CHECKING:
    from machina.gateway.server import Gateway


def _platform(platform: str) -> PlatformSpec:
    """Resolve *platform* or raise the registered INVALID_PARAMS error."""
    try:
        return resolve_platform(platform)
    except UnknownPlatform as error:
        raise RpcError(INVALID_PARAMS, str(error)) from None


def _row(result: DetectResult) -> dict[str, Any]:
    """One explicit JSON-safe status row (no binary paths, no secrets)."""
    scope = result.state_scope or (", ".join(result.scopes) if result.scopes else None)
    return {
        "platform": result.platform,
        "label": result.label,
        "installed": bool(result.installed),
        "version": result.version,
        "running": bool(result.running),
        "registered": bool(result.registered),
        "enabled": None if result.enabled is None else bool(result.enabled),
        "scope": scope,
        "notes": list(result.notes),
    }


async def interop_status(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Read-only detection of every supported harness (threaded; bounded)."""
    results = await asyncio.to_thread(detect)
    return {"platforms": [_row(result) for result in results.values()]}


async def interop_enable(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Idempotently register Machina's MCP server with one harness."""
    spec = _platform(_str(params, "platform", max_len=32))
    scope = _str(params, "scope", required=False, max_len=16).lower() or "project"
    if scope not in ("project", "global"):
        raise RpcError(INVALID_PARAMS, "scope must be project|global")
    dry_run = _bool(params, "dry_run")
    result = await asyncio.to_thread(enable, spec.key, scope=scope, dry_run=dry_run)
    return asdict(result)


async def interop_disable(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Remove the registration where the platform CLI supports it."""
    spec = _platform(_str(params, "platform", max_len=32))
    result = await asyncio.to_thread(disable, spec.key)
    return asdict(result)
