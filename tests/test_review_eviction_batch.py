"""Eviction pages old chunks and archives a successful page atomically."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

from machina.core.context import ContextManager
from machina.core.session import SessionManager


async def test_eviction_queries_a_bounded_page(monkeypatch):
    from machina.core import context

    database = MagicMock()
    database.fetchrow = AsyncMock(return_value={"total_chunks": 1000, "total_tokens": 0})
    database.fetch = AsyncMock(return_value=[])
    monkeypatch.setattr(context, "db", database)
    manager = ContextManager()
    assert await manager.evict_old_chunks(uuid.uuid4(), max_chunks=1) == 0
    assert "LIMIT" in database.fetch.await_args.args[0]


async def test_eviction_uses_one_transaction_for_a_page(monkeypatch):
    from machina.core import context

    session = uuid.uuid4()
    now = datetime.now(UTC)
    rows = [
        {
            "id": uuid.uuid4(),
            "agent_id": "owner",
            "chunk_type": "document",
            "token_count": 1,
            "created_at": now + timedelta(seconds=i),
            "payload_msgpack": b"\x80",
            "embedding": None,
        }
        for i in range(12)
    ]
    database = MagicMock()
    database.fetchrow = AsyncMock(return_value={"total_chunks": 12, "total_tokens": 12})
    database.fetch = AsyncMock(return_value=rows)
    connection = AsyncMock()
    connection.transaction = MagicMock()
    connection.fetch = AsyncMock(return_value=[{"id": row["id"]} for row in rows])
    database.acquire.return_value.__aenter__.return_value = connection
    monkeypatch.setattr(context, "db", database)
    manager = ContextManager()
    assert await manager.evict_old_chunks(session, max_chunks=10) == 2
    assert database.acquire.call_count == 1
    assert connection.transaction.call_count == 1


async def test_eviction_pages_across_more_than_one_hundred_chunks(db_pool, monkeypatch):
    assert (
        await db_pool.fetchval("SELECT to_regclass('idx_context_chunks_session_eviction')")
        is not None
    )
    monkeypatch.setattr("machina.core.context.db", db_pool)
    monkeypatch.setattr("machina.core.session.db", db_pool)
    session = await SessionManager().create(title="paged eviction", agent_id="owner")
    try:
        start = datetime(2025, 1, 1, tzinfo=UTC)
        await db_pool.executemany(
            "INSERT INTO context_chunks "
            "(id, session_id, agent_id, chunk_type, payload_msgpack, token_count, created_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7)",
            [
                (
                    uuid.uuid4(),
                    session.id,
                    "owner",
                    # Conversational scope: eviction never selects indexed
                    # documents (AH-AUDIT-034); pagination filler uses a
                    # compactable conversational type.
                    "user_message",
                    b"\x80",
                    1,
                    start + timedelta(seconds=i),
                )
                for i in range(112)
            ],
        )
        assert await ContextManager().evict_old_chunks(session.id, max_chunks=10) == 102
        assert (
            await db_pool.fetchval(
                "SELECT count(*) FROM context_chunks WHERE session_id = $1", session.id
            )
            == 10
        )
        assert (
            await db_pool.fetchval(
                "SELECT count(*) FROM context_archive WHERE session_id = $1", session.id
            )
            == 102
        )
    finally:
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session.id)


async def test_batch_and_individual_archive_failures_log_tracebacks(monkeypatch, caplog):
    """W7: archive-failure warnings carry the traceback, not just the message."""
    import logging

    from machina.core import context

    session = uuid.uuid4()
    now = datetime.now(UTC)
    rows = [
        {
            "id": uuid.uuid4(),
            "agent_id": "owner",
            "chunk_type": "user_message",
            "token_count": 1,
            "created_at": now + timedelta(seconds=i),
            "payload_msgpack": b"\x80",
            "embedding": None,
        }
        for i in range(12)
    ]
    database = MagicMock()
    database.fetchrow = AsyncMock(return_value={"total_chunks": 12, "total_tokens": 12})
    database.fetch = AsyncMock(return_value=rows)
    connection = AsyncMock()
    connection.transaction = MagicMock()
    connection.fetch = AsyncMock(return_value=[{"id": row["id"]} for row in rows])
    connection.executemany = AsyncMock(side_effect=RuntimeError("batch insert failed"))
    connection.fetchval = AsyncMock(return_value=rows[0]["id"])  # per-row claim
    connection.fetchrow = AsyncMock(return_value=None)  # archive_chunk returns None
    database.acquire.return_value.__aenter__.return_value = connection
    monkeypatch.setattr(context, "db", database)

    with caplog.at_level(logging.WARNING, logger="machina.core.context"):
        evicted = await ContextManager().evict_old_chunks(session, max_chunks=2)
    assert evicted == 0
    batch = [r for r in caplog.records if "Batch archive failed" in r.getMessage()]
    individual = [r for r in caplog.records if "Failed to archive chunk" in r.getMessage()]
    # The safe-logging factory moves exc_info into a sanitized traceback
    # (class + frames): its presence is the exc_info=True pin.
    assert batch and batch[0].exc_text, "batch archive warning must include the traceback"
    assert "RuntimeError" in batch[0].exc_text
    assert individual and individual[0].exc_text, "per-chunk warning must include the traceback"
    assert "RuntimeError" in individual[0].exc_text
