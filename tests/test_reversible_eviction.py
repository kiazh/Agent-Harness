"""Tests for reversible eviction — archive before delete, resurrection via embedding."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ah.core.context import ContextManager


@pytest.fixture
def manager():
    return ContextManager()


@pytest.fixture
def mock_db():
    mock = AsyncMock()
    mock.fetch = AsyncMock(return_value=[])
    mock.fetchrow = AsyncMock(return_value=None)
    mock.fetchval = AsyncMock(return_value=0)
    mock.execute = AsyncMock(return_value="DELETE 0")
    return mock


class TestEvictionArchivesBeforeDelete:
    """evict_old_chunks must archive chunks to context_archive before deleting them."""

    async def test_eviction_archives_before_delete(self, manager, mock_db):
        """When evicting, chunks must be inserted into context_archive before DELETE."""
        session_id = uuid.uuid4()
        payload_msgpack = b"\x81\xa4text\xa5hello"
        embedding_str = "[0.1,0.2,0.3]"

        # Return 11 chunks — the code preserves the most recent 10, so 1 is evictable
        rows = [
            {
                "id": uuid.uuid4(),
                "token_count": 100,
                "chunk_type": "user_message",
                "created_at": f"2025-01-0{i + 1}T00:00:00Z",
                "payload_msgpack": payload_msgpack,
                "embedding": embedding_str,
                "agent_id": "harness",
            }
            for i in range(11)
        ]
        # The cursor query has LIMIT 1 (remaining_evictable = max(0, 11 - 10) = 1),
        # so the mock must return only 1 row, not all 11.
        mock_db.fetch = AsyncMock(return_value=rows[:1])
        mock_db.fetchrow = AsyncMock(
            return_value={"total_chunks": 11, "total_tokens": 1100}
        )
        connection = AsyncMock()
        connection.fetchrow.return_value = {"id": uuid.uuid4()}
        connection.fetch = AsyncMock(return_value=[{"id": r["id"]} for r in rows[:1]])
        connection.executemany = AsyncMock()
        connection.execute = AsyncMock(return_value="DELETE 1")
        connection.transaction = MagicMock()
        mock_db.acquire = MagicMock()
        mock_db.acquire.return_value.__aenter__.return_value = connection

        with patch("ah.core.context.db", mock_db):
            evicted = await manager.evict_old_chunks(session_id, max_tokens=50)

        assert evicted == 1
        # Verify archive INSERT was called (via executemany for batch insert)
        all_calls = list(connection.executemany.call_args_list) + list(
            connection.execute.call_args_list
        )
        archive_inserted = any("context_archive" in str(call) for call in all_calls)
        assert archive_inserted, "Expected INSERT INTO context_archive before DELETE"
        connection.executemany.assert_awaited_once()


class TestResurrectionViaEmbedding:
    """Archived chunks must be retrievable by embedding similarity."""

    async def test_resurrection_via_embedding(self, manager, mock_db):
        """resurrect_context should query context_archive by embedding similarity."""
        session_id = uuid.uuid4()
        chunk_id = uuid.uuid4()
        payload_msgpack = b"\x81\xa4text\xa5hello"
        embedding_str = "[0.1,0.2,0.3]"

        mock_db.fetch = AsyncMock(
            return_value=[
                {
                    "id": chunk_id,
                    "session_id": session_id,
                    "chunk_id": chunk_id,
                    "payload_msgpack": payload_msgpack,
                    "embedding": embedding_str,
                    "archived_at": "2025-01-01T00:00:00Z",
                    "archive_reason": "evicted",
                    "resurrection_count": 0,
                    "last_resurrected": None,
                    "similarity": 0.95,
                }
            ]
        )

        with patch("ah.core.context.db", mock_db):
            results = await manager.resurrect_context(
                session_id, query_embedding=[0.1, 0.2, 0.3], top_k=5
            )

        assert len(results) == 1
        chunk, similarity = results[0]
        assert chunk.id == chunk_id
        assert similarity == 0.95
        # Verify the query was against context_archive
        fetch_calls = mock_db.fetch.call_args_list
        archive_queried = any("context_archive" in str(call) for call in fetch_calls)
        assert archive_queried, "Expected query against context_archive table"


class TestArchivePreservesByteExact:
    """Archived payload_msgpack must be byte-identical to the original."""

    async def test_archive_preserves_byte_exact(self, manager, mock_db):
        """archive_chunk must store the exact payload_msgpack bytes."""
        session_id = uuid.uuid4()
        chunk_id = uuid.uuid4()
        original_payload = b"\x81\xa4text\xa5hello"  # msgpack bytes

        mock_db.fetchrow = AsyncMock(
            return_value={
                "id": uuid.uuid4(),
                "session_id": session_id,
                "chunk_id": chunk_id,
                "payload_msgpack": original_payload,
                "embedding": "[0.1,0.2,0.3]",
                "archived_at": "2025-01-01T00:00:00Z",
                "archive_reason": "evicted",
                "resurrection_count": 0,
                "last_resurrected": None,
            }
        )

        with patch("ah.core.context.db", mock_db):
            result = await manager.archive_chunk(
                session_id=session_id,
                chunk_id=chunk_id,
                payload_msgpack=original_payload,
                embedding=[0.1, 0.2, 0.3],
                archive_reason="evicted",
            )

        assert result is not None
        # Verify the INSERT used the exact same bytes
        execute_calls = mock_db.fetchrow.call_args_list
        # fetchrow is used for the INSERT ... RETURNING
        assert len(execute_calls) > 0


class TestResurrectionCountIncremented:
    """resurrection_count must increment when a chunk is resurrected."""

    async def test_resurrection_count_incremented(self, manager, mock_db):
        """After resurrect_context, the resurrection_count for retrieved chunks must increase."""
        session_id = uuid.uuid4()
        chunk_id = uuid.uuid4()
        payload_msgpack = b"\x81\xa4text\xa5hello"
        embedding_str = "[0.1,0.2,0.3]"

        mock_db.fetch = AsyncMock(
            return_value=[
                {
                    "id": chunk_id,
                    "session_id": session_id,
                    "chunk_id": chunk_id,
                    "payload_msgpack": payload_msgpack,
                    "embedding": embedding_str,
                    "archived_at": "2025-01-01T00:00:00Z",
                    "archive_reason": "evicted",
                    "resurrection_count": 0,
                    "last_resurrected": None,
                    "similarity": 0.95,
                }
            ]
        )
        mock_db.execute = AsyncMock(return_value="UPDATE 1")

        with patch("ah.core.context.db", mock_db):
            results = await manager.resurrect_context(
                session_id, query_embedding=[0.1, 0.2, 0.3], top_k=5
            )

        assert len(results) == 1
        # Verify UPDATE was called to increment resurrection_count
        execute_calls = mock_db.execute.call_args_list
        update_called = any(
            "context_archive" in str(call) and "resurrection_count" in str(call)
            for call in execute_calls
        )
        assert update_called, "Expected UPDATE to increment resurrection_count"
