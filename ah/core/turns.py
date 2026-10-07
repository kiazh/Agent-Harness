"""Session execution/mutation coordinator (AH-010, AH-017; Phase B/6; 5.3).

Single shared coordinator for gateway submit/compress/delete, REST prompt/
delete, scheduler jobs and compression.

Design (5.3 corrective):
- Business ``sessions.status`` (active/idle/archived) NEVER carries execution
  state. Live ownership lives in ``claim_owner / claim_kind / claim_expires_at``.
- Local mutexes (per-process asyncio locks) are NOT ownership: they only
  serialize same-process claim attempts. Cross-process truth is the claim row:
  ``claim_owner IS NULL OR claim_expires_at <= now()`` means free.
- Owner tokens: ``try_begin_turn`` / ``begin_mutation`` return an opaque
  token; ``end_turn`` / ``end_mutation`` release ONLY on token equality. A
  stale owner can never release a newer owner's claim.
- Crash recovery: claims expire (turn TTL covers the turn timeout + margin).
  A dead worker's claim becomes reclaimable without manual repair.
- No SQL transaction/connection is held across a model stream or a human
  approval wait — claims are single-statement UPDATEs; waits hold only the
  in-process task + logical ownership.
- Nested delegation never deadlocks: children run on their own sessions;
  parent-context recording uses direct inserts, never the parent's claim.

Backwards-compatible module functions delegate to SessionTurnCoordinator.
"""

from __future__ import annotations

import asyncio
import threading as _threading
import uuid
from contextlib import asynccontextmanager

TURN_TTL_SECONDS = 360
MUTATION_TTL_SECONDS = 120

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
    """Separate in-process mutex for mutations (never confused with ownership)."""
    key = str(session_id)
    with _guard:
        lock = _mutation_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _mutation_locks[key] = lock
        return lock


def _live_claim_where() -> str:
    return "(claim_owner IS NULL OR claim_expires_at IS NULL OR claim_expires_at <= now())"


async def _db_claim(session_id: uuid.UUID, owner: str, kind: str, ttl_s: int) -> bool:
    """Atomic single-statement claim. True iff this owner now holds it."""
    from ah.db.connection import db

    row = await db.fetchrow(
        f"""
        UPDATE sessions SET claim_owner = $2, claim_kind = $3,
            claim_expires_at = now() + ($4 * interval '1 second')
        WHERE id = $1 AND {_live_claim_where()}
        RETURNING id
        """,
        session_id,
        owner,
        kind,
        ttl_s,
    )
    return row is not None


async def _db_release(session_id: uuid.UUID, owner: str) -> bool:
    """Owner-matched release. False for stale/foreign owners (no effect)."""
    from ah.db.connection import db

    result = await db.execute(
        "UPDATE sessions SET claim_owner = NULL, claim_kind = NULL, claim_expires_at = NULL "
        "WHERE id = $1 AND claim_owner = $2",
        session_id,
        owner,
    )
    from ah.db.connection import parse_command_count

    return parse_command_count(result) > 0


async def _db_claim_state(session_id: uuid.UUID) -> dict | None:
    from ah.db.connection import db

    try:
        row = await db.fetchrow(
            "SELECT claim_owner, claim_kind, claim_expires_at FROM sessions WHERE id = $1",
            session_id,
        )
    except Exception:
        return None
    if row is None:
        return None
    return dict(row)


async def _live_claim(session_id: uuid.UUID, kinds: tuple[str, ...]) -> bool:
    """True when a live (unexpired) claim of any of *kinds* exists (any process)."""
    from ah.db.connection import db

    if not db.connected:
        return False
    state = await _db_claim_state(session_id)
    if not state or not state.get("claim_owner"):
        return False
    if kinds and state.get("claim_kind") not in kinds:
        return False
    exp = state.get("claim_expires_at")
    if exp is None:
        return True
    try:
        from datetime import UTC, datetime

        now = datetime.now(UTC)
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=UTC)
        if exp <= now:
            return False
    except Exception:
        return True
    return True


async def _acquire_or_steal(
    lock: asyncio.Lock, session_id: uuid.UUID, kinds: tuple[str, ...]
) -> bool:
    """Acquire *lock*, stealing it when no live blocking claim backs it.

    A locked mutex with no live DB claim is stale (holder dead or expired):
    release and take it. A locked mutex WITH a live claim means a real owner
    holds the session — return False. DB-unavailable degrades to plain mutex
    semantics (fail closed: locked means busy).
    """
    from ah.db.connection import db

    if not lock.locked():
        await lock.acquire()
        return True
    if not db.connected:
        return False
    try:
        if await _live_claim(session_id, kinds):
            return False
    except Exception:
        return False
    try:
        lock.release()
    except RuntimeError:
        pass
    await lock.acquire()
    return True


