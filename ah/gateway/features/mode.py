"""Mode + approval feature handlers (Phase D/E)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ah.core.config import config
from ah.gateway.errors import INVALID_PARAMS, RpcError
from ah.observability.diagnostics import record_failure

if TYPE_CHECKING:
    from ah.gateway.server import Gateway

from ah.gateway.features._common import _str


async def mode_get(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    gw.require_db()
    from ah.core.session_mode import get_session_mode, resolve_effective_mode

    session_id = params.get("sessionId")
    if session_id:
        effective = await resolve_effective_mode(str(session_id))
        return {
            "mode": effective,
            "session_mode": get_session_mode(str(session_id)),
            "default": config.get("execution_mode"),
            "backend": _backend(str(session_id)),
        }
    return {"mode": config.get("execution_mode"), "backend": _backend()}


async def mode_set(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Explicit mode activation (AH-AUDIT-001).

    scope=session (default): session-local only — never mutates the global
    default, so future sessions are unaffected. Full activation records a
    session-scoped grant AFTER the session override is staged; a grant-write
    failure leaves no partial elevation (override is rolled back).
    scope=global: explicit persistent-default change affecting subsequently
    created sessions only.
    """
    gw.require_db()
    from ah.core.session_mode import (
        clear_session_mode,
        get_effective_mode,
        set_session_mode,
    )

    mode = _str(params, "mode", max_len=20).lower()
    if mode not in ("ask", "workspace", "sandbox", "full"):
        raise RpcError(INVALID_PARAMS, "mode must be ask|workspace|sandbox|full")
    scope = str(params.get("scope") or "session").lower()
    if scope not in ("session", "global"):
        raise RpcError(INVALID_PARAMS, "scope must be session|global")
    if mode == "sandbox":
        from ah.core.runtime import runtime_services

        report = runtime_services.status()
        if report.get("sandbox") == "unavailable":
            raise RpcError(
                INVALID_PARAMS,
                "sandbox isolation unavailable (no Docker); staying in ask mode. "
                "Approve host ask mode instead — never a silent downgrade.",
            )
    if scope == "global":
        # Explicit future-session default: global mutation is the point.
        # Existing sessions keep their explicit session overrides.
        config.set("execution_mode", mode, persist=bool(params.get("persist", False)))
        return {"mode": mode, "scope": scope, "backend": _backend()}

    # Session-local activation: never touch the global default.
    from ah.permissions import store as _store

    session = await gw.get_session(params) if params.get("sessionId") else None
    if session is None:
        raise RpcError(INVALID_PARAMS, "sessionId is required for session-scoped mode")
    sid = str(session.id)
    from ah.db.connection import db

    if db.connected:
        from ah.core.session_mode import activate_durable_mode

        await activate_durable_mode(sid, session.agent_id, mode)
        return {"mode": mode, "scope": scope, "backend": _backend(sid)}
    previous = get_effective_mode(sid)
    if mode == "full":
        # Stage the override first so policy sees the intended mode, then
        # persist the grant that actually authorizes it. Roll back the
        # override when the grant write fails: no partial elevation.
        set_session_mode(sid, mode)
        try:
            await _store.save_grant(
                {
                    "session_id": sid,
                    "agent_id": session.agent_id,
                    "mode": "full",
                    "capability": "session",
                    "scope_path": None,
                    "scope_type": "file",
                    "grant_kind": "session",
                    "digest": "full-mode-session",
                }
            )
        except Exception:
            # Atomicity: failure leaves no elevation behind.
            try:
                if previous == config.get("execution_mode"):
                    clear_session_mode(sid)
                else:
                    set_session_mode(sid, previous)
            except Exception:
                clear_session_mode(sid)
            raise
    else:
        # Leaving full (or switching ask/workspace/sandbox): revoke the
        # session's full-mode grant, then record the session override.
        # Revocation failure must not leave full authority active under a
        # non-full label: keep the full override and surface the error.
        from ah.permissions.broker import permission_broker

        try:
            await permission_broker.revoke(sid)
        except Exception:
            set_session_mode(sid, "full")
            raise
        set_session_mode(sid, mode)
    return {"mode": mode, "scope": scope, "backend": _backend(sid)}


async def mode_revoke(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Revoke SESSION grants only (LP-07); the global default is untouched.

    Pass scope=global (CLI durable reset) to also reset the persistent default.
    Session scope clears the session override so the session falls back to
    the global default (well-defined switch-back-to-ask behavior).
    """
    gw.require_db()
    from ah.core.session_mode import clear_session_mode, get_effective_mode
    from ah.permissions.broker import permission_broker

    session = await gw.get_session(params)
    from ah.db.connection import db

    if db.connected:
        from ah.core.session_mode import activate_durable_mode

        n = await activate_durable_mode(str(session.id), session.agent_id, "ask")
        if params.get("scope") == "global":
            config.set("execution_mode", "ask")
        return {"revoked": n, "mode": "ask", "scope": params.get("scope", "session")}
    n = await permission_broker.revoke(str(session.id))
    if str(params.get("scope") or "session") == "global":
        config.set("execution_mode", "ask")
        return {"revoked": n, "mode": "ask", "scope": "global"}
    clear_session_mode(str(session.id))
    return {"revoked": n, "mode": get_effective_mode(str(session.id)), "scope": "session"}


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
    if verdict not in ("approved", "denied", "cancelled", "expired"):
        raise RpcError(INVALID_PARAMS, "verdict must be approved|denied|cancelled|expired")
    grant = str(params.get("grant") or "once").lower()
    if grant not in ("once", "session"):
        raise RpcError(INVALID_PARAMS, "grant must be once|session")
    rec = await _store.get_approval(request_id)
    if rec is None:
        raise RpcError(INVALID_PARAMS, "unknown approval")
    bound_session = params.get("sessionId", params.get("session_id"))
    if bound_session is not None and str(bound_session) != str(rec.get("session_id") or ""):
        raise RpcError(INVALID_PARAMS, "approval does not belong to this session")
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
    except Exception as _boundary_error:
        # Optional fallback preserves the primary outcome; report no payload.
        record_failure("mode.approvals_resolve", _boundary_error)
    try:
        gw._write(
            {
                "jsonrpc": "2.0",
                "method": "event",
                "params": gw.event_payload(
                    "approval.resolved",
                    str(rec.get("session_id") or ""),
                    str(rec.get("turn_id") or ""),
                    requestId=request_id,
                    status=verdict,
                ),
            }
        )
    except Exception as _boundary_error:
        # Optional fallback preserves the primary outcome; report no payload.
        record_failure("mode.approvals_resolve", _boundary_error)
    return {"approval": {"requestId": request_id, "status": verdict}}


def _backend(session_id: str | None = None) -> str:
    try:
        from ah.core.session_mode import get_effective_mode

        if get_effective_mode(session_id) == "sandbox":
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
