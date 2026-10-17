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

Module functions are the only surface: the never-adopted SessionTurnCoordinator
indirection was deleted (review M4)."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading as _threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from machina.core.metrics import metrics
from machina.observability.diagnostics import record_failure
from machina.observability.instrumentation import instrumented, log_event

logger = logging.getLogger(__name__)


def _owner_hash(token: str | None) -> str:
    """Stable, non-reversible owner identity for logs (never the raw token)."""
    if not token:
        return ""
    try:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]
    except Exception:
        return ""

TURN_TTL_SECONDS = 360
MUTATION_TTL_SECONDS = 120

#: Bounded budget for the shielded reconciliation of a durable claim whose
#: acquisition was cancelled (MCH-002): longest bounded wait allowed before
#: ownership is treated as ambiguous and left to stale-claim recovery.
CLAIM_RECONCILE_TIMEOUT_SECONDS = 5.0

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


def _note_lock_contention(site: str) -> None:
    """Observe a stale-mutex release race (control flow unchanged).

    A RuntimeError from lock.release() means another task already released
    (or stole) the mutex: benign contention that was previously swallowed
    silently. Semantics are identical — the exception is still absorbed —
    but each claim/release site now counts its races.
    """
    try:
        metrics.increment_counter("turns.lock_release_contention")
        metrics.increment_counter(f"turns.lock_release_contention.{site}")
    except Exception:
        pass


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
    from machina.db.connection import db

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
    from machina.db.connection import db

    result = await db.execute(
        "UPDATE sessions SET claim_owner = NULL, claim_kind = NULL, claim_expires_at = NULL "
        "WHERE id = $1 AND claim_owner = $2",
        session_id,
        owner,
    )
    from machina.db.connection import parse_command_count

    return parse_command_count(result) > 0


async def _db_claim_state(session_id: uuid.UUID) -> dict | None:
    from machina.db.connection import db

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
    from machina.db.connection import db

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
        now = datetime.now(UTC)
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=UTC)
        if exp <= now:
            return False
    except Exception as _boundary_error:
        # Optional fallback preserves the primary outcome; report no payload.
        record_failure("turns.live_claim.expiry", _boundary_error)
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
    from machina.db.connection import db

    if not lock.locked():
        await lock.acquire()
        return True
    if not db.connected:
        return False
    try:
        if await _live_claim(session_id, kinds):
            return False
    except Exception as _boundary_error:
        # Optional fallback preserves the primary outcome; report no payload.
        record_failure("turns.acquire_or_steal", _boundary_error)
        return False
    try:
        lock.release()
    except RuntimeError:
        _note_lock_contention("acquire_steal")
    await lock.acquire()
    return True


async def _db_renew(session_id: uuid.UUID, owner: str, ttl_s: int) -> bool:
    """Extend a live claim owned by *owner*. False when expired/stolen."""
    from machina.db.connection import db

    result = await db.execute(
        "UPDATE sessions SET claim_expires_at = now() + ($3 * interval '1 second') "
        "WHERE id = $1 AND claim_owner = $2 AND claim_expires_at > now()",
        session_id,
        owner,
        ttl_s,
    )
    from machina.db.connection import parse_command_count

    return parse_command_count(result) > 0


def _observe_reconcile_task(task: asyncio.Task) -> None:
    """Consume a detached reconciliation release so it is never a silent orphan."""
    if not task.cancelled():
        error = task.exception()
        if error is not None:
            record_failure("turns.cancel_reconcile.detached", error)


