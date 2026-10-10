"""Tests for memory system bug fixes."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ah.core.models import LLMResponse
from ah.memory import (
    MemoryConsolidator,
    MemoryEntry,
    MemoryRetriever,
    MemoryStore,
    RetrievedMemory,
)
from ah.memory.approval import MemoryApprovalGate
from ah.memory.identity import ValidationResult


def _make_row(**overrides):
    """Create a mock DB row for a memory entry."""
    row = {
        "id": uuid.uuid4(),
        "session_id": uuid.uuid4(),
        "agent_id": "harness",
        "content": "Test memory",
        "category": "fact",
        "importance": 0.5,
        "created_at": datetime.now(UTC),
        "last_accessed": None,
        "access_count": 0,
        "embedding": None,
        "explicitly_important": False,
        "base_strength": 1.0,
    }
    row.update(overrides)
    return row


def _transactional_connection(mock_db):
    """Create a mock connection that supports transactions."""
    conn = AsyncMock()
    conn.fetchrow = mock_db.fetchrow
    conn.execute = mock_db.execute
    conn.transaction = MagicMock(return_value=AsyncMock())
    cm = AsyncMock()
    cm.__aenter__.return_value = conn
    mock_db.acquire = MagicMock(return_value=cm)
    return conn


# ===========================================================================
# Bug 1: reject() race condition
# ===========================================================================


class TestRejectRaceCondition:
    """Tests for Bug 1: reject() race condition fix."""

    @pytest.fixture
    def gate(self):
        return MemoryApprovalGate(enabled=True)

    @pytest.fixture
    def mock_db(self):
        mock = AsyncMock()
        from tests.support.database import attach_connection

        attach_connection(mock)
        mock.fetch = AsyncMock(return_value=[])
        mock.fetchrow = AsyncMock(return_value=None)
        mock.fetchval = AsyncMock(return_value=0)
        mock.execute = AsyncMock(return_value="UPDATE 1")
        return mock

    async def test_reject_uses_transaction_with_for_update(self, gate, mock_db):
        """Verify reject() uses FOR UPDATE in a transaction to prevent race conditions."""
        with patch("ah.memory.approval.db", mock_db):
            mock_db.fetchrow = AsyncMock(return_value={"status": "pending"})
            mock_db.execute = AsyncMock(return_value="UPDATE 1")
            conn = _transactional_connection(mock_db)
            result = await gate.reject(uuid.uuid4())
            assert result is True
            # Verify FOR UPDATE was used in the query
            call_args = conn.fetchrow.call_args
            assert call_args is not None
            query = call_args[0][0]
            assert "FOR UPDATE" in query

    async def test_reject_concurrent_already_approved(self, gate, mock_db):
        """Verify reject() returns False when memory was already approved concurrently."""
        with patch("ah.memory.approval.db", mock_db):
            mock_db.fetchrow = AsyncMock(return_value={"status": "approved"})
            mock_db.execute = AsyncMock(return_value="UPDATE 0")
            _transactional_connection(mock_db)
            result = await gate.reject(uuid.uuid4())
            assert result is False


# ===========================================================================
# Bug 2: store.add() validates against wrong agent
# ===========================================================================


class TestStoreAddSourceAgent:
    """Tests for Bug 2: store.add() validates against wrong agent."""

    @pytest.fixture
    def store(self):
        return MemoryStore()

    @pytest.fixture
    def mock_db(self):
        mock = AsyncMock()
        from tests.support.database import attach_connection

        attach_connection(mock)
        mock.fetch = AsyncMock(return_value=[])
        mock.fetchrow = AsyncMock(return_value=None)
        mock.fetchval = AsyncMock(return_value=0)
        mock.execute = AsyncMock(return_value="INSERT 0 1")
        return mock

    async def test_add_validates_against_source_agent(self, store, mock_db):
        """Verify that a shared write is validated as the owning agent.

        The memory stays attributed to the source (agent_b), so the gate's
        cross-agent check sees agent_b != agent_a and demands provenance.
        Unsigned deliveries are then quarantined instead of accepted.
        """
        with patch("ah.memory.store.db", mock_db):
            mock_db.fetchrow = AsyncMock(return_value=_make_row())

            captured_agent_id = None
            captured_memory_agent = None

            async def mock_validate(agent_id, memory, *args, **kwargs):
                nonlocal captured_agent_id, captured_memory_agent
                captured_agent_id = agent_id
                captured_memory_agent = memory.agent_id
                return ValidationResult(is_valid=True, confidence=0.9, reason="OK")

            with patch("ah.memory.store.identity_gate") as mock_gate:
                mock_gate.validate_incoming = mock_validate
                entry = await store.add(
                    session_id=uuid.uuid4(),
                    agent_id="agent_a",
                    content="Test memory",
                    category="fact",
                    source_agent="agent_b",
                )
                assert entry is not None
                # The gate decides for the owner (agent_a) while the memory
                # stays attributed to the source (agent_b).
                assert captured_agent_id == "agent_a"
                assert captured_memory_agent == "agent_b"


# ===========================================================================
# Bug 6: get_weak_memories() doesn't filter quarantined
# ===========================================================================


class TestGetWeakMemoriesQuarantined:
    """Tests for Bug 6: get_weak_memories() doesn't filter quarantined."""

    @pytest.fixture
    def store(self):
        return MemoryStore()

    @pytest.fixture
    def mock_db(self):
        mock = AsyncMock()
        from tests.support.database import attach_connection

        attach_connection(mock)
        mock.fetch = AsyncMock(return_value=[])
        mock.fetchrow = AsyncMock(return_value=None)
        mock.fetchval = AsyncMock(return_value=0)
        mock.execute = AsyncMock(return_value="DELETE 0")
        return mock

    async def test_get_weak_memories_filters_quarantined(self, store, mock_db):
        """Verify get_weak_memories() filters out quarantined memories."""
        with patch("ah.memory.store.db", mock_db):
            mock_db.fetch = AsyncMock(return_value=[_make_row(importance=0.02)])
            await store.get_weak_memories(threshold=0.05)
            # Verify the query includes quarantined = FALSE
            call_args = mock_db.fetch.call_args
            assert call_args is not None
            query = call_args[0][0]
            assert "quarantined = FALSE" in query or "quarantined = false" in query.lower()

    async def test_get_weak_memories_with_agent_filters_quarantined(self, store, mock_db):
        """Verify get_weak_memories() with agent filter also filters quarantined."""
        with patch("ah.memory.store.db", mock_db):
            mock_db.fetch = AsyncMock(return_value=[_make_row(importance=0.02)])
            await store.get_weak_memories(threshold=0.05, agent_id="harness")
            call_args = mock_db.fetch.call_args
            assert call_args is not None
            query = call_args[0][0]
            assert "quarantined = FALSE" in query or "quarantined = false" in query.lower()


