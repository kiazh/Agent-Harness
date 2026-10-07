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
        # Explicit activation with disclosed SESSION grant. The global config
        # value alone never authorizes (LP-07): only this live grant does.
        from ah.permissions import store as _store

        session = await gw.get_session(params) if params.get("sessionId") else None
        if session is not None:
            await _store.save_grant(
                {
                    "session_id": str(session.id),
                    "agent_id": session.agent_id,
                    "mode": "full",
                    "capability": "session",
                    "scope_path": None,
                    "scope_type": "file",
                    "grant_kind": "session",
                    "digest": "full-mode-session",
                }
            )
    return {"mode": mode, "scope": scope, "backend": _backend()}


async def mode_revoke(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Revoke SESSION grants only (LP-07); the global default is untouched.

    Pass scope=global (CLI durable reset) to also reset the persistent default.
    """
    gw.require_db()
    from ah.permissions.broker import permission_broker

    session = await gw.get_session(params)
    n = await permission_broker.revoke(str(session.id))
    if str(params.get("scope") or "session") == "global":
        config.set("execution_mode", "ask")
        return {"revoked": n, "mode": "ask", "scope": "global"}
    return {"revoked": n, "mode": config.get("execution_mode"), "scope": "session"}


async def approvals_list(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    gw.require_db()
    from ah.permissions import store as _store

    session = await gw.get_session(params)
    return {"approvals": await _store.list_pending(str(session.id))}


async def approvals_resolve(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Human principal resolves a pending approval (agents cannot self-approve).

    grant: once (default, exact action, single execution, nothing saved) or
    session (explicit reusable authority for the same operation+scope; content
    changes still re-approve). Saved immediately and durably on explicit choice.
    """
    gw.require_db()
    from ah.permissions import store as _store

    request_id = _str(params, "requestId", max_len=64)
    verdict = _str(params, "verdict", max_len=16).lower()
    if verdict not in ("approved", "denied"):
        raise RpcError(INVALID_PARAMS, "verdict must be approved|denied")
    grant = str(params.get("grant") or "once").lower()
    if grant not in ("once", "session"):
        raise RpcError(INVALID_PARAMS, "grant must be once|session")
    rec = await _store.get_approval(request_id)
    if rec is None:
        raise RpcError(INVALID_PARAMS, "unknown approval")
    # Authenticated principal: gateway token holder (human transport), never agent.
    resolved = await _store.resolve_decision(request_id, verdict, principal="tui")
    if resolved is None:
        raise RpcError(INVALID_PARAMS, "approval already consumed or not permitted")
    if verdict == "approved" and grant == "session":
        # Explicit reusable authority (LP-03): bound operation + scope, with
        # the exact digest so changed content re-approves.
        targets = [t for t in str(rec.get("target", "")).split("|") if t]
        await _store.save_grant(
            {
                "session_id": rec.get("session_id", ""),
                "agent_id": rec.get("agent_id", ""),
                "mode": "ask",
                "capability": rec.get("operation", ""),
                "scope_path": targets[0] if targets else None,
                "scope_type": "file",
                "grant_kind": "session",
                "digest": rec.get("digest", ""),
            }
        )
    # Wake the awaiting turn (same process) and notify the UI.
    try:
        entry = gw._pending_approvals.pop(request_id, None)
        fut = entry[0] if isinstance(entry, tuple) else entry
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


async def grant_full_default(session) -> bool:
    """Auto-grant full mode for a NEW session under an explicit global default.

    LP-07/13: a persistent `full` default is disclosed consent for future
    sessions. TUI `/mode full` (session scope) never touches the default;
    per-session activation stays the authority everywhere else.
    """
    try:
        if (config.get("execution_mode") or "ask").lower() != "full":
            return False
    except Exception:
        return False
    from ah.permissions import store as _store

    await _store.save_grant(
        {
            "session_id": str(session.id),
            "agent_id": session.agent_id,
            "mode": "full",
            "capability": "session",
            "scope_path": None,
            "scope_type": "file",
            "grant_kind": "session",
            "digest": "full-mode-session",
        }
    )
    return True