@instrumented("turns", "reconcile")
async def _reconcile_cancelled_claim(session_id: uuid.UUID, owner: str, kind: str) -> None:
    """Shielded, bounded cleanup for a claim whose acquisition was cancelled.

    MCH-002: cancellation can strike *after* the durable claim committed but
    before any caller received the owner token. The claim is released ONLY
    when it is provably still owned by this call's token (``_db_release``
    matches ``claim_owner = owner`` exactly, so a successor's claim can never
    be released). When ownership cannot be proven (timeout, database error,
    or a second cancellation), the durable claim is LEFT for stale-claim
    recovery rather than released blind, and the uncertainty is recorded.
    A second cancellation propagates; it is never converted to an error.
    """
    release = asyncio.ensure_future(_db_release(session_id, owner))
    try:
        async with asyncio.timeout(CLAIM_RECONCILE_TIMEOUT_SECONDS):
            released = await asyncio.shield(release)
    except asyncio.CancelledError:
        # A stop delivered mid-reconciliation is ownership uncertainty too:
        # keep the token-matched release observed (it may still clear our own
        # claim) and propagate the requested cancellation instead of masking.
        release.add_done_callback(_observe_reconcile_task)
        raise
    except (TimeoutError, Exception) as error:
        release.add_done_callback(_observe_reconcile_task)
        # Unresolved ownership (bounded budget spent or database error):
        # leave the durable claim for stale-claim recovery (expiry), and
        # record enough to make the ambiguity auditable.
        record_failure(f"turns.{kind}.cancel_reconcile", error)
        log_event(
            "turns.reconcile",
            level="warning",
            claim_kind=kind,
            session_id=str(session_id),
            owner_hash=_owner_hash(owner),
            outcome="unknown",
            error_class=type(error).__name__,
        )
        logger.warning(
            "Cancelled %s acquisition for session %s: release outcome unknown; "
            "durable claim left for stale-claim recovery",
            kind,
            session_id,
        )
        return
    if released:
        log_event(
            "turns.reconcile",
            level="debug",
            claim_kind=kind,
            session_id=str(session_id),
            owner_hash=_owner_hash(owner),
            outcome="released",
        )
        logger.info(
            "Cancelled %s acquisition for session %s; released the still-owned claim",
            kind,
            session_id,
        )
    else:
        log_event(
            "turns.reconcile",
            level="debug",
            claim_kind=kind,
            session_id=str(session_id),
            owner_hash=_owner_hash(owner),
            outcome="not_owned",
        )
        logger.info(
            "Cancelled %s acquisition for session %s; claim was already released or taken",
            kind,
            session_id,
        )


async def renew_turn(
    session_id: uuid.UUID, token: str | None, ttl_s: int = TURN_TTL_SECONDS
) -> bool:
    """Renew a live turn claim (AH-AUDIT-013). False on loss/expiry."""
    if not token:
        return False
    from machina.db.connection import db

    if not db.connected:
        # DB-less: ownership is the local registry entry.
        owner = _local_owner(session_id)
        return bool(owner is not None and owner.get("token") == token)
    try:
        return await _db_renew(session_id, token, ttl_s)
    except Exception as _boundary_error:
        # Optional fallback preserves the primary outcome; report no payload.
        record_failure("turns.renew_turn", _boundary_error)
        return False


async def owns_claim(session_id: uuid.UUID, token: str | None) -> bool:
    """True when *token* still holds a live claim (loss-of-ownership check)."""
    if not token:
        return False
    from machina.db.connection import db

    if not db.connected:
        owner = _local_owner(session_id)
        return bool(owner is not None and owner.get("token") == token)
    try:
        state = await _db_claim_state(session_id)
    except Exception as _boundary_error:
        # Optional fallback preserves the primary outcome; report no payload.
        record_failure("turns.owns_claim", _boundary_error)
        return False
    if not state or state.get("claim_owner") != token:
        return False
    exp = state.get("claim_expires_at")
    if exp is None:
        return True
    try:
        now = datetime.now(UTC)
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=UTC)
        return exp > now
    except Exception as _boundary_error:
        # Optional fallback preserves the primary outcome; report no payload.
        record_failure("turns.owns_claim.expiry", _boundary_error)
        return False