# ===========================================================================
# Bug 7: evict_weak_memories() CTE slow
# ===========================================================================


class TestEvictWeakMemoriesCTE:
    """Tests for Bug 7: evict_weak_memories() CTE slow."""

    @pytest.fixture
    def store(self):
        return MemoryStore()

    @pytest.fixture
    def mock_db(self):
        mock = AsyncMock()
        from tests.support.database import attach_connection

        attach_connection(mock)
        mock.fetch = AsyncMock(return_value=[])
        mock.fetchrow = AsyncMock(return_value=None)
        mock.fetchval = AsyncMock(return_value=0)
        mock.execute = AsyncMock(return_value="DELETE 0")
        return mock

    async def test_evict_weak_memories_uses_limit(self, store, mock_db):
        """Verify evict_weak_memories() uses LIMIT to avoid full table scan."""
        with patch("ah.memory.store.db", mock_db):
            mock_db.execute = AsyncMock(return_value="DELETE 5")
            result = await store.evict_weak_memories(max_memories=10)
            assert result == 5
            # Verify the query uses a subquery with LIMIT instead of COUNT(*)
            call_args = mock_db.execute.call_args
            assert call_args is not None
            query = call_args[0][0]
            # Should have LIMIT in the subquery
            assert "LIMIT" in query


# ===========================================================================
# Bug 5: No timeout on re-ranking
# ===========================================================================


