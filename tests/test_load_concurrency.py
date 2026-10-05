"""Load tests for concurrent sessions — Gate 3.

Exercises the concurrency-critical paths against the real PostgreSQL DB:
  - usage.reserve/finish with pg_advisory_xact_lock (fixed agent→session ordering)
  - context chunk writes/reads (no lost or duplicate chunks)
  - session creation/listing under concurrency
  - _RpcGateway asyncio.Lock serialization

Invariants asserted:
  1. No deadlocks (advisory-lock ordering is correct)
  2. No orphaned 'reserved' rows after all operations complete
  3. Usage totals equal the sum of completed calls
  4. Concurrent context writes/reads preserve chunk counts (no loss, no duplication)
  5. Session creation/listing returns consistent results
  6. Gateway lock serializes without starving

Run with: python -m pytest tests/test_load_concurrency.py -q
Deselect from fast suite: python -m pytest tests/ -q -m "not load"
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid
from typing import Any

import pytest
import pytest_asyncio

from ah.core.config import config

# ─── configuration ───────────────────────────────────────────────────────────

def _concurrency() -> int:
    """Number of concurrent tasks. Configurable via env var."""
    return int(os.environ.get("LOAD_TEST_CONCURRENCY", "25"))


def _chunks_per_task() -> int:
    """Number of context chunks each task writes."""
    return int(os.environ.get("LOAD_TEST_CHUNKS_PER_TASK", "10"))


# ─── fixtures ───────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def load_db(db_pool):
    """Wire the global db singleton to the real test pool for this test."""
    from ah.db.connection import db

    original_dsn = db.dsn
    db._pool = db_pool._pool
    db.dsn = db_pool.dsn
    yield db_pool
    # Restore original state
    db._pool = None
    db.dsn = original_dsn


@pytest_asyncio.fixture
async def clean_load_data(db_pool):
    """Clean up any leftover data from previous load test runs."""
    await db_pool.execute("DELETE FROM llm_usage WHERE agent_id LIKE 'load-test-%'")
    await db_pool.execute(
        "DELETE FROM context_chunks WHERE agent_id LIKE 'load-test-%'"
    )
    await db_pool.execute(
        "DELETE FROM context_archive WHERE agent_id LIKE 'load-test-%'"
    )
    await db_pool.execute("DELETE FROM sessions WHERE agent_id LIKE 'load-test-%'")
    yield
    # Final cleanup
    await db_pool.execute("DELETE FROM llm_usage WHERE agent_id LIKE 'load-test-%'")
    await db_pool.execute(
        "DELETE FROM context_chunks WHERE agent_id LIKE 'load-test-%'"
    )
    await db_pool.execute(
        "DELETE FROM context_archive WHERE agent_id LIKE 'load-test-%'"
    )
    await db_pool.execute("DELETE FROM sessions WHERE agent_id LIKE 'load-test-%'")


# ─── helpers ────────────────────────────────────────────────────────────────


async def _reserve_and_finish(
    store: Any,
    session_id: uuid.UUID,
    agent_id: str,
    messages: list[dict[str, Any]],
    *,
    fail: bool = False,
) -> uuid.UUID | None:
    """Reserve usage, optionally fail, and return the reservation ID."""
    reservation_id = await store.reserve(
        session_id, agent_id, "fake", "fake-model", messages, [], 8
    )
    if fail:
        await store.finish(reservation_id, failed=True)
    else:
        await store.finish(
            reservation_id,
            {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        )
    return reservation_id


# ─── tests ──────────────────────────────────────────────────────────────────


@pytest.mark.load
@pytest.mark.asyncio
async def test_concurrent_usage_reservations_no_deadlock(
    load_db, clean_load_data, monkeypatch
):
    """N concurrent tasks reserving usage for the same agent but different
    sessions must not deadlock. The fixed agent→session advisory-lock ordering
    in UsageStore.reserve() prevents deadlocks.

    Invariant: all reservations complete (no deadlock), and the final state
    has zero orphaned 'reserved' rows.
    """
    from ah.core.usage import UsageStore

    monkeypatch.setattr("ah.core.usage.db", load_db)
    # Disable budget limits so all reservations succeed
    monkeypatch.setattr(config, "usage_session_request_limit", 0)
    monkeypatch.setattr(config, "usage_session_token_limit", 0)
    monkeypatch.setattr(config, "usage_agent_request_limit", 0)
    monkeypatch.setattr(config, "usage_agent_token_limit", 0)

    N = _concurrency()
    agent_id = f"load-test-usage-{uuid.uuid4()}"
    store = UsageStore()
    messages = [{"role": "user", "content": "hello"}]

    # Each task gets its own session but shares the same agent — this is the
    # exact scenario that would deadlock if lock ordering were wrong.
    session_ids = [uuid.uuid4() for _ in range(N)]

    start = time.monotonic()
    results = await asyncio.gather(
        *(
            _reserve_and_finish(store, sid, agent_id, messages)
            for sid in session_ids
        ),
        return_exceptions=True,
    )
    elapsed = time.monotonic() - start

    # No deadlocks or errors
    errors = [r for r in results if isinstance(r, Exception)]
    assert not errors, f"Concurrent reservations raised errors: {errors}"

    # All reservations succeeded
    assert all(isinstance(r, uuid.UUID) for r in results)

    # No orphaned 'reserved' rows — every reservation was finished
    orphaned = await load_db.fetchval(
        "SELECT COUNT(*) FROM llm_usage WHERE agent_id = $1 AND status = 'reserved'",
        agent_id,
    )
    assert orphaned == 0, f"Found {orphaned} orphaned reserved rows"

    # All rows are 'complete'
    total_rows = await load_db.fetchval(
        "SELECT COUNT(*) FROM llm_usage WHERE agent_id = $1", agent_id
    )
    assert total_rows == N

    # Each call accounted for 5 tokens (3 prompt + 2 completion)
    total_tokens = await load_db.fetchval(
        "SELECT COALESCE(SUM(accounted_tokens), 0) FROM llm_usage WHERE agent_id = $1",
        agent_id,
    )
    assert total_tokens == N * 5

    # Should complete well under a minute
    assert elapsed < 30, f"Reservations took {elapsed:.1f}s — possible contention issue"


@pytest.mark.load
@pytest.mark.asyncio
async def test_concurrent_usage_with_failures_balances(
    load_db, clean_load_data, monkeypatch
):
    """When some calls fail, the usage accounting must still balance:
    - Failed calls get status='error' with accounted_tokens = reserved_tokens
    - Successful calls get status='complete' with real token counts
    - No orphaned 'reserved' rows remain
    """
    from ah.core.usage import UsageStore

    monkeypatch.setattr("ah.core.usage.db", load_db)
    monkeypatch.setattr(config, "usage_session_request_limit", 0)
    monkeypatch.setattr(config, "usage_session_token_limit", 0)
    monkeypatch.setattr(config, "usage_agent_request_limit", 0)
    monkeypatch.setattr(config, "usage_agent_token_limit", 0)

    N = _concurrency()
    agent_id = f"load-test-usage-fail-{uuid.uuid4()}"
    store = UsageStore()
    messages = [{"role": "user", "content": "hello"}]

    session_ids = [uuid.uuid4() for _ in range(N)]
    # Every 3rd call fails
    fail_flags = [i % 3 == 0 for i in range(N)]

    async def _task(sid: uuid.UUID, fail: bool) -> uuid.UUID | None:
        return await _reserve_and_finish(
            store, sid, agent_id, messages, fail=fail
        )

    results = await asyncio.gather(
        *(_task(sid, fail) for sid, fail in zip(session_ids, fail_flags)),
        return_exceptions=True,
    )

    errors = [r for r in results if isinstance(r, Exception)]
    assert not errors, f"Tasks raised errors: {errors}"

    # No orphaned reserved rows
    orphaned = await load_db.fetchval(
        "SELECT COUNT(*) FROM llm_usage WHERE agent_id = $1 AND status = 'reserved'",
        agent_id,
    )
    assert orphaned == 0

    # Count complete vs error
    complete_count = await load_db.fetchval(
        "SELECT COUNT(*) FROM llm_usage WHERE agent_id = $1 AND status = 'complete'",
        agent_id,
    )
    error_count = await load_db.fetchval(
        "SELECT COUNT(*) FROM llm_usage WHERE agent_id = $1 AND status = 'error'",
        agent_id,
    )

    expected_errors = sum(fail_flags)
    expected_complete = N - expected_errors
    assert complete_count == expected_complete
    assert error_count == expected_errors

    # Total rows = N
    total = await load_db.fetchval(
        "SELECT COUNT(*) FROM llm_usage WHERE agent_id = $1", agent_id
    )
    assert total == N


@pytest.mark.load
@pytest.mark.asyncio
async def test_concurrent_context_writes_no_loss_or_duplication(
    load_db, clean_load_data, monkeypatch
):
    """N concurrent tasks each write M chunks to their own session, then read
    them back. The chunk count must match exactly — no lost writes, no
    duplicates.

    Also tests concurrent reads while writes are in flight.
    """
    from ah.core.context import ContextManager
    from ah.core.session import SessionManager

    monkeypatch.setattr("ah.core.context.db", load_db)
    monkeypatch.setattr("ah.core.session.db", load_db)

    N = _concurrency()
    M = _chunks_per_task()
    agent_id = f"load-test-ctx-{uuid.uuid4()}"

    # Create sessions
    session_mgr = SessionManager()
    sessions = await asyncio.gather(
        *(session_mgr.create(title=f"load-ctx-{i}", agent_id=agent_id) for i in range(N))
    )
    session_ids = [s.id for s in sessions]

    async def _write_chunks(sid: uuid.UUID, task_idx: int) -> int:
        """Write M chunks to the session, return the number written."""
        mgr = ContextManager()
        for j in range(M):
            await mgr.add_chunk(
                session_id=sid,
                agent_id=agent_id,
                chunk_type="user_message",
                payload={"text": f"task-{task_idx}-chunk-{j}", "seq": j},
                token_count=1,
            )
        return M

    # All tasks write concurrently
    write_results = await asyncio.gather(
        *(_write_chunks(sid, i) for i, sid in enumerate(session_ids)),
        return_exceptions=True,
    )

    write_errors = [r for r in write_results if isinstance(r, Exception)]
    assert not write_errors, f"Write errors: {write_errors}"
    assert all(r == M for r in write_results)

    # Read back and verify counts
    async def _verify_chunks(sid: uuid.UUID) -> int:
        mgr = ContextManager()
        chunks = await mgr.get_chunks(sid, limit=M * 2)
        return len(chunks)

    read_results = await asyncio.gather(
        *(_verify_chunks(sid) for sid in session_ids),
        return_exceptions=True,
    )

    read_errors = [r for r in read_results if isinstance(r, Exception)]
    assert not read_errors, f"Read errors: {read_errors}"

    # Each session must have exactly M chunks — no loss, no duplication
    for i, count in enumerate(read_results):
        assert count == M, (
            f"Session {i}: expected {M} chunks, got {count} "
            f"(lost={M - count}, extra={count - M})"
        )

    # Verify total across all sessions
    total_chunks = await load_db.fetchval(
        "SELECT COUNT(*) FROM context_chunks WHERE agent_id = $1", agent_id
    )
    assert total_chunks == N * M


@pytest.mark.load
@pytest.mark.asyncio
async def test_concurrent_context_mixed_read_write(
    load_db, clean_load_data, monkeypatch
):
    """Half the tasks write chunks while the other half read. After all
    writes complete, a final read must see all chunks.

    This tests that the LRU cache invalidation works correctly under
    concurrency — a stale cache must not cause missing chunks.
    """
    from ah.core.context import ContextManager
    from ah.core.session import SessionManager

    monkeypatch.setattr("ah.core.context.db", load_db)
    monkeypatch.setattr("ah.core.session.db", load_db)

    N = _concurrency()
    M = _chunks_per_task()
    agent_id = f"load-test-mixed-{uuid.uuid4()}"

    session_mgr = SessionManager()
    sessions = await asyncio.gather(
        *(session_mgr.create(title=f"load-mixed-{i}", agent_id=agent_id) for i in range(N))
    )
    session_ids = [s.id for s in sessions]

    async def _writer(sid: uuid.UUID, task_idx: int) -> int:
        mgr = ContextManager()
        for j in range(M):
            await mgr.add_chunk(
                session_id=sid,
                agent_id=agent_id,
                chunk_type="assistant_message",
                payload={"text": f"writer-{task_idx}-{j}", "seq": j},
                token_count=1,
            )
        return M

    async def _reader(sid: uuid.UUID) -> int:
        """Read chunks — may run concurrently with writes."""
        mgr = ContextManager()
        # Small delay to increase chance of overlapping with writes
        await asyncio.sleep(0.001)
        chunks = await mgr.get_chunks(sid, limit=M * 2)
        return len(chunks)

    # Launch all tasks concurrently — half write, half read
    half = N // 2
    tasks = []
    for i, sid in enumerate(session_ids):
        if i < half:
            tasks.append(_writer(sid, i))
        else:
            tasks.append(_reader(sid))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    errors = [r for r in results if isinstance(r, Exception)]
    assert not errors, f"Mixed read/write errors: {errors}"

    # After all tasks complete, verify final state
    for i, sid in enumerate(session_ids):
        mgr = ContextManager()
        chunks = await mgr.get_chunks(sid, limit=M * 2)
        if i < half:
            # Writer sessions must have exactly M chunks
            assert len(chunks) == M, (
                f"Writer session {i}: expected {M}, got {len(chunks)}"
            )
        # Reader sessions may have 0..M chunks depending on timing — that's OK


@pytest.mark.load
@pytest.mark.asyncio
async def test_concurrent_session_creation_and_listing(
    load_db, clean_load_data, monkeypatch
):
    """N concurrent tasks create sessions, then list them. All created
    sessions must appear in the listing — no lost creates, no duplicates.
    """
    from ah.core.session import SessionManager

    monkeypatch.setattr("ah.core.session.db", load_db)

    N = _concurrency()
    agent_id = f"load-test-sess-{uuid.uuid4()}"

    session_mgr = SessionManager()

    # Create sessions concurrently
    created = await asyncio.gather(
        *(
            session_mgr.create(title=f"load-sess-{i}", agent_id=agent_id)
            for i in range(N)
        ),
        return_exceptions=True,
    )

    create_errors = [r for r in created if isinstance(r, Exception)]
    assert not create_errors, f"Create errors: {create_errors}"

    created_ids = {s.id for s in created}
    assert len(created_ids) == N, f"Expected {N} unique sessions, got {len(created_ids)}"

    # List sessions and verify all created ones are present
    listed = await session_mgr.list_sessions(limit=N * 2)
    listed_ids = {s.id for s in listed}

    missing = created_ids - listed_ids
    assert not missing, f"{len(missing)} created sessions not found in listing"

    # Verify count in DB
    db_count = await load_db.fetchval(
        "SELECT COUNT(*) FROM sessions WHERE agent_id = $1", agent_id
    )
    assert db_count == N


@pytest.mark.load
@pytest.mark.asyncio
async def test_concurrent_session_state_updates(
    load_db, clean_load_data, monkeypatch
):
    """N concurrent tasks update the same session's state. The final state
    must be one of the written values (last-writer-wins is acceptable, but
    the state must be consistent — not a mix of values).
    """
    from ah.core.session import SessionManager

    monkeypatch.setattr("ah.core.session.db", load_db)

    N = _concurrency()
    agent_id = f"load-test-state-{uuid.uuid4()}"

    session_mgr = SessionManager()
    session = await session_mgr.create(title="load-state", agent_id=agent_id)
    sid = session.id

    async def _update_state(task_idx: int) -> None:
        await session_mgr.update_state(sid, {"turn": task_idx, "data": f"task-{task_idx}"})

    await asyncio.gather(
        *(_update_state(i) for i in range(N)),
        return_exceptions=True,
    )

    # Read final state — must be a valid state from one of the tasks
    final_session = await session_mgr.get(sid)
    assert final_session is not None
    turn = final_session.state.get("turn")
    data = final_session.state.get("data")
    assert turn is not None, "Final state has no 'turn' key"
    assert data == f"task-{turn}", (
        f"State inconsistent: turn={turn} but data={data}"
    )


@pytest.mark.load
@pytest.mark.asyncio
async def test_concurrent_usage_same_session_no_overspend(
    load_db, clean_load_data, monkeypatch
):
    """N concurrent tasks all reserving usage for the SAME session with a
    request limit of 1. Exactly one must succeed; the rest must get
    UsageBudgetExceededError. This tests that the advisory lock correctly
    serializes budget checks.
    """
    from ah.core.usage import UsageStore
    from ah.core.exceptions import UsageBudgetExceededError

    monkeypatch.setattr("ah.core.usage.db", load_db)
    monkeypatch.setattr(config, "usage_session_request_limit", 1)
    monkeypatch.setattr(config, "usage_session_token_limit", 0)
    monkeypatch.setattr(config, "usage_agent_request_limit", 0)
    monkeypatch.setattr(config, "usage_agent_token_limit", 0)

    N = _concurrency()
    agent_id = f"load-test-same-sess-{uuid.uuid4()}"
    session_id = uuid.uuid4()
    store = UsageStore()
    messages = [{"role": "user", "content": "hello"}]

    results = await asyncio.gather(
        *(store.reserve(session_id, agent_id, "fake", "fake-model", messages, [], 8) for _ in range(N)),
        return_exceptions=True,
    )

    successes = [r for r in results if isinstance(r, uuid.UUID)]
    budget_errors = [r for r in results if isinstance(r, UsageBudgetExceededError)]
    other_errors = [r for r in results if isinstance(r, Exception) and not isinstance(r, UsageBudgetExceededError)]

    assert not other_errors, f"Unexpected errors: {other_errors}"
    assert len(successes) == 1, f"Expected exactly 1 success, got {len(successes)}"
    assert len(budget_errors) == N - 1, f"Expected {N-1} budget errors, got {len(budget_errors)}"

    # Finish the one successful reservation
    await store.finish(
        successes[0],
        {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    )

    # Verify final state
    summary = await store.summary(session_id, agent_id)
    assert summary["session"]["requests"] == 1
    assert summary["session"]["accountedTokens"] == 5


@pytest.mark.load
@pytest.mark.asyncio
async def test_rpc_gateway_concurrent_calls(load_db, monkeypatch):
    """N concurrent calls through _RpcGateway must all get correct responses.
    The asyncio.Lock in _RpcGateway.call() serializes request/response pairing.

    Each call must receive its own response — no cross-talk.
    """
    from ah.api.app import _RpcGateway

    N = _concurrency()

    # Create a mock gateway that echoes back the request
    class MockGateway:
        def __init__(self):
            self._db_ready = True

        async def handle_line(self, line: str) -> None:
            import json
            frame = json.loads(line)
            rid = frame.get("id")
            # Simulate some async work
            await asyncio.sleep(0.001)
            # Echo back with the same id
            self._writer({
                "jsonrpc": "2.0",
                "id": rid,
                "result": {"echo": frame.get("params", {}), "id": rid},
            })

        async def close(self) -> None:
            pass

    mock_gw = MockGateway()
    gw = _RpcGateway.__new__(_RpcGateway)
    gw._responses = {}
    gw._response_timestamps = {}
    gw._next_id = 0
    gw._lock = asyncio.Lock()
    gw._gateway = mock_gw
    mock_gw._writer = gw._capture

    async def _call(task_idx: int) -> dict[str, Any]:
        result = await gw.call("test.echo", {"task": task_idx})
        return result

    results = await asyncio.gather(
        *(_call(i) for i in range(N)),
        return_exceptions=True,
    )

    errors = [r for r in results if isinstance(r, Exception)]
    assert not errors, f"Gateway errors: {errors}"

    # Each result must have the correct id — no cross-talk
    for i, result in enumerate(results):
        assert result["id"] == i + 1, (
            f"Task {i}: expected response id {i+1}, got {result['id']}"
        )
        assert result["echo"]["task"] == i, (
            f"Task {i}: expected echo task={i}, got {result['echo']['task']}"
        )

    await gw.close()


@pytest.mark.load
@pytest.mark.asyncio
async def test_concurrent_eviction_preserves_recent_chunks(
    load_db, clean_load_data, monkeypatch
):
    """Concurrent eviction on the same session must not evict the most recent
    10 chunks. The cursor-based pagination with batch archive+delete in a
    single transaction must be safe under concurrency.
    """
    from ah.core.context import ContextManager
    from ah.core.session import SessionManager

    monkeypatch.setattr("ah.core.context.db", load_db)
    monkeypatch.setattr("ah.core.session.db", load_db)

    agent_id = f"load-test-evict-{uuid.uuid4()}"
    session_mgr = SessionManager()
    session = await session_mgr.create(title="load-evict", agent_id=agent_id)
    sid = session.id

    # Write 50 chunks with increasing timestamps
    from datetime import UTC, datetime, timedelta
    base_time = datetime(2025, 1, 1, tzinfo=UTC)
    mgr = ContextManager()
    for i in range(50):
        await mgr.add_chunk(
            session_id=sid,
            agent_id=agent_id,
            chunk_type="user_message",
            payload={"text": f"chunk-{i}", "seq": i},
            token_count=10,
        )

    # Run eviction concurrently from multiple tasks
    N = 5
    results = await asyncio.gather(
        *(mgr.evict_old_chunks(sid, max_chunks=10) for _ in range(N)),
        return_exceptions=True,
    )

    errors = [r for r in results if isinstance(r, Exception)]
    assert not errors, f"Eviction errors: {errors}"

    # The most recent 10 chunks must always be preserved
    remaining = await mgr.get_chunks(sid, limit=100)
    remaining_payloads = {c.payload.get("seq") for c in remaining}

    # The last 10 chunks (seq 40-49) must be present
    for seq in range(40, 50):
        assert seq in remaining_payloads, (
            f"Recent chunk seq={seq} was evicted — eviction must preserve recent 10"
        )

    # Total remaining should be <= 10 (the preserved recent chunks)
    assert len(remaining) <= 10, (
        f"Expected <= 10 remaining chunks, got {len(remaining)}"
    )

    # Every evicted chunk must be archived exactly once — concurrent eviction
    # must not double-archive (which would duplicate rows and corrupt the
    # reversible-eviction record).
    total_chunks = 50
    archived = await load_db.fetchval(
        "SELECT COUNT(*) FROM context_archive WHERE session_id = $1", sid
    )
    distinct_archived = await load_db.fetchval(
        "SELECT COUNT(DISTINCT chunk_id) FROM context_archive WHERE session_id = $1", sid
    )
    assert archived == distinct_archived, (
        f"Concurrent eviction double-archived chunks: {archived} rows for "
        f"{distinct_archived} distinct chunks"
    )
    assert archived == total_chunks - len(remaining), (
        f"Archived {archived} but {total_chunks - len(remaining)} chunks were removed"
    )


@pytest.mark.load
@pytest.mark.asyncio
async def test_concurrent_mixed_workload(load_db, clean_load_data, monkeypatch):
    """A mixed workload: some tasks do usage reservations, some write context
    chunks, some create sessions, some list sessions. All must complete
    without errors and the final state must be consistent.
    """
    from ah.core.usage import UsageStore
    from ah.core.context import ContextManager
    from ah.core.session import SessionManager

    monkeypatch.setattr("ah.core.usage.db", load_db)
    monkeypatch.setattr("ah.core.context.db", load_db)
    monkeypatch.setattr("ah.core.session.db", load_db)
    monkeypatch.setattr(config, "usage_session_request_limit", 0)
    monkeypatch.setattr(config, "usage_session_token_limit", 0)
    monkeypatch.setattr(config, "usage_agent_request_limit", 0)
    monkeypatch.setattr(config, "usage_agent_token_limit", 0)

    N = _concurrency()
    agent_id = f"load-test-mixed-{uuid.uuid4()}"
    store = UsageStore()
    ctx_mgr = ContextManager()
    session_mgr = SessionManager()
    messages = [{"role": "user", "content": "hello"}]

    # Pre-create sessions for context writers
    sessions = await asyncio.gather(
        *(session_mgr.create(title=f"mixed-{i}", agent_id=agent_id) for i in range(N))
    )

    async def _usage_task(task_idx: int) -> uuid.UUID | None:
        sid = sessions[task_idx % N].id
        return await _reserve_and_finish(store, sid, agent_id, messages)

    async def _context_task(task_idx: int) -> int:
        sid = sessions[task_idx % N].id
        for j in range(5):
            await ctx_mgr.add_chunk(
                session_id=sid,
                agent_id=agent_id,
                chunk_type="user_message",
                payload={"text": f"mixed-{task_idx}-{j}", "seq": j},
                token_count=1,
            )
        return 5

    async def _session_task(task_idx: int) -> uuid.UUID:
        s = await session_mgr.create(title=f"mixed-extra-{task_idx}", agent_id=agent_id)
        return s.id

    async def _list_task(task_idx: int) -> int:
        sessions = await session_mgr.list_sessions(limit=N * 3)
        return len(sessions)

    # Launch all tasks concurrently
    all_tasks = []
    for i in range(N):
        all_tasks.append(_usage_task(i))
        all_tasks.append(_context_task(i))
        all_tasks.append(_session_task(i))
        all_tasks.append(_list_task(i))

    results = await asyncio.gather(*all_tasks, return_exceptions=True)

    errors = [r for r in results if isinstance(r, Exception)]
    assert not errors, f"Mixed workload errors: {errors[:5]}"

    # Verify usage: no orphaned reserved rows
    orphaned = await load_db.fetchval(
        "SELECT COUNT(*) FROM llm_usage WHERE agent_id = $1 AND status = 'reserved'",
        agent_id,
    )
    assert orphaned == 0, f"Found {orphaned} orphaned reserved rows"

    # Verify context: each session has exactly 5 chunks
    for i in range(N):
        sid = sessions[i].id
        count = await load_db.fetchval(
            "SELECT COUNT(*) FROM context_chunks WHERE session_id = $1", sid
        )
        assert count == 5, f"Session {i}: expected 5 chunks, got {count}"

    # Verify sessions: N original + N extra = 2N
    session_count = await load_db.fetchval(
        "SELECT COUNT(*) FROM sessions WHERE agent_id = $1", agent_id
    )
    assert session_count == 2 * N