@instrumented("turns", "acquire")
async def try_begin_turn(
    session_id: uuid.UUID, *, turn_id: str | None = None, ttl_s: int = TURN_TTL_SECONDS
) -> str | None:
    """Claim a turn; returns the owner token, or None when busy.

    Fails closed: DB errors (other than provable unavailability) propagate
    instead of degrading into concurrent execution. A new turn is blocked by
    any live claim (turn or mutation). DB-less mode enforces the same
    single-owner rule through the token-aware local registry (AH-AUDIT-016).
    """
    from machina.db.connection import db

    lock = _session_lock(session_id)
    if not await _acquire_or_steal(lock, session_id, ("turn", "mutation")):
        log_event(
            "turns.claim",
            level="debug",
            claim_kind="turn",
            session_id=str(session_id),
            outcome="busy",
        )
        return None
    owner = f"turn:{turn_id or uuid.uuid4().hex}:{uuid.uuid4().hex}"
    try:
        if db.connected:
            claimed = await _db_claim(session_id, owner, "turn", ttl_s)
            if not claimed:
                try:
                    lock.release()
                except RuntimeError:
                    _note_lock_contention("begin_turn_claim_busy")
                log_event(
                    "turns.claim",
                    level="debug",
                    claim_kind="turn",
                    session_id=str(session_id),
                    owner_hash=_owner_hash(owner),
                    outcome="busy",
                )
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
                        _note_lock_contention("begin_turn_stale_conflict")
                    log_event(
                        "turns.claim",
                        level="debug",
                        claim_kind="turn",
                        session_id=str(session_id),
                        owner_hash=_owner_hash(owner),
                        outcome="stale_conflict",
                    )
                    return None
            log_event(
                "turns.claim",
                level="debug",
                claim_kind="turn",
                session_id=str(session_id),
                owner_hash=_owner_hash(owner),
                outcome="acquired",
            )
            return owner
        # DB-less: the local registry IS the claim (either kind blocks).
        if not _local_try_claim(session_id, "turn", owner):
            try:
                lock.release()
            except RuntimeError:
                _note_lock_contention("begin_turn_local_busy")
            log_event(
                "turns.claim",
                level="debug",
                claim_kind="turn",
                session_id=str(session_id),
                owner_hash=_owner_hash(owner),
                outcome="busy",
            )
            return None
        log_event(
            "turns.claim",
            level="debug",
            claim_kind="turn",
            session_id=str(session_id),
            owner_hash=_owner_hash(owner),
            outcome="acquired",
        )
        return owner
    except asyncio.CancelledError:
        # MCH-002: cancellation during acquisition must reconcile acquisition
        # state — a durable claim may have committed without any caller ever
        # receiving this token. Bounded, token-matched release only; a
        # successor's claim is never released (see _reconcile_cancelled_claim).
        log_event(
            "turns.claim",
            level="warning",
            claim_kind="turn",
            session_id=str(session_id),
            owner_hash=_owner_hash(owner),
            outcome="cancelled",
        )
        try:
            await _reconcile_cancelled_claim(session_id, owner, "turn")
        finally:
            try:
                lock.release()
            except RuntimeError:
                _note_lock_contention("begin_turn_cancelled")
            _local_release(session_id, owner)
        raise
    except Exception:
        try:
            lock.release()
        except RuntimeError:
            _note_lock_contention("begin_turn_error_cleanup")
        _local_release(session_id, owner)
        raise


@instrumented("turns", "release")
async def end_turn(session_id: uuid.UUID, token: str | None) -> bool:
    """Release a turn claim; only the exact owner succeeds (stale-safe).

    AH-AUDIT-017: the local mutex is released only when the caller owns the
    local registry entry. A stale token whose DB release was rejected never
    unlocks a newer owner's local claim. Database errors propagate so callers
    cannot mistake unknown ownership for a successfully retired turn.
    """
    from machina.db.connection import db

    if not token:
        log_event(
            "turns.release",
            level="debug",
            claim_kind="turn",
            session_id=str(session_id),
            outcome="no_token",
        )
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
                        _note_lock_contention("end_turn_release")
        log_event(
            "turns.release",
            level="debug",
            claim_kind="turn",
            session_id=str(session_id),
            owner_hash=_owner_hash(token),
            outcome="released" if ok else "stale",
        )
        return ok
    # DB-less: the token-gated local registry is the claim.
    mine = _local_release(session_id, token)
    if mine:
        lock = _session_lock(session_id)
        if lock.locked():
            try:
                lock.release()
            except RuntimeError:
                _note_lock_contention("end_turn_local_release")
        log_event(
            "turns.release",
            level="debug",
            claim_kind="turn",
            session_id=str(session_id),
            owner_hash=_owner_hash(token),
            outcome="released",
        )
        return True
    log_event(
        "turns.release",
        level="debug",
        claim_kind="turn",
        session_id=str(session_id),
        owner_hash=_owner_hash(token),
        outcome="stale",
    )
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
    from machina.db.connection import db

    if db.connected:
        try:
            return await _live_claim(session_id, kinds)
        except Exception as error:
            # Unknown remote ownership blocks mutation until DB recovery/expiry.
            record_failure("turns.turn_active", error)
            return True
    return turn_locked(session_id)


