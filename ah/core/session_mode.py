"""Session-local execution-mode registry (AH-AUDIT-001).

A session's effective mode is resolved as:

    session override (explicit per-session activation) → global default

Activating full/ask/workspace/sandbox for ONE session never mutates the
process-global default, so a temporary session activation can never be
interpreted as consent for future sessions. Only an explicit
``scope=global`` change (plus the pre-existing persistent-default path)
touches the global default, and only subsequently created sessions inherit
it via ``grant_full_default``.

Thread-safe in-memory registry. Session overrides are authority state for
routing only — actual authorization still requires live permission grants
(see ah.permissions.policy.decide): a session override of ``full`` without
its session full-mode grant executes nothing.
"""

from __future__ import annotations

import threading
import uuid

MODES = ("ask", "workspace", "sandbox", "full")

_lock = threading.Lock()
_session_modes: dict[str, str] = {}


def set_session_mode(session_id: str, mode: str) -> None:
    """Record an explicit per-session mode (does not touch global config)."""
    normalized = str(mode or "").strip().lower()
    if normalized not in MODES:
        raise ValueError("mode must be ask|workspace|sandbox|full")
    with _lock:
        _session_modes[str(session_id)] = normalized


def clear_session_mode(session_id: str) -> None:
    """Remove a session override (session falls back to the global default)."""
    with _lock:
        _session_modes.pop(str(session_id), None)


def get_session_mode(session_id: str | None) -> str | None:
    """Return the explicit session override, or None when unset."""
    if not session_id:
        return None
    with _lock:
        return _session_modes.get(str(session_id))


def get_global_default() -> str:
    """Return the process-global default mode (explicit future-session default)."""
    try:
        from ah.core.config import config

        return str(config.get("execution_mode") or "ask").lower()
    except Exception:
        return "ask"


def get_effective_mode(session_id: str | None = None) -> str:
    """Resolve the effective mode for a session (override → global)."""
    override = get_session_mode(session_id) if session_id else None
    if override:
        return override
    return get_global_default()


async def resolve_effective_mode(session_id: str | None = None) -> str:
    """Read durable authority fresh; database failures never use cached mode.

    The synchronous registry supports display and explicitly DB-less execution.
    Production proposal construction calls this resolver before broker stamping.
    """
    from ah.db.connection import db

    if not session_id or not db.connected:
        return get_effective_mode(session_id)
    row = await db.fetchrow(
        "SELECT execution_mode FROM sessions WHERE id = $1", uuid.UUID(str(session_id))
    )
    if row is None:
        raise ValueError("session not found while resolving execution mode")
    mode = row["execution_mode"]
    set_session_mode(str(session_id), mode)
    return mode


async def activate_durable_mode(session_id: str, agent_id: str, mode: str) -> int:
    """Serialize routing and full-grant changes in one row-locked transaction."""
    from ah.db.connection import db, parse_command_count
    from ah.permissions import store

    if mode not in MODES:
        raise ValueError("mode must be ask|workspace|sandbox|full")
    sid = uuid.UUID(session_id)
    async with db.acquire() as connection:
        async with connection.transaction():
            row = await connection.fetchrow("SELECT id FROM sessions WHERE id = $1 FOR UPDATE", sid)
            if row is None:
                raise ValueError("session not found while activating execution mode")
            revoked = parse_command_count(
                await connection.execute(
                    "UPDATE permission_grants SET revoked = TRUE "
                    "WHERE session_id = $1 AND revoked = FALSE",
                    sid,
                )
            )
            if mode == "full":
                await store.save_grant(
                    {
                        "session_id": session_id,
                        "agent_id": agent_id,
                        "mode": "full",
                        "capability": "session",
                        "scope_path": None,
                        "scope_type": "file",
                        "grant_kind": "session",
                        "digest": "full-mode-session",
                    },
                    connection=connection,
                )
            await connection.execute(
                "UPDATE sessions SET execution_mode = $2, last_activity = now() WHERE id = $1",
                sid,
                mode,
            )
    set_session_mode(session_id, mode)
    from ah.core.session import session_manager

    await session_manager._cache_invalidate(sid)
    return revoked


def reset_for_tests() -> None:
    """Clear all session overrides (tests only)."""
    with _lock:
        _session_modes.clear()
