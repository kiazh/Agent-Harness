"""FM4 eviction/wipe/search fixes (m4, m5, n7, n8).

RED before fix:
- m4: eviction token totals counted documents while candidates excluded
  them, so document tokens caused over-eviction of conversational chunks.
- m5: delete_chunks ran the chunk DELETE and image-cache DELETE as two
  independent statements (partial wipe on failure).
- n7: search_by_embedding applied the threshold after LIMIT, so it could
  return fewer than top_k even when qualifying rows existed.
- n8: a query that yields no tsquery terms (e.g. non-ASCII only) was
  dropped silently.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

from machina.core.context import ContextManager


def _chunk_row(session, *, index: int, chunk_type: str = "user_message", tokens: int = 1):
    return {
        "id": uuid.uuid4(),
        "agent_id": "harness",
        "chunk_type": chunk_type,
        "token_count": tokens,
        "created_at": datetime.now(UTC) + timedelta(seconds=index),
        "payload_msgpack": b"\x80",
        "embedding": None,
    }


async def test_eviction_totals_exclude_documents(monkeypatch):
    """m4: document tokens must not drive conversational over-eviction."""
    from machina.core import context

    session = uuid.uuid4()
    conversational = [_chunk_row(session, index=i) for i in range(12)]

    async def fetchrow(query, *args):
        assert "COUNT(*) AS total_chunks" in query
        if "chunk_type <> 'document'" in query:
            return {"total_chunks": 12, "total_tokens": 12}
        return {"total_chunks": 14, "total_tokens": 2012}

    database = MagicMock()
    database.fetchrow = AsyncMock(side_effect=fetchrow)
    database.fetch = AsyncMock(return_value=conversational)
    connection = AsyncMock()
    connection.transaction = MagicMock()
    connection.fetch = AsyncMock(return_value=[{"id": r["id"]} for r in conversational[:4]])
    database.acquire.return_value.__aenter__.return_value = connection
    monkeypatch.setattr(context, "db", database)

    evicted = await ContextManager().evict_old_chunks(session, max_tokens=20)
    assert evicted == 0, "documents must be excluded from the eviction budget"


async def test_delete_chunks_is_single_transaction(monkeypatch):
    """m5: both deletes run on one connection inside one transaction."""
    from machina.core import context

    database = MagicMock()
    connection = MagicMock()
    connection.execute = AsyncMock(side_effect=["DELETE 3", "DELETE 1"])
    connection.fetchval = AsyncMock(return_value=True)
    connection.transaction = MagicMock()
    database.acquire.return_value.__aenter__.return_value = connection
    monkeypatch.setattr(context, "db", database)

    count = await ContextManager().delete_chunks(uuid.uuid4())
    assert count == 3
    assert connection.transaction.call_count == 1
    statements = [call.args[0] for call in connection.execute.await_args_list]
    assert any("DELETE FROM context_chunks" in s for s in statements)
    assert any("DELETE FROM session_image_context" in s for s in statements)


async def test_delete_chunks_missing_image_table_skips_second_delete(monkeypatch):
    """m5: an unmigrated database wipes chunks without aborting the tx."""
    from machina.core import context

    database = MagicMock()
    connection = MagicMock()
    connection.execute = AsyncMock(return_value="DELETE 2")
    connection.fetchval = AsyncMock(return_value=False)
    connection.transaction = MagicMock()
    database.acquire.return_value.__aenter__.return_value = connection
    monkeypatch.setattr(context, "db", database)

    count = await ContextManager().delete_chunks(uuid.uuid4())
    assert count == 2
    statements = [call.args[0] for call in connection.execute.await_args_list]
    assert not any("session_image_context" in s for s in statements)


async def test_search_by_embedding_filters_threshold_in_sql(monkeypatch):
    """n7: threshold must be part of the SQL before LIMIT."""
    from machina.core import context

    database = MagicMock()
    database.fetch = AsyncMock(return_value=[])
    monkeypatch.setattr(context, "db", database)

    await ContextManager().search_by_embedding(
        uuid.uuid4(), [0.1, 0.2, 0.3], top_k=5, threshold=0.9
    )
    query, *args = database.fetch.await_args.args
    assert "$4" in query and 0.9 in args
    assert query.index("1 - (embedding <=> $1::vector) >=") < query.index("LIMIT")


async def test_search_archive_text_logs_when_no_terms(monkeypatch, caplog):
    """n8: dropped non-ASCII queries must be visible, not silent."""
    from machina.core import context

    database = MagicMock()
    database.fetch = AsyncMock(return_value=[])
    monkeypatch.setattr(context, "db", database)
    session = uuid.uuid4()

    with caplog.at_level(logging.WARNING, logger="machina.core.context"):
        results = await ContextManager().search_archive_text(session, "日本語のクエリ")
    assert results == []
    messages = [r.getMessage() for r in caplog.records]
    assert any(str(session) in m for m in messages), messages
    database.fetch.assert_not_awaited()


async def test_search_archive_text_normal_query_unchanged(monkeypatch):
    from machina.core import context

    database = MagicMock()
    database.fetch = AsyncMock(return_value=[])
    monkeypatch.setattr(context, "db", database)

    assert await ContextManager().search_archive_text(uuid.uuid4(), "hello world") == []
    query, *args = database.fetch.await_args.args
    assert "to_tsquery" in query and "hello | world" in args
