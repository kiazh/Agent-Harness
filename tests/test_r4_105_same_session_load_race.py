"""R4-105(b): same-session read/write load race against the real database.

Replaces the wrong-seam "mixed read/write" race in ``test_load_concurrency``
(``test_concurrent_context_mixed_read_write``): there, readers and writers ran
on *different* sessions, so a stale read could never be observed and the test
said so ("Reader sessions may have 0..M chunks depending on timing — that's
OK"). ``ContextManager`` has no read-side cache (the documented "LRU cache
invalidation" claim is a phantom), so the correctness claim to prove is
DB-level: concurrent writers and readers on the SAME session must never let a
reader observe a gap, a partial multi-row write, a duplicate, or a
regressing (stale) snapshot.

Determinism: the writer's first unit is committed, then the writer blocks
until a reader records one sample that observed exactly that unit. That
sample is written to the transcript BEFORE the event is set, so "a reader
observed an in-flight state" is guaranteed, not timing-luck. Every later
assertion is a set/prefix invariant, not a timing assertion.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
import pytest_asyncio

pytestmark = pytest.mark.db

SESSIONS = 4
UNITS = 10
READERS_PER_SESSION = 2
WRITER_GAP_SECONDS = 0.002
READER_GAP_SECONDS = 0.001
HANDSHAKE_TIMEOUT_SECONDS = 15.0
READER_SAMPLE_CAP = 4_000


@pytest_asyncio.fixture
async def race_db():
    """Wire the shared ``db`` singleton to a PRIVATE test schema.

    Deliberately independent of the ``db_pool`` fixture: that fixture runs the
    full public ``schema.sql`` (ALTER TABLE ...) per test, which deadlocks
    against other workspace processes' concurrent data writes on the shared
    test database. A private schema's DDL never locks public relations.
    """
    dsn = os.environ.get("MACHINA_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("MACHINA_TEST_DATABASE_URL not set")
    from machina.db.connection import Database, db
    from tests.support.schema import isolated_schema

    administrator = Database(dsn=dsn)
    await administrator.connect()
    try:
        async with isolated_schema(administrator) as (_schema, database):
            original_pool, original_dsn = db._pool, db.dsn
            db._pool = database._pool
            db.dsn = database.dsn
            try:
                yield database
            finally:
                db._pool = original_pool
                db.dsn = original_dsn
    finally:
        await administrator.close()


async def _race_session(session_id, agent_id, run_token, unit_size):
    """One writer + N readers on ONE session.

    Returns ``(per_reader_samples, written_ids)`` where each reader's samples
    stay in that reader's own observation order (asserting monotonicity across
    a merged sequence would mix two readers' timelines).

    A *unit* is one write call: a single ``add_chunk`` (size 1) or one
    ``add_chunks_batch`` (atomic multi-row insert). Readers must observe
    units as complete, in-order prefixes.
    """
    from machina.core.context import ContextManager

    writer_mgr = ContextManager()
    first_unit_seen = asyncio.Event()
    writer_done = asyncio.Event()
    written_ids: set[uuid.UUID] = set()

    async def writer():
        for unit in range(UNITS):
            if unit_size == 1:
                chunk = await writer_mgr.add_chunk(
                    session_id=session_id,
                    agent_id=agent_id,
                    chunk_type="assistant_message",
                    payload={
                        "r4_105_run": run_token,
                        "r4_105_unit": unit,
                        "r4_105_slot": 0,
                    },
                    token_count=1,
                )
                written_ids.add(chunk.id)
            else:
                inserted = await writer_mgr.add_chunks_batch(
                    [
                        {
                            "session_id": session_id,
                            "agent_id": agent_id,
                            "chunk_type": "assistant_message",
                            "payload": {
                                "r4_105_run": run_token,
                                "r4_105_unit": unit,
                                "r4_105_slot": slot,
                            },
                            "token_count": 1,
                        }
                        for slot in range(unit_size)
                    ]
                )
                assert len(inserted) == unit_size, inserted
                written_ids.update(chunk.id for chunk in inserted)
            if unit == 0:
                # Block until a reader has recorded the in-flight unit 0
                # sample; the writer cannot advance past it before that.
                await asyncio.wait_for(first_unit_seen.wait(), timeout=HANDSHAKE_TIMEOUT_SECONDS)
            else:
                await asyncio.sleep(WRITER_GAP_SECONDS)
        writer_done.set()

    async def reader() -> list[dict]:
        """This reader's own samples, in ITS observation order."""
        reader_mgr = ContextManager()
        own: list[dict] = []
        for _ in range(READER_SAMPLE_CAP):
            chunks = await reader_mgr.get_chunks(session_id, limit=UNITS * unit_size + 16)
            per_unit: dict[int, int] = {}
            ids: set[uuid.UUID] = set()
            for chunk in chunks:
                unit = int(chunk.payload["r4_105_unit"])
                per_unit[unit] = per_unit.get(unit, 0) + 1
                ids.add(chunk.id)
            sample = {
                "units": sorted(per_unit),
                "chunks": per_unit,
                "ids": ids,
                "sizes": [len(chunks)],
            }
            own.append(sample)
            if 0 in per_unit:
                first_unit_seen.set()
            if writer_done.is_set():
                return own  # the final sample above is post-writer
            await asyncio.sleep(READER_GAP_SECONDS)
        raise AssertionError("reader sample cap exhausted before the writer finished")

    reader_tasks = [asyncio.create_task(reader()) for _ in range(READERS_PER_SESSION)]
    writer_task = asyncio.create_task(writer())
    try:
        await asyncio.wait_for(writer_task, timeout=60)
        per_reader = [await asyncio.wait_for(task, timeout=30) for task in reader_tasks]
    finally:
        for task in [writer_task, *reader_tasks]:
            if not task.done():
                task.cancel()
        await asyncio.gather(writer_task, *reader_tasks, return_exceptions=True)
    return per_reader, written_ids


