"""UsageStore summary() only includes complete rows."""
from __future__ import annotations

import os
import uuid

import pytest

TEST_DSN = os.environ.get("AGENT_HARNESS_TEST_DATABASE_URL", "")
needs_db = pytest.mark.skipif(not TEST_DSN, reason="AGENT_HARNESS_TEST_DATABASE_URL not set")


@pytest.fixture
async def connected_db():
    from ah.db.connection import db

    await db.connect()
    yield
    await db.close()


@needs_db
@pytest.mark.usefixtures("connected_db")
@pytest.mark.asyncio
async def test_summary_excludes_reserved_rows(db_pool):
    """summary() should only count rows with status='complete', not 'reserved' or 'error'."""
    from ah.core.usage import UsageStore

    store = UsageStore()
    session_id = uuid.uuid4()
    agent_id = f"summary-test-{session_id}"

    # Create a reserved row (should NOT be counted)
    await store.reserve(session_id, agent_id, "fake", "fake", [{"role": "user", "content": "hi"}], [], 8)

    # Create a completed row (should be counted)
    res2 = await store.reserve(session_id, agent_id, "fake", "fake", [{"role": "user", "content": "hi"}], [], 8)
    await store.finish(res2, {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7})

    # Create an error row (should NOT be counted)
    res3 = await store.reserve(session_id, agent_id, "fake", "fake", [{"role": "user", "content": "hi"}], [], 8)
    await store.finish(res3, failed=True)

    summary = await store.summary(session_id, agent_id)

    # Only the completed row should be counted
    assert summary["session"]["requests"] == 1
    assert summary["session"]["accountedTokens"] == 7
    assert summary["agent"]["requests"] == 1
    assert summary["agent"]["accountedTokens"] == 7

    # Cleanup
    await db_pool.execute("DELETE FROM llm_usage WHERE session_id = $1", session_id)


@needs_db
@pytest.mark.usefixtures("connected_db")
@pytest.mark.asyncio
async def test_summary_counts_only_complete_rows(db_pool):
    """summary() should count only complete rows, not reserved or error rows."""
    from ah.core.usage import UsageStore

    store = UsageStore()
    session_id = uuid.uuid4()
    agent_id = f"summary-test2-{session_id}"

    # Create multiple completed rows
    for i in range(3):
        res = await store.reserve(session_id, agent_id, "fake", "fake", [{"role": "user", "content": "hi"}], [], 8)
        await store.finish(res, {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7})

    # Create reserved and error rows
    await store.reserve(session_id, agent_id, "fake", "fake", [{"role": "user", "content": "hi"}], [], 8)
    res_err = await store.reserve(session_id, agent_id, "fake", "fake", [{"role": "user", "content": "hi"}], [], 8)
    await store.finish(res_err, failed=True)

    summary = await store.summary(session_id, agent_id)

    # Only 3 complete rows should be counted
    assert summary["session"]["requests"] == 3
    assert summary["session"]["accountedTokens"] == 21  # 3 * 7
    assert summary["agent"]["requests"] == 3
    assert summary["agent"]["accountedTokens"] == 21

    # Cleanup
    await db_pool.execute("DELETE FROM llm_usage WHERE session_id = $1", session_id)
