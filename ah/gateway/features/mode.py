"""Mode + approval feature handlers (Phase D/E)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ah.core.config import config
from ah.gateway.errors import INVALID_PARAMS, RpcError

if TYPE_CHECKING:
    from ah.gateway.server import Gateway

from ah.gateway.features._common import _str


async def mode_get(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    gw.require_db()
    return {"mode": config.get("execution_mode"), "backend": _backend()}


async def mode_set(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Explicit mode activation. Full mode records a session-scoped grant."""
    gw.require_db()
    mode = _str(params, "mode", max_len=20).lower()
    if mode not in ("ask", "workspace", "sandbox", "full"):
        raise RpcError(INVALID_PARAMS, "mode must be ask|workspace|sandbox|full")
    scope = str(params.get("scope") or "session")
    if mode == "sandbox":
        from ah.core.runtime import runtime_services

        report = runtime_services.status()
        if report.get("sandbox") == "unavailable":
            raise RpcError(
                INVALID_PARAMS,
                "sandbox isolation unavailable (no Docker); staying in ask mode. "
                "Approve host ask mode instead — never a silent downgrade.",
            )
    config.set("execution_mode", mode, persist=bool(params.get("persist", False)))
    if mode == "full":
        # Explicit activation with disclosed grant (default: current session).
        from ah.permissions import store as _store

        session = await gw.get_session(params) if params.get("sessionId") else None
        await _store.save_grant(
            {
                "session_id": str(session.id) if session else "",
                "agent_id": session.agent_id if session else "harness",
                "mode": "full",
                "capability": "session",
                "scope_path": None,
                "digest": f"full-mode-{scope}",
            }
        )
    return {"mode": mode, "scope": scope, "backend": _backend()}


async def mode_revoke(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    gw.require_db()
    from ah.permissions.broker import permission_broker

    session = await gw.get_session(params)
    n = await permission_broker.revoke(str(session.id))
    config.set("execution_mode", "ask")
    return {"revoked": n, "mode": "ask"}


async def approvals_list(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    gw.require_db()
    from ah.permissions import store as _store

    session = await gw.get_session(params)
    return {"approvals": await _store.list_pending(str(session.id))}


async def approvals_resolve(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Human principal resolves a pending approval (agents cannot self-approve)."""
    gw.require_db()
    from ah.permissions import store as _store

    request_id = _str(params, "requestId", max_len=64)
    verdict = _str(params, "verdict", max_len=16).lower()
    if verdict not in ("approved", "denied"):
        raise RpcError(INVALID_PARAMS, "verdict must be approved|denied")
    rec = await _store.get_approval(request_id)
    if rec is None:
        raise RpcError(INVALID_PARAMS, "unknown approval")
    # Authenticated principal: gateway token holder (human transport), never agent.
    resolved = await _store.resolve_approval(request_id, verdict, principal="tui")
    if resolved is None:
        raise RpcError(INVALID_PARAMS, "approval already consumed or not permitted")
    # Wake the awaiting turn (same process) and notify the UI.
    try:
        fut = gw._pending_approvals.pop(request_id, None)
        if fut is not None and not fut.done():
            fut.set_result(verdict)
    except Exception:
        pass
    try:
        gw._write(
            {
                "jsonrpc": "2.0",
                "method": "event",
                "params": {
                    "type": "permission.resolved",
                    "sessionId": rec.get("session_id", ""),
                    "requestId": request_id,
                    "status": verdict,
                },
            }
        )
    except Exception:
        pass
    return {"approval": {"requestId": request_id, "status": verdict}}


def _backend() -> str:
    try:
        if (config.get("execution_mode") or "ask") == "sandbox":
            return "sandbox"
        return "host"
    except Exception:
        return "host"