@instrumented("turns", "mutation_acquire")
async def begin_mutation(session_id: uuid.UUID, *, ttl_s: int = MUTATION_TTL_SECONDS) -> str | None:
    """Claim a mutation slot; fails when a live turn owns the session.

    DB-less mode uses the shared local registry, so a held turn blocks a
    mutation and vice versa (AH-AUDIT-016).
    """
    from machina.db.connection import db

    lock = _mutation_session_lock(session_id)
    if not await _acquire_or_steal(lock, session_id, ("turn", "mutation")):
        log_event(
            "turns.claim",
            level="debug",
            claim_kind="mutation",
            session_id=str(session_id),
            outcome="busy",
        )
        return None
    owner = f"mutation:{uuid.uuid4().hex}"
    try:
        if db.connected:
            claimed = await _db_claim(session_id, owner, "mutation", ttl_s)
            if not claimed:
                try:
                    lock.release()
                except RuntimeError:
                    _note_lock_contention("begin_mutation_claim_busy")
                log_event(
                    "turns.claim",
                    level="debug",
                    claim_kind="mutation",
                    session_id=str(session_id),
                    owner_hash=_owner_hash(owner),
                    outcome="busy",
                )
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
                        _note_lock_contention("begin_mutation_stale_conflict")
                    log_event(
                        "turns.claim",
                        level="debug",
                        claim_kind="mutation",
                        session_id=str(session_id),
                        owner_hash=_owner_hash(owner),
                        outcome="stale_conflict",
                    )
                    return None
            log_event(
                "turns.claim",
                level="debug",
                claim_kind="mutation",
                session_id=str(session_id),
                owner_hash=_owner_hash(owner),
                outcome="acquired",
            )
            return owner
        if not _local_try_claim(session_id, "mutation", owner):
            try:
                lock.release()
            except RuntimeError:
                _note_lock_contention("begin_mutation_local_busy")
            log_event(
                "turns.claim",
                level="debug",
                claim_kind="mutation",
                session_id=str(session_id),
                owner_hash=_owner_hash(owner),
                outcome="busy",
            )
            return None
        log_event(
            "turns.claim",
            level="debug",
            claim_kind="mutation",
            session_id=str(session_id),
            owner_hash=_owner_hash(owner),
            outcome="acquired",
        )
        return owner
    except asyncio.CancelledError:
        # MCH-002 (mutation twin): same token-matched, bounded, shielded
        # reconciliation as the turn path before the cancellation propagates.
        log_event(
            "turns.claim",
            level="warning",
            claim_kind="mutation",
            session_id=str(session_id),
            owner_hash=_owner_hash(owner),
            outcome="cancelled",
        )
        try:
            await _reconcile_cancelled_claim(session_id, owner, "mutation")
        finally:
            try:
                lock.release()
            except RuntimeError:
                _note_lock_contention("begin_mutation_cancelled")
            _local_release(session_id, owner)
        raise
    except Exception:
        try:
            lock.release()
        except RuntimeError:
            _note_lock_contention("begin_mutation_error_cleanup")
        _local_release(session_id, owner)
        raise


