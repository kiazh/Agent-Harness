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


def reset_for_tests() -> None:
    """Clear all session overrides (tests only)."""
    with _lock:
        _session_modes.clear()
