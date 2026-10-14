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
(see machina.permissions.policy.decide): a session override of ``full`` without
its session full-mode grant executes nothing.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token

# Stream B seam: module-level broker import. Cycle-safe by construction —
# machina.permissions.broker's module-level closure is
# {observability.diagnostics, permissions.store, permissions.policy}, none of
# which import core.session_mode / core.execution_context (locked by
# tests/test_architecture_pairs.py). All turn-id reads go through the single
# _broker_turn() seam below; nothing else in this module touches the broker.
from machina.permissions import broker as _permissions_broker

MODES = ("ask", "workspace", "sandbox", "full")

_lock = threading.Lock()
_session_modes: dict[str, str] = {}


# Turn-scoped mode snapshot (Stream A cost seam). The agent tool loop
# re-resolves execution mode on EVERY tool call; resolving once per turn and
# carrying the value in a ContextVar collapses N durable reads to one. The
# scope is entered explicitly at turn entry (agent run/run_stream,
# orchestrator delegate) via turn_scope(); resolve_effective_mode consults it
# and falls back to the broker turn id when no explicit scope is active.
# Broker per-guard immutable snapshots are untouched (separate seam).
#
# M1: the ContextVar holds an immutable snapshot (copy-on-write). Child tasks
# created inside the turn inherit the snapshot by reference, never a mutable
# dict. Every invalidate_turn_mode() bumps a process-global generation; reads
# only serve a snapshot written at the current generation, so a task holding
# a pre-invalidation snapshot re-resolves instead of serving a stale mode
# (even after the turn ended in its parent context).
class _TurnModeSnapshot:
    """Immutable per-context mode snapshot taken at ``generation``."""

    __slots__ = ("generation", "modes")

    def __init__(self, generation: int, modes: dict[str, str] | None = None) -> None:
        self.generation = generation
        self.modes = dict(modes or {})

    def get(self, session_id: str) -> str | None:
        return self.modes.get(session_id)


_turn_scope: ContextVar[_TurnModeSnapshot | None] = ContextVar("turn_mode_scope", default=None)
_turn_generation = 0

# Implicit per-(turn, session) mode cache for callers running under a broker
# turn without an explicit scope (transports set the turn id before agent.run).
# Bounded; entries are keyed by unique turn ids so a new turn never reads stale
# state. Only populated on the durable (DB-connected) path — the cheap sync
# fallback stays exactly as-is.
# Trust assumption: broker turn ids are 48-bit-random per turn, so (turn, session) keys never collide across turns; a miss simply falls through to a fresh DB re-read.
_implicit_modes: dict[tuple[str, str], str] = {}
_IMPLICIT_MODE_CAP = 1024


def turn_scope() -> Iterator[None]:
    """Explicit turn scope: mode resolves once per turn inside the block."""
    return _turn_scope_manager()


@contextmanager
def _turn_scope_manager() -> Iterator[None]:
    token = begin_turn_scope()
    try:
        yield
    finally:
        end_turn_scope(token)


def begin_turn_scope() -> Token:
    """Enter a turn scope in the current task; pair with end_turn_scope."""
    with _lock:
        generation = _turn_generation
    return _turn_scope.set(_TurnModeSnapshot(generation))


def end_turn_scope(token: Token) -> None:
    """Leave a turn scope entered with begin_turn_scope.

    Async generators can be closed from a different task context than the one
    that entered the scope (client disconnect on per-step stream drivers); a
    cross-context reset raises ValueError in CPython. The scope's original
    context is tearing down in that case, so the reset is skipped.
    """
    try:
        _turn_scope.reset(token)
    except ValueError:
        pass


def _valid_turn_scope() -> _TurnModeSnapshot | None:
    """Current context's snapshot, or None when absent/invalidated."""
    scope = _turn_scope.get()
    if scope is None:
        return None
    with _lock:
        current = _turn_generation
    return scope if scope.generation == current else None


def peek_turn_mode(session_id: str | None) -> str | None:
    """Cached mode for this turn, or None when unresolved/uncached."""
    if not session_id:
        return None
    scope = _valid_turn_scope()
    if scope is not None:
        cached = scope.get(str(session_id))
        if cached is not None:
            return cached
    turn = _broker_turn()
    if turn:
        with _lock:
            return _implicit_modes.get((turn, str(session_id)))
    return None


def invalidate_turn_mode(session_id: str | None) -> None:
    """Drop cached mode for a session (any mode change must call this).

    Bumps the global turn-cache generation so snapshots inherited by child
    tasks (possibly after the turn ended in the caller's context) can no
    longer serve a stale mode.
    """
    global _turn_generation
    sid = str(session_id) if session_id else None
    with _lock:
        _turn_generation += 1
        if sid:
            for key in [k for k in _implicit_modes if k[1] == sid]:
                _implicit_modes.pop(key, None)


