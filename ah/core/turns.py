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

from ah.observability.diagnostics import record_failure

TURN_TTL_SECONDS = 360
MUTATION_TTL_SECONDS = 120

_locks: dict[str, asyncio.Lock] = {}
_mutation_locks: dict[str, asyncio.Lock] = {}

_guard = _threading.Lock()

# Local ownership registry (AH-AUDIT-016/017): one token-aware record per
# session covering BOTH turn and mutation claims. Logical ownership is
# separate from the mutexes below; release requires token equality so a
# stale owner can never unlock a newer owner. DB-less (tests/degraded) mode
# enforces the same single-owner rule in-process (no cross-process claim).
_local_claims: dict[str, dict] = {}


def _local_try_claim(session_id: object, kind: str, token: str) -> bool:
    """Claim local ownership when no live local claim exists (either kind)."""
    with _guard:
        existing = _local_claims.get(str(session_id))
        if existing is not None:
            return False
        _local_claims[str(session_id)] = {"kind": kind, "token": token}
        return True


def _local_release(session_id: object, token: str | None) -> bool:
    """Release local ownership only for the matching owner token."""
    if not token:
        return False
    with _guard:
        existing = _local_claims.get(str(session_id))
        if existing is None or existing.get("token") != token:
            return False
        _local_claims.pop(str(session_id), None)
        return True


def _local_owner(session_id: object) -> dict | None:
    with _guard:
        rec = _local_claims.get(str(session_id))
        return dict(rec) if rec is not None else None


def _local_reset_for_tests() -> None:
    with _guard:
        _local_claims.clear()


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
        """,  # nosec B608 # Fixed SQL fragments; external values use bound parameters.
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

    # An unavailable database is unknown ownership, never an absent claim.
    row = await db.fetchrow(
        "SELECT claim_owner, claim_kind, claim_expires_at FROM sessions WHERE id = $1",
        session_id,
    )
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


async def _db_renew(session_id: uuid.UUID, owner: str, ttl_s: int) -> bool:
    """Extend a live claim owned by *owner*. False when expired/stolen."""
    from ah.db.connection import db

    result = await db.execute(
        "UPDATE sessions SET claim_expires_at = now() + ($3 * interval '1 second') "
        "WHERE id = $1 AND claim_owner = $2 AND claim_expires_at > now()",
        session_id,
        owner,
        ttl_s,
    )
    from ah.db.connection import parse_command_count

    return parse_command_count(result) > 0


async def renew_turn(
    session_id: uuid.UUID, token: str | None, ttl_s: int = TURN_TTL_SECONDS
) -> bool:
    """Renew a live turn claim (AH-AUDIT-013). False on loss/expiry."""
    if not token:
        return False
    from ah.db.connection import db

    if not db.connected:
        # DB-less: ownership is the local registry entry.
        owner = _local_owner(session_id)
        return bool(owner is not None and owner.get("token") == token)
    try:
        return await _db_renew(session_id, token, ttl_s)
    except Exception:
        return False


async def renew_mutation(
    session_id: uuid.UUID, token: str | None, ttl_s: int = MUTATION_TTL_SECONDS
) -> bool:
    """Renew a live mutation claim (AH-AUDIT-013). False on loss/expiry."""
    return await renew_turn(session_id, token, ttl_s=ttl_s)


async def owns_claim(session_id: uuid.UUID, token: str | None) -> bool:
    """True when *token* still holds a live claim (loss-of-ownership check)."""
    if not token:
        return False
    from ah.db.connection import db

    if not db.connected:
        owner = _local_owner(session_id)
        return bool(owner is not None and owner.get("token") == token)
    try:
        state = await _db_claim_state(session_id)
    except Exception:
        return False
    if not state or state.get("claim_owner") != token:
        return False
    exp = state.get("claim_expires_at")
    if exp is None:
        return True
    try:
        from datetime import UTC, datetime

        now = datetime.now(UTC)
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=UTC)
        return exp > now
    except Exception:
        return False


async def try_begin_turn(
    session_id: uuid.UUID, *, turn_id: str | None = None, ttl_s: int = TURN_TTL_SECONDS
) -> str | None:
    """Claim a turn; returns the owner token, or None when busy.

    Fails closed: DB errors (other than provable unavailability) propagate
    instead of degrading into concurrent execution. A new turn is blocked by
    any live claim (turn or mutation). DB-less mode enforces the same
    single-owner rule through the token-aware local registry (AH-AUDIT-016).
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
            if not _local_try_claim(session_id, "turn", owner):
                # A live DB claim is ours, so any local entry is stale
                # (same-process holder died without release): evict only
                # when its token no longer holds a live claim.
                stale = _local_owner(session_id)
                stale_token = (stale or {}).get("token")
                if stale_token and not await owns_claim(session_id, stale_token):
                    _local_release(session_id, stale_token)
                    _local_try_claim(session_id, "turn", owner)
                else:
                    await _db_release(session_id, owner)
                    try:
                        lock.release()
                    except RuntimeError:
                        pass
                    return None
            return owner
        # DB-less: the local registry IS the claim (either kind blocks).
        if not _local_try_claim(session_id, "turn", owner):
            try:
                lock.release()
            except RuntimeError:
                pass
            return None
        return owner
    except Exception:
        try:
            lock.release()
        except RuntimeError:
            pass
        _local_release(session_id, owner)
        raise


