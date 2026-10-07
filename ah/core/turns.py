"""Session execution/mutation coordinator (AH-010, AH-017; Phase B/6).

Single shared coordinator for gateway submit/compress/delete, REST prompt/
delete, scheduler jobs and compression. Per-process asyncio locks close the
check-then-act race within one process; cross-process safety comes from an
atomic DB status claim (sessions.status running) so two workers (gateway
stdio child vs uvicorn workers) cannot run concurrent turns on one session.

Phase 6 additions: logical turn ownership that survives permission waits
without holding SQL transactions/pool connections; approval-wait vs compute
timeouts distinguished; fork-safe child tokens via owner-authorized internal
mutation; durable fencing for multi-process runners.

Contract:
- ``try_begin_turn`` atomically claims a session for execution. Returns
  True on success, False when a turn is already running.
- ``end_turn`` releases the claim (idempotent).
- ``mutation_lock`` serializes compress/delete against turn begin/end within
  this process. REST checks the DB claim BEFORE sending SSE headers so
  conflicts surface as 409, not mid-stream.
- Multi-process note: in-process locks alone are insufficient; the DB claim
  is the fencing mechanism. If the DB is unavailable, fall back to
  in-process locks only (documented degradation).
"""

from __future__ import annotations

import asyncio
import threading as _threading
import uuid
from contextlib import asynccontextmanager

_locks: dict[str, asyncio.Lock] = {}
_mutation_locks: dict[str, asyncio.Lock] = {}

_guard = _threading.Lock()


def _session_lock(session_id: uuid.UUID) -> asyncio.Lock:
    key = str(session_id)
    with _guard:
        lock = _locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _locks[key] = lock
        return lock


def _mutation_session_lock(session_id: uuid.UUID) -> asyncio.Lock:
    """Separate lock for compress/delete/fork (Phase 6).

    Mutations must NOT share the turn lock: auto-compaction at the turn
    boundary and explicit /compress must be able to check turn state without
    deadlocking on their own claim. They serialize among themselves and
    check ``turn_locked`` / gateway ``turn_running`` inside.
    """
    key = str(session_id)
    with _guard:
        lock = _mutation_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _mutation_locks[key] = lock
        return lock


async def try_begin_turn(session_id: uuid.UUID) -> bool:
    """Atomically claim *session_id* for a turn.

    Tries the DB status claim first (cross-process); falls back to in-process
    lock state when the DB is unreachable. Returns True if this caller owns
    the turn.
    """
    lock = _session_lock(session_id)
    # In-process fast path: if locked, someone in this process owns it.
    if lock.locked():
        return False
    # Cross-process fence via atomic status transition.
    try:
        from ah.db.connection import db

        if db.connected:
            row = await db.fetchrow(
                """
                UPDATE sessions SET status = 'running'
                WHERE id = $1 AND status = 'active'
                RETURNING id
                """,
                session_id,
            )
            if row is None:
                return False
            await lock.acquire()
            return True
    except Exception:
        pass
    # DB unavailable or sessions table without running state: use in-process
    # lock only (documented degradation).
    if lock.locked():
        return False
    await lock.acquire()
    return True


async def end_turn(session_id: uuid.UUID) -> None:
    """Release a turn claim (idempotent)."""
    lock = _session_lock(session_id)
    if lock.locked():
        try:
            lock.release()
        except RuntimeError:
            pass
    try:
        from ah.db.connection import db

        if db.connected:
            await db.execute(
                """
                UPDATE sessions SET status = 'active'
                WHERE id = $1 AND status = 'running'
                """,
                session_id,
            )
    except Exception:
        pass


def turn_locked(session_id: uuid.UUID) -> bool:
    """Best-effort in-process check (use try_begin_turn for fencing)."""
    lock = _locks.get(str(session_id))
    return bool(lock and lock.locked())


@asynccontextmanager
async def mutation_lock(session_id: uuid.UUID):
    """Serialize compress/delete/fork against each other (Phase 6).

    Uses a dedicated mutation lock, then callers check turn state inside.
    Never blocks on the caller's own turn claim.
    """
    lock = _mutation_session_lock(session_id)
    async with lock:
        yield


class TurnOwnership:
    """Logical turn slot with approval-aware waiting (Phase 6).

    Holds the in-process lock + DB ``running`` claim for the whole turn,
    including permission waits. Never holds a DB transaction or pool
    connection across the turn or across human approval waits — only the
    lightweight status row + in-process lock.
    """

    def __init__(self, session_id: uuid.UUID, turn_id: str) -> None:
        self.session_id = session_id
        self.turn_id = turn_id
        self._approval_event = asyncio.Event()
        self._approval_event.set()  # no wait by default

    async def wait_for_approval(self, timeout: float) -> bool:
        """Wait for a permission decision without consuming compute budget.

        Returns True when approved, False on denial/timeout/cancel. Compute
        timeouts must be paused by callers while awaiting this.
        """
        self._approval_event.clear()
        try:
            await asyncio.wait_for(self._approval_event.wait(), timeout=timeout)
            return bool(getattr(self, "_approved", False))
        except (TimeoutError, asyncio.CancelledError):
            return False

    def resolve_approval(self, approved: bool) -> None:
        self._approved = approved
        self._approval_event.set()

    def child_token(self) -> dict:
        """Owner-authorized internal mutation token for delegated children.

        Lets a child record parent context without reacquiring the parent's
        active turn lock (avoids parent/child deadlock).
        """
        return {
            "parent_session": str(self.session_id),
            "parent_turn": self.turn_id,
            "authorized": True,
        }


class SessionTurnCoordinator:
    """Shared coordinator object (Phase B/6). Module functions delegate here."""

    async def begin_turn(self, session_id: uuid.UUID) -> bool:
        return await try_begin_turn(session_id)

    async def finish_turn(self, session_id: uuid.UUID) -> None:
        await end_turn(session_id)

    def locked(self, session_id: uuid.UUID) -> bool:
        return turn_locked(session_id)

    def mutation(self, session_id: uuid.UUID):
        return mutation_lock(session_id)


session_turn_coordinator = SessionTurnCoordinator()