async def try_begin_turn(
    session_id: uuid.UUID, *, turn_id: str | None = None, ttl_s: int = TURN_TTL_SECONDS
) -> str | None:
    """Claim a turn; returns the owner token, or None when busy.

    Fails closed: DB errors (other than provable unavailability) propagate
    instead of degrading into concurrent execution. A new turn is blocked by
    any live claim (turn or mutation).
    """
    from ah.db.connection import db

    lock = _session_lock(session_id)
    if not await _acquire_or_steal(lock, session_id, ("turn", "mutation")):
        return None
    owner = f"turn:{turn_id or uuid.uuid4().hex}:{uuid.uuid4().hex}"
    try:
        if db.connected:
            claimed = await _db_claim(session_id, owner, "turn", ttl_s)
            if not claimed:
                try:
                    lock.release()
                except RuntimeError:
                    pass
                return None
            return owner
        return owner  # DB-less (tests): in-process lock is the claim.
    except Exception:
        try:
            lock.release()
        except RuntimeError:
            pass
        raise


async def end_turn(session_id: uuid.UUID, token: str | None) -> bool:
    """Release a turn claim; only the exact owner succeeds (stale-safe)."""
    from ah.db.connection import db

    if not token:
        return False
    ok = True
    if db.connected:
        try:
            ok = await _db_release(session_id, token)
        except Exception:
            ok = False
    lock = _session_lock(session_id)
    if lock.locked():
        try:
            lock.release()
        except RuntimeError:
            pass
    return ok


def turn_locked(session_id: uuid.UUID) -> bool:
    """Best-effort in-process check (fast path only, not ownership proof)."""
    lock = _locks.get(str(session_id))
    return bool(lock and lock.locked())


async def turn_active(session_id: uuid.UUID, kinds: tuple[str, ...] = ("turn",)) -> bool:
    """True when a live (unexpired) claim of *kinds* exists (any process).

    Defaults to turn claims only, so a caller holding its own mutation slot
    never rejects itself (Pattern D). In-process locks are not consulted:
    mutex occupancy is not ownership evidence.
    """
    from ah.db.connection import db

    if db.connected:
        try:
            return await _live_claim(session_id, kinds)
        except Exception:
            return turn_locked(session_id)
    return turn_locked(session_id)


async def begin_mutation(session_id: uuid.UUID, *, ttl_s: int = MUTATION_TTL_SECONDS) -> str | None:
    """Claim a mutation slot; fails when a live turn owns the session."""
    from ah.db.connection import db

    lock = _mutation_session_lock(session_id)
    if not await _acquire_or_steal(lock, session_id, ("turn", "mutation")):
        return None
    owner = f"mutation:{uuid.uuid4().hex}"
    try:
        if db.connected:
            claimed = await _db_claim(session_id, owner, "mutation", ttl_s)
            if not claimed:
                try:
                    lock.release()
                except RuntimeError:
                    pass
                return None
            return owner
        return owner
    except Exception:
        try:
            lock.release()
        except RuntimeError:
            pass
        raise


async def end_mutation(session_id: uuid.UUID, token: str | None) -> bool:
    from ah.db.connection import db

    if not token:
        return False
    ok = True
    if db.connected:
        try:
            ok = await _db_release(session_id, token)
        except Exception:
            ok = False
    lock = _mutation_session_lock(session_id)
    if lock.locked():
        try:
            lock.release()
        except RuntimeError:
            pass
    return ok


@asynccontextmanager
async def mutation_lock(session_id: uuid.UUID):
    """Serialize same-process mutations (mutex only; ownership via begin_mutation)."""
    lock = _mutation_session_lock(session_id)
    async with lock:
        yield


class TurnOwnership:
    """Logical turn slot with approval-aware waiting (Phase 6).

    Holds the owner token + in-process slot for the whole turn, including
    permission waits. Never holds a DB transaction or pool connection across
    the turn or across human approval waits.
    """

    def __init__(self, session_id: uuid.UUID, turn_id: str, token: str | None = None) -> None:
        self.session_id = session_id
        self.turn_id = turn_id
        self.token = token
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

    async def begin_turn(self, session_id: uuid.UUID, **kw) -> str | None:
        return await try_begin_turn(session_id, **kw)

    async def finish_turn(self, session_id: uuid.UUID, token: str | None) -> bool:
        return await end_turn(session_id, token)

    def locked(self, session_id: uuid.UUID) -> bool:
        return turn_locked(session_id)

    async def active(self, session_id: uuid.UUID) -> bool:
        return await turn_active(session_id)

    def mutation(self, session_id: uuid.UUID):
        return mutation_lock(session_id)


session_turn_coordinator = SessionTurnCoordinator()