@instrumented("turns", "mutation_release")
async def end_mutation(session_id: uuid.UUID, token: str | None) -> bool:
    """Release a mutation claim; stale tokens never unlock newer owners."""
    from machina.db.connection import db

    if not token:
        log_event(
            "turns.release",
            level="debug",
            claim_kind="mutation",
            session_id=str(session_id),
            outcome="no_token",
        )
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
                    _note_lock_contention("end_mutation_release")
        log_event(
            "turns.release",
            level="debug",
            claim_kind="mutation",
            session_id=str(session_id),
            owner_hash=_owner_hash(token),
            outcome="released" if ok else "stale",
        )
        return ok
    mine = _local_release(session_id, token)
    if mine:
        lock = _mutation_session_lock(session_id)
        if lock.locked():
            try:
                lock.release()
            except RuntimeError:
                _note_lock_contention("end_mutation_local_release")
        log_event(
            "turns.release",
            level="debug",
            claim_kind="mutation",
            session_id=str(session_id),
            owner_hash=_owner_hash(token),
            outcome="released",
        )
        return True
    log_event(
        "turns.release",
        level="debug",
        claim_kind="mutation",
        session_id=str(session_id),
        owner_hash=_owner_hash(token),
        outcome="stale",
    )
    return False


async def renewal_loop(
    session_id: uuid.UUID,
    token: str | None,
    *,
    interval_s: float,
    ttl_s: int = TURN_TTL_SECONDS,
    ownership_lost: asyncio.Event,
    on_lost: Callable[[], None] | None = None,
) -> None:
    """Periodic claim renewal shared by every long-running transport (AH-AUDIT-013).

    ``renew_turn`` absorbs transient DB errors into False (fail closed), so a
    single False (or a raised error — ownership uncertainty, never a retry
    signal) means the claim is no longer provably ours: set the loss event,
    count the metric, run the transport-specific ``on_lost`` effects, and
    stop. Stop the loop by cancelling its task — every transport already
    joins the task during release.

    ``ttl_s`` bounds the renewal cadence; the renew call itself keeps
    renew_turn's public two-argument shape (its default TTL), which every
    transport shares.
    """
    if not token:
        return
    while True:
        await asyncio.sleep(min(interval_s, ttl_s / 3))
        try:
            ok = await renew_turn(session_id, token)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Fail closed exactly like the two former inline loops: an error in
            # the renewal path is ownership uncertainty, not a retry signal
            # (renew_turn itself maps transient DB errors to False internally).
            ok = False
        if ok:
            continue
        logger.warning("Turn %s lost ownership; stopping effects", session_id)
        ownership_lost.set()
        metrics.increment_counter("turn.ownership_lost")
        if on_lost is not None:
            try:
                on_lost()
            except Exception as _boundary_error:
                # The loss event + metric are already recorded; a transport
                # callback failure must never suppress loss handling itself.
                record_failure("turns.renewal_loop.on_lost", _boundary_error)
        return


class TurnOwnership:
    """Logical turn slot: owner token + in-process slot for a whole turn.

    Never holds a DB transaction or pool connection across the turn.

    AH-AUDIT-013: start_renewal() keeps a long turn alive with periodic
    renew_claim; renewal failure or detected loss sets ownership_lost so
    owned effects stop. TurnEngine.close cancels and joins the renewal task
    itself during release. Crash expiry recovery is preserved (no renewal →
    the claim lapses and becomes reclaimable).
    """

    def __init__(
        self,
        session_id: uuid.UUID,
        turn_id: str,
        token: str | None = None,
        ownership_lost: asyncio.Event | None = None,
    ) -> None:
        self.session_id = session_id
        self.turn_id = turn_id
        self.token = token
        # Callers that already publish a per-turn loss event (gateway map, SSE
        # transport) pass it in so the primitive and their readers share ONE
        # event; standalone callers get a fresh one.
        self.ownership_lost = ownership_lost or asyncio.Event()
        self._renew_task: asyncio.Task | None = None

    def start_renewal(
        self,
        interval_s: float = 60.0,
        ttl_s: int = TURN_TTL_SECONDS,
        on_lost: Callable[[], None] | None = None,
    ) -> asyncio.Task | None:
        """Begin periodic claim renewal through the shared primitive; idempotent.

        Always returns the renewal task (which exits immediately when this
        owner has no token) so transports can join or cancel it uniformly.
        """
        if self._renew_task is not None and not self._renew_task.done():
            return self._renew_task
        self._renew_task = asyncio.create_task(
            renewal_loop(
                self.session_id,
                self.token,
                interval_s=interval_s,
                ttl_s=ttl_s,
                ownership_lost=self.ownership_lost,
                on_lost=on_lost,
            ),
            name=f"turn-renew-{self.turn_id}",
        )
        return self._renew_task

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
