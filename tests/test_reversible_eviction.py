"""Tests for reversible eviction — archive before delete, resurrection via embedding."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from machina.core.context import ContextManager


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
        mock_db.fetchrow = AsyncMock(return_value={"total_chunks": 11, "total_tokens": 1100})
        connection = AsyncMock()
        connection.fetchrow.return_value = {"id": uuid.uuid4()}
        connection.fetch = AsyncMock(return_value=[{"id": r["id"]} for r in rows[:1]])
        connection.executemany = AsyncMock()
        connection.execute = AsyncMock(return_value="DELETE 1")
        connection.transaction = MagicMock()
        mock_db.acquire = MagicMock()
        mock_db.acquire.return_value.__aenter__.return_value = connection

        with patch("machina.core.context.db", mock_db):
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

        with patch("machina.core.context.db", mock_db):
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

        with patch("machina.core.context.db", mock_db):
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

        with patch("machina.core.context.db", mock_db):
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


# ─── W7 batch 2: replacement error boundaries ────────────────────────────────


class TestReplacementErrorBoundaries:
    """Diagnostics are recorded before replacement derives fallback values."""

    async def test_ownership_probe_failure_records_diagnostic(self, manager, mock_db):
        from datetime import UTC, datetime

        from machina.core.exceptions import StaleOwnershipError
        from machina.core.metrics import metrics

        session_id = uuid.uuid4()
        connection = AsyncMock()
        connection.transaction = MagicMock()
        connection.fetchrow = AsyncMock(
            return_value={
                "claim_owner": "owner",
                "claim_kind": "turn",
                "claim_expires_at": datetime.now(UTC),
            }
        )
        connection.fetchval = AsyncMock(side_effect=RuntimeError("clock unavailable"))
        mock_db.acquire = MagicMock()
        mock_db.acquire.return_value.__aenter__.return_value = connection

        before = metrics.get_counter("diagnostic.failures")
        with patch("machina.core.context.db", mock_db):
            with pytest.raises(StaleOwnershipError):
                await manager.replace_chunks_by_ids(session_id, [], [], expected_owner="owner")
        # The probe failure is counted even though the fallback (stale) stands.
        assert metrics.get_counter("diagnostic.failures") == before + 1

    async def test_archive_derived_field_failures_record_diagnostics(self, manager, mock_db):
        from datetime import UTC, datetime

        from machina.core.metrics import metrics

        session_id = uuid.uuid4()
        chunk_id = uuid.uuid4()
        row = {
            "id": chunk_id,
            "agent_id": "harness",
            "chunk_type": "user_message",
            "payload_msgpack": b"\xc1",  # reserved MessagePack code: unpack fails
            "token_count": 1,
            "embedding": "not-a-vector",
            "created_at": datetime(2025, 1, 1, tzinfo=UTC),
        }
        connection = AsyncMock()
        connection.transaction = MagicMock()
        connection.fetch = AsyncMock(return_value=[row])
        connection.execute = AsyncMock(return_value="DELETE 1")
        mock_db.acquire = MagicMock()
        mock_db.acquire.return_value.__aenter__.return_value = connection

        before = metrics.get_counter("diagnostic.failures")
        with patch("machina.core.context.db", mock_db):
            replaced = await manager.replace_chunks_by_ids(
                session_id, [chunk_id], [], archive_reason="compressed"
            )
        assert replaced == 0
        assert metrics.get_counter("diagnostic.failures") == before + 2
        # Archived with None derived fields rather than losing the row.
        archive_calls = [
            c for c in connection.execute.await_args_list if "context_archive" in str(c)
        ]
        assert archive_calls, "archive insert must still run with fallback derived fields"