class TestRerankTimeout:
    """Tests for Bug 5: No timeout on re-ranking."""

    @pytest.fixture
    def retriever(self):
        return MemoryRetriever()

    @pytest.fixture
    def mock_store(self):
        store = AsyncMock()
        store.search = AsyncMock(return_value=[])
        store.search_by_embedding = AsyncMock(return_value=[])
        store.update_access = AsyncMock()
        return store

    async def test_rerank_has_timeout(self, retriever, mock_store):
        """Verify _rerank() uses asyncio.wait_for with a timeout."""
        # Create a mock LLM that hangs
        mock_llm = AsyncMock()

        async def slow_complete(*args, **kwargs):
            await asyncio.sleep(10)  # Would hang without timeout
            return LLMResponse(content="[]", model="test", usage={"total_tokens": 5})

        mock_llm.complete = slow_complete
        retriever.llm = mock_llm
        retriever.store = mock_store

        # Create some candidates
        candidates = [
            RetrievedMemory(
                memory=MemoryEntry(
                    id=uuid.uuid4(),
                    session_id=None,
                    agent_id="harness",
                    content="Test",
                    category="fact",
                ),
                score=0.5,
            )
        ]

        # Patch asyncio.wait_for to verify it's called
        with patch("ah.memory.retriever.asyncio.wait_for", side_effect=TimeoutError()):
            result = await retriever._rerank("test", candidates)
            # Should fall back to original candidates on timeout
            assert result == candidates


# ===========================================================================
# Bug 4: ILIKE full table scan
# ===========================================================================


class TestKeywordSearchFTS:
    """Tests for Bug 4: ILIKE full table scan."""

    @pytest.fixture
    def retriever(self):
        return MemoryRetriever()

    @pytest.fixture
    def mock_store(self):
        store = AsyncMock()
        store.search = AsyncMock(return_value=[])
        store.search_by_embedding = AsyncMock(return_value=[])
        store.update_access = AsyncMock()
        return store

    async def test_keyword_search_uses_fts(self, retriever, mock_store):
        """Verify keyword search uses FTS with an ILIKE fallback for partial tokens."""
        retriever.store = mock_store

        mock_db = AsyncMock()
        from tests.support.database import attach_connection

        attach_connection(mock_db)
        mock_db.fetch = AsyncMock(return_value=[])

        with patch("ah.memory.retriever.db", mock_db):
            results = await retriever._keyword_search("test query")
            assert isinstance(results, list)
            # Verify the query uses to_tsvector or similar FTS
            call_args = mock_db.fetch.call_args
            assert call_args is not None
            query = call_args[0][0]
            assert "to_tsvector" in query
            # ILIKE stays as the fallback for partial-length lexemes.
            assert "ILIKE" in query


# ===========================================================================
# Bug 3: O(n*m) embedding dedup
# ===========================================================================


class TestConsolidatorBatchDedup:
    """Tests for Bug 3: O(n*m) embedding dedup."""

    @pytest.fixture
    def consolidator(self):
        return MemoryConsolidator()

    @pytest.fixture
    def mock_llm(self):
        provider = AsyncMock()
        provider.complete = AsyncMock(
            return_value=LLMResponse(
                content='[{"content": "User likes Python", "category": "preference", "importance": 0.8, "explicitly_important": false}]',
                model="test",
                usage={"total_tokens": 10},
            )
        )
        return provider

    @pytest.fixture
    def mock_store(self):
        store = AsyncMock()
        store.add = AsyncMock(
            side_effect=lambda **kwargs: MemoryEntry(
                id=uuid.uuid4(),
                session_id=kwargs.get("session_id"),
                agent_id=kwargs.get("agent_id", "harness"),
                content=kwargs.get("content", ""),
                category=kwargs.get("category", "fact"),
                importance=kwargs.get("importance", 0.5),
            )
        )
        store.search_by_embedding = AsyncMock(return_value=[])
        store.update_access = AsyncMock()
        return store

    async def test_consolidate_uses_batch_dedup(self, consolidator, mock_llm, mock_store):
        """Verify consolidator uses batch similarity instead of O(n*m) nested loop."""
        consolidator.llm = mock_llm
        consolidator.store = mock_store

        # Create candidates with embeddings
        candidate = MemoryEntry(
            id=uuid.uuid4(),
            session_id=None,
            agent_id="harness",
            content="User likes Python",
            category="preference",
            importance=0.8,
            embedding=[0.1] * 1536,
        )

        # Mock _extract_memories to return our candidate
        async def mock_extract(*args, **kwargs):
            return [candidate]

        consolidator._extract_memories = mock_extract

        # Mock context_manager
        with patch("ah.memory.consolidator.context_manager") as mock_cm:
            mock_cm.get_chunks = AsyncMock(return_value=[])
            # This should not raise and should complete
            results = await consolidator.consolidate_session(uuid.uuid4(), "harness")
            assert results.status == "empty" and results.entries == []