def _broker_turn() -> str:
    """Current broker turn id, or "" outside a turn / when unavailable.

    THE single broker-turn seam in this module (Stream B): every turn-scoped
    read (peek, implicit cache, durable resolve) goes through here. Prefers
    the ``broker.get_current_turn()`` accessor when present, falling back to
    the ``broker._turn`` ContextVar otherwise. Never raises — failures yield
    "" so callers bypass the turn cache (fail-closed, never poisoned).
    """
    accessor = getattr(_permissions_broker, "get_current_turn", None)
    if callable(accessor):
        try:
            return str(accessor() or "")
        except Exception:
            return ""
    try:
        return str(_permissions_broker._turn.get() or "")
    except Exception:
        return ""


def _cache_turn_mode(session_id: str, mode: str, *, durable: bool) -> None:
    sid = str(session_id)
    scope = _turn_scope.get()
    if scope is not None:
        # Copy-on-write: replace the snapshot instead of mutating the object
        # shared with child task contexts. A generation bump between checks
        # means the inherited entries are stale: start from an empty base.
        with _lock:
            generation = _turn_generation
            base = scope.modes if scope.generation == generation else {}
            _turn_scope.set(_TurnModeSnapshot(generation, {**base, sid: mode}))
    turn = _broker_turn() if durable else ""
    if turn:
        with _lock:
            if len(_implicit_modes) >= _IMPLICIT_MODE_CAP:
                _implicit_modes.pop(next(iter(_implicit_modes)), None)
            _implicit_modes[(turn, sid)] = mode


def set_session_mode(session_id: str, mode: str) -> None:
    """Record an explicit per-session mode (does not touch global config)."""
    normalized = str(mode or "").strip().lower()
    if normalized not in MODES:
        raise ValueError("mode must be ask|workspace|sandbox|full")
    with _lock:
        _session_modes[str(session_id)] = normalized
    invalidate_turn_mode(session_id)


def clear_session_mode(session_id: str) -> None:
    """Remove a session override (session falls back to the global default)."""
    with _lock:
        _session_modes.pop(str(session_id), None)
    invalidate_turn_mode(session_id)


def get_session_mode(session_id: str | None) -> str | None:
    """Return the explicit session override, or None when unset."""
    if not session_id:
        return None
    with _lock:
        return _session_modes.get(str(session_id))


def get_global_default() -> str:
    """Return the process-global default mode (explicit future-session default)."""
    try:
        from machina.core.config import config

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

    Turn-scoped: inside turn_scope() (or a broker turn on the durable path)
    the first successful resolution is reused for the rest of the turn, so N
    tool calls cost one durable read. Failures are never cached.
    """
    from machina.db.connection import db

    sid = str(session_id) if session_id else None
    if sid is not None:
        scope = _valid_turn_scope()
        if scope is not None:
            cached = scope.get(sid)
            if cached is not None:
                return cached
        if scope is None and db.connected:
            turn = _broker_turn()
            if turn:
                with _lock:
                    hit = _implicit_modes.get((turn, sid))
                if hit is not None:
                    return hit

    if not session_id or not db.connected:
        mode = get_effective_mode(session_id)
        if sid is not None:
            _cache_turn_mode(sid, mode, durable=False)
        return mode
    row = await db.fetchrow(
        "SELECT execution_mode FROM sessions WHERE id = $1", uuid.UUID(str(session_id))
    )
    if row is None:
        raise ValueError("session not found while resolving execution mode")
    mode = row["execution_mode"]
    set_session_mode(str(session_id), mode)
    _cache_turn_mode(str(session_id), mode, durable=True)
    return mode


async def activate_durable_mode(session_id: str, agent_id: str, mode: str) -> int:
    """Serialize routing and full-grant changes in one row-locked transaction."""
    from machina.db.connection import db, parse_command_count
    from machina.permissions import store

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
    # Raw-SQL revocation above bypasses store.revoke_grants: drop the
    # per-turn grant cache explicitly (save_grant already does for full).
    store.invalidate_grant_cache(session_id)
    from machina.core.session import session_manager

    await session_manager._cache_invalidate(sid)
    return revoked


def reset_for_tests() -> None:
    """Clear all session overrides (tests only)."""
    global _turn_generation
    with _lock:
        _session_modes.clear()
        _implicit_modes.clear()
        _turn_generation = 0
    _turn_scope.set(None)
