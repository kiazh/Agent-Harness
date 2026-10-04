"""UsageStore cleanup: mark orphaned reserved rows as failed."""
from __future__ import annotations

import os
import uuid
from unittest.mock import AsyncMock

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
async def test_cleanup_orphaned_reservations_marks_them_failed(db_pool):
    """cleanup_orphaned_reservations() should mark all 'reserved' rows as 'error'."""
    from ah.core.usage import UsageStore

    store = UsageStore()
    session_id = uuid.uuid4()
    agent_id = f"cleanup-test-{session_id}"

    # Create some reserved rows
    res1 = await store.reserve(session_id, agent_id, "fake", "fake", [{"role": "user", "content": "hi"}], [], 8)
    res2 = await store.reserve(session_id, agent_id, "fake", "fake", [{"role": "user", "content": "hi"}], [], 8)

    # Create a completed row (should NOT be touched)
    res3 = await store.reserve(session_id, agent_id, "fake", "fake", [{"role": "user", "content": "hi"}], [], 8)
    await store.finish(res3, {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7})

    # Run cleanup
    cleaned = await store.cleanup_orphaned_reservations()

    # Verify reserved rows are now error
    row1 = await db_pool.fetchrow("SELECT status FROM llm_usage WHERE id = $1", res1)
    row2 = await db_pool.fetchrow("SELECT status FROM llm_usage WHERE id = $1", res2)
    row3 = await db_pool.fetchrow("SELECT status FROM llm_usage WHERE id = $1", res3)

    assert row1["status"] == "error"
    assert row2["status"] == "error"
    assert row3["status"] == "complete"  # untouched
    assert cleaned == 2

    # Cleanup
    await db_pool.execute("DELETE FROM llm_usage WHERE session_id = $1", session_id)


@needs_db
@pytest.mark.usefixtures("connected_db")
@pytest.mark.asyncio
async def test_cleanup_orphaned_reservations_returns_zero_when_none(db_pool):
    """cleanup_orphaned_reservations() should return 0 when no reserved rows exist."""
    from ah.core.usage import UsageStore

    store = UsageStore()
    cleaned = await store.cleanup_orphaned_reservations()
    assert cleaned == 0


@pytest.mark.asyncio
async def test_gateway_startup_calls_cleanup(monkeypatch):
    """Gateway._initialize() should call cleanup_orphaned_reservations() on startup."""
    from ah.gateway.server import Gateway

    mock_cleanup = AsyncMock(return_value=0)
    monkeypatch.setattr("ah.core.usage.usage_store.cleanup_orphaned_reservations", mock_cleanup)

    frames = []
    gateway = Gateway(frames.append, owns_db=False)
    gateway._db_ready = False  # Force the DB path

    # Mock db.connect to avoid real DB
    import ah.db.connection as db_mod
    original_connect = db_mod.db.connect
    db_mod.db.connect = AsyncMock()

    # Mock the connected property
    original_connected = type(db_mod.db).connected
    type(db_mod.db).connected = property(lambda self: True)

    try:
        await gateway._initialize({})
        mock_cleanup.assert_awaited_once()
    finally:
        db_mod.db.connect = original_connect
        type(db_mod.db).connected = original_connected