def _assert_prefix_and_atomicity(samples, unit_size):
    """Every sample must be a complete, duplicate-free prefix of units 0..m."""
    for sample in samples:
        units = sample["units"]
        assert units == list(range(len(units))), (
            f"observed a non-prefix unit set (gap or reorder): {units}"
        )
        for unit, count in sample["chunks"].items():
            assert count == unit_size, f"unit {unit} observed partially: {count}/{unit_size} chunks"
        total = sum(sample["chunks"].values())
        assert sample["sizes"][0] == total, sample
        assert len(sample["ids"]) == total, "duplicate chunk ids in one read"


def _assert_monotonic(samples):
    """A later read may never lose a unit a previous read observed."""
    observed = 0
    for sample in samples:
        now = len(sample["units"])
        assert now >= observed, (
            f"stale read: observed {observed} units, then {now} (chunks became invisible)"
        )
        observed = now


@pytest.mark.asyncio
@pytest.mark.parametrize("unit_size", [1, 5], ids=["single-chunk-writes", "batch-writes"])
async def test_same_session_readers_see_monotonic_complete_prefix(race_db, unit_size):
    from machina.core.session import session_manager

    agent_id = f"r4-105-race-{uuid.uuid4()}"
    sessions = [
        await session_manager.create(title=f"r4-105-race-{i}", agent_id=agent_id)
        for i in range(SESSIONS)
    ]
    session_ids = [s.id for s in sessions]

    results = await asyncio.gather(
        *(_race_session(sid, agent_id, f"run-{i}", unit_size) for i, sid in enumerate(session_ids)),
        return_exceptions=True,
    )
    errors = [r for r in results if isinstance(r, Exception)]
    assert not errors, f"race workers failed: {errors}"

    from machina.core.context import ContextManager

    reader = ContextManager()
    for i, (sid, (per_reader, written_ids)) in enumerate(zip(session_ids, results, strict=True)):
        flat = [sample for own in per_reader for sample in own]
        assert flat, f"session {i}: no reader samples"
        _assert_prefix_and_atomicity(flat, unit_size)
        for own in per_reader:
            # Ordering is asserted per reader: merging two readers' sequences
            # would interleave a lagging reader's older sample after a faster
            # reader's newer one (a harness artifact, not a stale DB read).
            assert own, f"session {i}: reader produced no samples"
            _assert_monotonic(own)
        # No-loss proof on an authoritative POST-COMPLETION read: the writer
        # task has fully returned, so every unit is committed and a fresh
        # statement must see all of them with exactly the written ids. (A
        # reader's last in-flight sample may legitimately miss a unit whose
        # commit landed after that statement's snapshot was taken — a
        # read-committed snapshot race, not lost data — so completeness is
        # asserted here, sequentially after the join, not on racy samples.)
        final = await reader.get_chunks(sid, limit=UNITS * unit_size + 16)
        assert {c.id for c in final} == written_ids, (
            f"session {i}: post-completion read lost "
            f"{len(written_ids - {c.id for c in final})} chunk(s)"
        )
        # Deterministic in-flight proof: the handshake guarantees some reader
        # recorded exactly unit 0 while the writer was blocked.
        assert any(sample["units"] == [0] for sample in flat), (
            f"session {i}: no mid-flight sample recorded"
        )
        db_count = await race_db.fetchval(
            "SELECT COUNT(*) FROM context_chunks WHERE session_id = $1", sid
        )
        assert db_count == UNITS * unit_size
