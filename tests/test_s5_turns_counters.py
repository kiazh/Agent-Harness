"""S5.5: turns.py contention counters — semantics identical, now observed.

Each test forces one `lock.release()` race deterministically (a stub lock
whose release() raises RuntimeError, exactly what a stolen mutex raises)
and asserts the corresponding metrics counter increments while control flow
is unchanged (busy stays busy, release still succeeds/fails as before).
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

from machina.core import turns as _turns
from machina.core.metrics import metrics


class _RaceLock:
    """Mutex stub: acquire succeeds, release always races (RuntimeError)."""

    def __init__(self, *, locked: bool = False):
        self._locked = locked

    def locked(self) -> bool:
        return self._locked

    async def acquire(self) -> bool:
        self._locked = True
        return True

    def release(self) -> None:
        raise RuntimeError("already released (forced contention)")


def _delta(counter: str) -> int:
    return metrics.get_counter(counter)


def _sid() -> uuid.UUID:
    return uuid.uuid4()


def _cleanup(sid: uuid.UUID) -> None:
    _turns._locks.pop(str(sid), None)
    _turns._mutation_locks.pop(str(sid), None)
    _turns._local_reset_for_tests()


async def test_note_lock_contention_counts_and_never_raises(monkeypatch):
    before = _delta("turns.lock_release_contention")
    before_site = _delta("turns.lock_release_contention.probe_site")
    _turns._note_lock_contention("probe_site")
    assert _delta("turns.lock_release_contention") == before + 1
    assert _delta("turns.lock_release_contention.probe_site") == before_site + 1
    # A broken metrics sink must never break claim/release control flow.
    monkeypatch.setattr(
        metrics,
        "increment_counter",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("sink down")),
    )
    _turns._note_lock_contention("probe_site")


async def test_acquire_steal_contention_counted(monkeypatch):
    from machina.db.connection import Database

    monkeypatch.setattr(Database, "connected", property(lambda self: True))
    monkeypatch.setattr(_turns, "_live_claim", AsyncMock(return_value=False))
    sid = _sid()
    lock = _RaceLock(locked=True)
    before = _delta("turns.lock_release_contention.acquire_steal")
    try:
        assert await _turns._acquire_or_steal(lock, sid, ("turn", "mutation")) is True
        assert _delta("turns.lock_release_contention.acquire_steal") == before + 1
    finally:
        _cleanup(sid)


async def test_begin_turn_claim_busy_contention_counted(monkeypatch):
    from machina.db.connection import Database, db

    monkeypatch.setattr(Database, "connected", property(lambda self: True))
    monkeypatch.setattr(db, "fetchrow", AsyncMock(return_value=None))  # claim refused
    sid = _sid()
    _turns._locks[str(sid)] = _RaceLock(locked=False)
    before = _delta("turns.lock_release_contention.begin_turn_claim_busy")
    try:
        assert await _turns.try_begin_turn(sid) is None  # still busy, no exception
        assert _delta("turns.lock_release_contention.begin_turn_claim_busy") == before + 1
    finally:
        _cleanup(sid)


async def test_begin_turn_local_busy_contention_counted():
    sid = _sid()
    try:
        owner = await _turns.try_begin_turn(sid)
        assert owner is not None
        _turns._locks[str(sid)] = _RaceLock(locked=False)  # steal the mutex only
        before = _delta("turns.lock_release_contention.begin_turn_local_busy")
        assert await _turns.try_begin_turn(sid) is None  # local claim still blocks
        assert _delta("turns.lock_release_contention.begin_turn_local_busy") == before + 1
    finally:
        _cleanup(sid)


async def test_begin_turn_error_cleanup_contention_counted(monkeypatch):
    sid = _sid()
    _turns._locks[str(sid)] = _RaceLock(locked=False)

    def _boom(*args, **kwargs):
        raise RuntimeError("forced claim failure")

    monkeypatch.setattr(_turns, "_local_try_claim", _boom)
    before = _delta("turns.lock_release_contention.begin_turn_error_cleanup")
    try:
        try:
            await _turns.try_begin_turn(sid)
        except RuntimeError:
            pass
        else:
            raise AssertionError("original error must still propagate")
        assert _delta("turns.lock_release_contention.begin_turn_error_cleanup") == before + 1
    finally:
        _cleanup(sid)


async def test_end_turn_local_release_contention_counted():
    sid = _sid()
    try:
        owner = await _turns.try_begin_turn(sid)
        assert owner is not None
        _turns._locks[str(sid)] = _RaceLock(locked=True)
        before = _delta("turns.lock_release_contention.end_turn_local_release")
        assert await _turns.end_turn(sid, owner) is True  # release still succeeds
        assert _delta("turns.lock_release_contention.end_turn_local_release") == before + 1
    finally:
        _cleanup(sid)


async def test_begin_mutation_claim_busy_contention_counted(monkeypatch):
    from machina.db.connection import Database, db

    monkeypatch.setattr(Database, "connected", property(lambda self: True))
    monkeypatch.setattr(db, "fetchrow", AsyncMock(return_value=None))
    sid = _sid()
    _turns._mutation_locks[str(sid)] = _RaceLock(locked=False)
    before = _delta("turns.lock_release_contention.begin_mutation_claim_busy")
    try:
        assert await _turns.begin_mutation(sid) is None
        assert _delta("turns.lock_release_contention.begin_mutation_claim_busy") == before + 1
    finally:
        _cleanup(sid)


async def test_begin_mutation_error_cleanup_contention_counted(monkeypatch):
    sid = _sid()
    _turns._mutation_locks[str(sid)] = _RaceLock(locked=False)

    def _boom(*args, **kwargs):
        raise RuntimeError("forced claim failure")

    monkeypatch.setattr(_turns, "_local_try_claim", _boom)
    before = _delta("turns.lock_release_contention.begin_mutation_error_cleanup")
    try:
        try:
            await _turns.begin_mutation(sid)
        except RuntimeError:
            pass
        else:
            raise AssertionError("original error must still propagate")
        assert _delta("turns.lock_release_contention.begin_mutation_error_cleanup") == before + 1
    finally:
        _cleanup(sid)


async def test_end_mutation_local_release_contention_counted():
    sid = _sid()
    try:
        owner = await _turns.begin_mutation(sid)
        assert owner is not None
        _turns._mutation_locks[str(sid)] = _RaceLock(locked=True)
        before = _delta("turns.lock_release_contention.end_mutation_local_release")
        assert await _turns.end_mutation(sid, owner) is True
        assert _delta("turns.lock_release_contention.end_mutation_local_release") == before + 1
    finally:
        _cleanup(sid)


async def test_end_turn_release_contention_counted(monkeypatch):
    """DB-backed release path: ownership release still succeeds on race."""
    from machina.db.connection import Database, db

    monkeypatch.setattr(Database, "connected", property(lambda self: True))
    monkeypatch.setattr(db, "fetchrow", AsyncMock(return_value={"id": 1}))
    monkeypatch.setattr(db, "execute", AsyncMock(return_value="UPDATE 1"))
    sid = _sid()
    try:
        owner = await _turns.try_begin_turn(sid)
        assert owner is not None
        _turns._locks[str(sid)] = _RaceLock(locked=True)
        before = _delta("turns.lock_release_contention.end_turn_release")
        assert await _turns.end_turn(sid, owner) is True
        assert _delta("turns.lock_release_contention.end_turn_release") == before + 1
    finally:
        _cleanup(sid)


async def test_end_mutation_release_contention_counted(monkeypatch):
    from machina.db.connection import Database, db

    monkeypatch.setattr(Database, "connected", property(lambda self: True))
    monkeypatch.setattr(db, "fetchrow", AsyncMock(return_value={"id": 1}))
    monkeypatch.setattr(db, "execute", AsyncMock(return_value="UPDATE 1"))
    sid = _sid()
    try:
        owner = await _turns.begin_mutation(sid)
        assert owner is not None
        _turns._mutation_locks[str(sid)] = _RaceLock(locked=True)
        before = _delta("turns.lock_release_contention.end_mutation_release")
        assert await _turns.end_mutation(sid, owner) is True
        assert _delta("turns.lock_release_contention.end_mutation_release") == before + 1
    finally:
        _cleanup(sid)


async def test_begin_turn_stale_conflict_contention_counted(monkeypatch):
    """DB-backed stale-local conflict: contender still loses, race counted."""
    from datetime import UTC, datetime, timedelta

    from machina.db.connection import Database, db

    monkeypatch.setattr(Database, "connected", property(lambda self: True))
    sid = _sid()
    try:
        assert _turns._local_try_claim(sid, "turn", "stale-token") is True
        _turns._locks[str(sid)] = _RaceLock(locked=False)
        live_state = {
            "claim_owner": "stale-token",
            "claim_kind": "turn",
            "claim_expires_at": datetime.now(UTC) + timedelta(seconds=300),
        }
        monkeypatch.setattr(db, "fetchrow", AsyncMock(side_effect=[{"id": 1}, live_state]))
        monkeypatch.setattr(db, "execute", AsyncMock(return_value="UPDATE 1"))
        before = _delta("turns.lock_release_contention.begin_turn_stale_conflict")
        assert await _turns.try_begin_turn(sid) is None
        assert _delta("turns.lock_release_contention.begin_turn_stale_conflict") == before + 1
    finally:
        _cleanup(sid)


async def test_begin_mutation_stale_conflict_contention_counted(monkeypatch):
    from datetime import UTC, datetime, timedelta

    from machina.db.connection import Database, db

    monkeypatch.setattr(Database, "connected", property(lambda self: True))
    sid = _sid()
    try:
        assert _turns._local_try_claim(sid, "mutation", "stale-token") is True
        _turns._mutation_locks[str(sid)] = _RaceLock(locked=False)
        live_state = {
            "claim_owner": "stale-token",
            "claim_kind": "mutation",
            "claim_expires_at": datetime.now(UTC) + timedelta(seconds=300),
        }
        monkeypatch.setattr(db, "fetchrow", AsyncMock(side_effect=[{"id": 1}, live_state]))
        monkeypatch.setattr(db, "execute", AsyncMock(return_value="UPDATE 1"))
        before = _delta("turns.lock_release_contention.begin_mutation_stale_conflict")
        assert await _turns.begin_mutation(sid) is None
        assert _delta("turns.lock_release_contention.begin_mutation_stale_conflict") == before + 1
    finally:
        _cleanup(sid)