async def end_turn(session_id: uuid.UUID, token: str | None) -> bool:
    """Release a turn claim; only the exact owner succeeds (stale-safe).

    AH-AUDIT-017: the local mutex is released only when the caller owns the
    local registry entry. A stale token whose DB release was rejected never
    unlocks a newer owner's local claim. Database errors propagate so callers
    cannot mistake unknown ownership for a successfully retired turn.
    """
    from ah.db.connection import db

    if not token:
        return False
    if db.connected:
        ok = await _db_release(session_id, token)
        if ok:
            # Opportunistically clear our own local entry; a rejected
            # stale token never touches a newer owner's entry.
            _local_release(session_id, token)
            lock = _session_lock(session_id)
            if lock.locked():
                # Release the mutex only when no live local claim backs it.
                if _local_owner(session_id) is None:
                    try:
                        lock.release()
                    except RuntimeError:
                        pass
        return ok
    # DB-less: the token-gated local registry is the claim.
    mine = _local_release(session_id, token)
    if mine:
        lock = _session_lock(session_id)
        if lock.locked():
            try:
                lock.release()
            except RuntimeError:
                pass
        return True
    return False


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
        except Exception as error:
            # Unknown remote ownership blocks mutation until DB recovery/expiry.
            record_failure("turns.turn_active", error)
            return True
    return turn_locked(session_id)


async def begin_mutation(session_id: uuid.UUID, *, ttl_s: int = MUTATION_TTL_SECONDS) -> str | None:
    """Claim a mutation slot; fails when a live turn owns the session.

    DB-less mode uses the shared local registry, so a held turn blocks a
    mutation and vice versa (AH-AUDIT-016).
    """
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
            if not _local_try_claim(session_id, "mutation", owner):
                stale = _local_owner(session_id)
                stale_token = (stale or {}).get("token")
                if stale_token and not await owns_claim(session_id, stale_token):
                    _local_release(session_id, stale_token)
                    _local_try_claim(session_id, "mutation", owner)
                else:
                    await _db_release(session_id, owner)
                    try:
                        lock.release()
                    except RuntimeError:
                        pass
                    return None
            return owner
        if not _local_try_claim(session_id, "mutation", owner):
            try:
                lock.release()
            except RuntimeError:
                pass
            return None
        return owner
    except Exception:
        try:
            lock.release()
        except RuntimeError:
            pass
        _local_release(session_id, owner)
        raise


async def end_mutation(session_id: uuid.UUID, token: str | None) -> bool:
    """Release a mutation claim; stale tokens never unlock newer owners."""
    from ah.db.connection import db

    if not token:
        return False
    if db.connected:
        ok = await _db_release(session_id, token)
        if ok:
            _local_release(session_id, token)
            lock = _mutation_session_lock(session_id)
            if lock.locked() and _local_owner(session_id) is None:
                try:
                    lock.release()
                except RuntimeError:
                    pass
        return ok
    mine = _local_release(session_id, token)
    if mine:
        lock = _mutation_session_lock(session_id)
        if lock.locked():
            try:
                lock.release()
            except RuntimeError:
                pass
        return True
    return False


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

    AH-AUDIT-013: start_renewal() keeps a long turn/approval wait alive with
    periodic renew_claim; renewal failure or detected loss sets
    ownership_lost so owned effects stop. stop_renewal() joins the task
    during release. Crash expiry recovery is preserved (no renewal → the
    claim lapses and becomes reclaimable).
    """

    def __init__(self, session_id: uuid.UUID, turn_id: str, token: str | None = None) -> None:
        self.session_id = session_id
        self.turn_id = turn_id
        self.token = token
        self._approval_event = asyncio.Event()
        self._approval_event.set()  # no wait by default
        self.ownership_lost = asyncio.Event()
        self._renew_task: asyncio.Task | None = None

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

    def start_renewal(self, interval_s: float = 60.0, ttl_s: int = TURN_TTL_SECONDS) -> None:
        """Begin periodic claim renewal; idempotent."""
        if self._renew_task is not None and not self._renew_task.done():
            return
        if not self.token:
            return

        async def _heartbeat() -> None:
            while True:
                await asyncio.sleep(min(interval_s, ttl_s / 3))
                try:
                    ok = await renew_turn(self.session_id, self.token, ttl_s=ttl_s)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    ok = False
                if ok:
                    continue
                self.ownership_lost.set()
                return

        self._renew_task = asyncio.create_task(_heartbeat(), name=f"turn-renew-{self.turn_id}")

    async def stop_renewal(self) -> None:
        """Stop and join the renewal task (called during release)."""
        task, self._renew_task = self._renew_task, None
        if task is None:
            return
        if not task.done():
            task.cancel()
        try:
            await asyncio.gather(task, return_exceptions=True)
        except (asyncio.CancelledError, Exception) as _boundary_error:
            # Optional fallback preserves the primary outcome; report no payload.
            record_failure("turns.stop_renewal", _boundary_error)

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
