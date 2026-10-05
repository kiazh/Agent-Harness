"""Eviction pages old chunks and archives a successful page atomically."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

from ah.core.context import ContextManager
from ah.core.session import SessionManager


async def test_eviction_queries_a_bounded_page(monkeypatch):
    from ah.core import context

    database = MagicMock()
    database.fetchrow = AsyncMock(return_value={"total_chunks": 1000, "total_tokens": 0})
    database.fetch = AsyncMock(return_value=[])
    monkeypatch.setattr(context, "db", database)
    manager = ContextManager()
    assert await manager.evict_old_chunks(uuid.uuid4(), max_chunks=1) == 0
    assert "LIMIT" in database.fetch.await_args.args[0]


async def test_eviction_uses_one_transaction_for_a_page(monkeypatch):
    from ah.core import context

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
    monkeypatch.setattr("ah.core.context.db", db_pool)
    monkeypatch.setattr("ah.core.session.db", db_pool)
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
                    "document",
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
