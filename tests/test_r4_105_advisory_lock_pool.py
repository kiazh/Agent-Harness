"""R4-105(e): advisory-lock pool path on a real DSN (bounded, private schema).

Already covered elsewhere: ``test_load_concurrency`` drives the usage
``pg_advisory_xact_lock`` ordering through a real pool (concurrent reserves,
exactly-one winner under a limit of 1), and ``test_memory_bug_fixes`` /
``test_fm5_consistency`` cover the consolidator's dedicated-connection lock
(the R10 starvation fix is proven in the review's ``memory_pool.py``, not the
pool path this task names).

Deterministic gap closed here: a REJECTED reservation must release both its
transaction-scoped advisory locks and its pooled connection. This suite runs
8 concurrent attempts through the shared pool (``max_size=3`` in the
``isolated_schema`` harness), so tasks genuinely queue for pool connections
while holding/waiting the advisory locks, then proves a later legitimate
reserve on the SAME scopes completes promptly. A session-scoped
(non-transactional) lock left behind by the rejected transaction — or a pool
connection pinned to it — would block that retry until the harness timeout.
The private schema keeps other test processes' migrations away from these
locks and rows.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
import pytest_asyncio

pytestmark = pytest.mark.db

N_CONCURRENT = 8
RETRY_TIMEOUT_SECONDS = 5.0


@pytest_asyncio.fixture
async def advisory_db():
    """Wire the shared ``db`` singleton to a PRIVATE test schema.

    Deliberately independent of the ``db_pool`` fixture (see the private
    schema rationale in ``test_r4_105_same_session_load_race``): public
    per-test DDL deadlocks against other processes' data writes here.
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


@pytest.mark.asyncio
async def test_rejected_reserve_releases_locks_and_connection_for_retry(advisory_db, monkeypatch):
    from machina.core.config import config
    from machina.core.exceptions import UsageBudgetExceededError
    from machina.core.session import session_manager
    from machina.core.usage import UsageStore

    agent_id = f"r4-105-lock-{uuid.uuid4()}"
    session = await session_manager.create(title="r4-105-lock", agent_id=agent_id)
    session_id = session.id
    messages = [{"role": "user", "content": "advisory lock probe"}]

    monkeypatch.setattr(config, "usage_session_request_limit", 1)
    monkeypatch.setattr(config, "usage_session_token_limit", 0)
    monkeypatch.setattr(config, "usage_agent_request_limit", 0)
    monkeypatch.setattr(config, "usage_agent_token_limit", 0)
    store = UsageStore()

    # N attempts on ONE session through a 3-connection pool: tasks queue for
    # both pooled connections and for the transaction-scoped advisory locks.
    results = await asyncio.gather(
        *(
            store.reserve(session_id, agent_id, "fake", "fake-model", messages, [], 8)
            for _ in range(N_CONCURRENT)
        ),
        return_exceptions=True,
    )
    winners = [r for r in results if isinstance(r, uuid.UUID)]
    rejected = [r for r in results if isinstance(r, UsageBudgetExceededError)]
    unexpected = [
        r
        for r in results
        if isinstance(r, Exception) and not isinstance(r, UsageBudgetExceededError)
    ]
    assert not unexpected, f"unexpected failures under pool contention: {unexpected}"
    assert len(winners) == 1, f"exactly one reservation must win: {winners}"
    assert len(rejected) == N_CONCURRENT - 1

    await store.finish(winners[0], {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})

    # Every rejected transaction rolled back: its advisory locks and its pool
    # connection must be free. A leaked lock would block this retry.
    monkeypatch.setattr(config, "usage_session_request_limit", 2)
    try:
        retry = await asyncio.wait_for(
            store.reserve(session_id, agent_id, "fake", "fake-model", messages, [], 8),
            timeout=RETRY_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        pytest.fail(
            "retry blocked: a rejected reservation left its advisory lock or pool connection behind"
        )
    assert retry is not None
    await store.finish(retry, {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})

    summary = await store.summary(session_id, agent_id)
    assert summary["session"]["requests"] == 2
    orphans = await advisory_db.fetchval(
        "SELECT COUNT(*) FROM llm_usage WHERE session_id = $1 AND status = 'reserved'",
        session_id,
    )
    assert orphans == 0, f"{orphans} orphaned reserved rows (locks or accounting stuck)"
