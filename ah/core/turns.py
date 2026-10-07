"""Session execution/mutation coordinator (AH-010, AH-017).

Single shared coordinator for gateway submit/compress/delete, REST prompt/
delete, scheduler jobs and compression. Per-process asyncio locks close the
check-then-act race within one process; cross-process safety comes from an
atomic DB status claim (sessions.status running) so two workers (gateway
stdio child vs uvicorn workers) cannot run concurrent turns on one session.

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

_guard = _threading.Lock()


def _session_lock(session_id: uuid.UUID) -> asyncio.Lock:
    key = str(session_id)
    with _guard:
        lock = _locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _locks[key] = lock
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
    """Serialize compress/delete against turn begin/end in this process."""
    lock = _session_lock(session_id)
    async with lock:
        yield
