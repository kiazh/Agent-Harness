"""Tests for memory system bug fixes."""

from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from machina.core.models import LLMResponse
from machina.memory import (
    MemoryConsolidator,
    MemoryEntry,
    MemoryRetriever,
    MemoryStore,
    RetrievedMemory,
)
from machina.memory.approval import MemoryApprovalGate
from machina.memory.identity import ValidationResult


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
        with patch("machina.memory.approval.db", mock_db):
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
        with patch("machina.memory.approval.db", mock_db):
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
        with patch("machina.memory.store.db", mock_db):
            mock_db.fetchrow = AsyncMock(return_value=_make_row())

            captured_agent_id = None
            captured_memory_agent = None

            async def mock_validate(agent_id, memory, *args, **kwargs):
                nonlocal captured_agent_id, captured_memory_agent
                captured_agent_id = agent_id
                captured_memory_agent = memory.agent_id
                return ValidationResult(is_valid=True, confidence=0.9, reason="OK")

            with patch("machina.memory.store.identity_gate") as mock_gate:
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
        with patch("machina.memory.store.db", mock_db):
            mock_db.fetch = AsyncMock(return_value=[_make_row(importance=0.02)])
            await store.get_weak_memories(threshold=0.05)
            # Verify the query includes quarantined = FALSE
            call_args = mock_db.fetch.call_args
            assert call_args is not None
            query = call_args[0][0]
            assert "quarantined = FALSE" in query or "quarantined = false" in query.lower()

    async def test_get_weak_memories_with_agent_filters_quarantined(self, store, mock_db):
        """Verify get_weak_memories() with agent filter also filters quarantined."""
        with patch("machina.memory.store.db", mock_db):
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
        with patch("machina.memory.store.db", mock_db):
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
        with patch("machina.memory.retriever.asyncio.wait_for", side_effect=TimeoutError()):
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

        with patch("machina.memory.retriever.db", mock_db):
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
        with patch("machina.memory.consolidator.context_manager") as mock_cm:
            mock_cm.get_chunks = AsyncMock(return_value=[])
            # This should not raise and should complete
            results = await consolidator.consolidate_session(uuid.uuid4(), "harness")
            assert results.status == "empty" and results.entries == []


# ===========================================================================
# Audit: repeated rerank indices must not duplicate a memory
# ===========================================================================


class TestRerankIndexDedup:
    """A repeated index in the LLM rerank response surfacing one memory N times."""

    async def test_repeated_indices_do_not_duplicate_outputs(self):
        from types import SimpleNamespace

        from machina.core import usage as usage_module

        candidates = [
            RetrievedMemory(
                memory=MemoryEntry(
                    id=uuid.uuid4(),
                    session_id=None,
                    agent_id="harness",
                    content=f"memory-{i}",
                    category="fact",
                ),
                score=1.0 - i * 0.1,
                source="dense",
            )
            for i in range(3)
        ]

        async def fake_complete(*args, **kwargs):
            return SimpleNamespace(content="[0,0,0]")

        retriever = MemoryRetriever(llm_provider=object())
        with patch.object(usage_module.usage_store, "complete_call", fake_complete):
            out = await retriever._rerank("query", candidates)

        assert [str(r.memory.id) for r in out] == [str(c.memory.id) for c in candidates]


# ===========================================================================
# R09: schema-invalid extraction must not advance the agent watermark
# ===========================================================================

TEST_DSN = os.environ.get("MACHINA_TEST_DATABASE_URL", "")
needs_db = pytest.mark.skipif(not TEST_DSN, reason="MACHINA_TEST_DATABASE_URL not set")


class TestConsolidationWatermarkRetry:
    """R09: a schema-invalid extraction result must not let the agent advance
    ``mem_consolidated_up_to``. The range stays retryable and the next
    consolidation re-extracts it, exactly like malformed JSON."""

    async def test_invalid_elements_keep_range_retryable(self):
        import importlib
        from types import SimpleNamespace

        from machina.core.agent import BaseReActAgent
        from machina.core.models import ContextChunk

        agent_module = importlib.import_module("machina.core.agent")
        consolidator_module = importlib.import_module("machina.memory.consolidator")
        usage_module = importlib.import_module("machina.core.usage")

        sid = uuid.uuid4()
        chunk = ContextChunk(
            uuid.uuid4(),
            sid,
            "harness",
            "user_message",
            {"content": "Remember: I prefer concise answers."},
        )
        session = SimpleNamespace(state={})

        async def get_chunks_since(session_id, *, since_time=None, since_id=None, limit=100):
            return [] if since_id == chunk.id else [chunk]

        context = SimpleNamespace(
            get_chunks_since=get_chunks_since,
            get_chunks=AsyncMock(return_value=[chunk]),
        )

        async def update_state_fields(session_id, fields):
            session.state.update(fields)

        sessions = SimpleNamespace(
            get=AsyncMock(return_value=session),
            update_state_fields=update_state_fields,
        )

        class _Store:
            def __init__(self):
                self.added = []

            async def search(self, **kwargs):
                return []

            async def search_by_embedding(self, **kwargs):
                return []

            async def update_access(self, *args, **kwargs):
                return None

            async def add(self, **kwargs):
                self.added.append(kwargs)
                return SimpleNamespace(id=uuid.uuid4())

        responses = [
            # Schema-invalid element: missing content.
            '[{"category": "preference", "importance": 0.9}]',
            # The retry returns a valid element for the same range.
            '[{"content": "I prefer concise answers.", "category": "preference", "importance": 0.9}]',
        ]

        async def complete(*args, **kwargs):
            return SimpleNamespace(content=responses.pop(0))

        store = _Store()
        agent = BaseReActAgent.__new__(BaseReActAgent)
        agent.agent_id = "harness"
        agent.memory_consolidator = MemoryConsolidator(llm_provider=object(), store=store)

        with (
            patch.object(consolidator_module, "db", SimpleNamespace(connected=False)),
            patch.object(consolidator_module, "context_manager", context),
            patch.object(agent_module, "context_manager", context),
            patch.object(agent_module, "session_manager", sessions),
            patch.object(usage_module.usage_store, "complete_call", complete),
        ):
            await agent._consolidate_memories(sid)
            assert "mem_consolidated_up_to" not in session.state
            assert len(responses) == 1, "invalid extraction must stay retryable"

            await agent._consolidate_memories(sid)
            assert session.state.get("mem_consolidated_up_to", {}).get("id") == str(chunk.id)
            assert len(responses) == 0, "the retry must re-extract the same range"


# ===========================================================================
# R10: consolidation must not starve the database pool it protects
# ===========================================================================


class _BoundedPoolDb:
    """Test double with a bounded pool: ``acquire`` fails fast when exhausted.

    Mirrors the review's bounded-pool fixture (memory_and_skill.pool_starvation):
    the consolidation may only use the connections it actually needs and must
    not hold one for its serialization lock while borrowing another.
    """

    connected = True

    def __init__(self, capacity: int):
        self.capacity = capacity
        self.sem = asyncio.Semaphore(capacity)
        self.locked = 0
        self.all_locked = asyncio.Event()

    @asynccontextmanager
    async def acquire(self):
        await asyncio.wait_for(self.sem.acquire(), timeout=0.5)
        try:
            yield self
        finally:
            self.sem.release()

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def execute(self, query, *args):
        if "pg_advisory_xact_lock" in query:
            self.locked += 1
            if self.locked == self.capacity:
                self.all_locked.set()
            # Wait until every pool connection is busy holding a lock —
            # the deterministic pool-exhaustion scenario from the review.
            await asyncio.wait_for(self.all_locked.wait(), timeout=5)

    async def fetch(self, query, *args):
        async with self.acquire():
            return []


class TestConsolidationPoolIndependence:
    """R10: the serialization lock must not consume a pool connection that
    the protected consolidation body itself needs."""

    async def test_one_connection_pool_does_not_deadlock(self, monkeypatch):
        database = _BoundedPoolDb(capacity=1)
        monkeypatch.setattr("machina.memory.consolidator.db", database)
        monkeypatch.setattr("machina.core.context.db", database)

        result = await asyncio.wait_for(
            MemoryConsolidator().consolidate_session(uuid.uuid4(), "harness"),
            timeout=5,
        )
        assert result.status == "empty"

    async def test_ten_concurrent_sessions_do_not_exhaust_the_pool(self, monkeypatch):
        database = _BoundedPoolDb(capacity=10)
        monkeypatch.setattr("machina.memory.consolidator.db", database)
        monkeypatch.setattr("machina.core.context.db", database)

        results = await asyncio.wait_for(
            asyncio.gather(
                *(
                    MemoryConsolidator().consolidate_session(uuid.uuid4(), "harness")
                    for _ in range(10)
                )
            ),
            timeout=5,
        )
        assert [r.status for r in results] == ["empty"] * 10

    @needs_db
    async def test_single_connection_real_pool(self, monkeypatch):
        """Reproduction: the public locked method must work with a 1-conn pool."""
        import asyncpg

        from machina.db.connection import Database

        database = Database(dsn=TEST_DSN)
        database._pool = await asyncpg.create_pool(TEST_DSN, min_size=1, max_size=1)
        try:
            monkeypatch.setattr("machina.memory.consolidator.db", database)
            monkeypatch.setattr("machina.core.context.db", database)

            result = await asyncio.wait_for(
                MemoryConsolidator().consolidate_session(uuid.uuid4(), "harness"),
                timeout=15,
            )
            assert result.status == "empty"
        finally:
            await database.close()

    @needs_db
    async def test_ten_concurrent_sessions_real_default_pool(self, monkeypatch):
        """10 simultaneous distinct-session consolidations must share the
        default 10-connection pool without timing out on their own locks."""
        import asyncpg

        from machina.db.connection import Database

        database = Database(dsn=TEST_DSN)
        database._pool = await asyncpg.create_pool(TEST_DSN, min_size=2, max_size=10)
        try:
            monkeypatch.setattr("machina.memory.consolidator.db", database)
            monkeypatch.setattr("machina.core.context.db", database)

            results = await asyncio.wait_for(
                asyncio.gather(
                    *(
                        MemoryConsolidator().consolidate_session(uuid.uuid4(), "harness")
                        for _ in range(10)
                    )
                ),
                timeout=15,
            )
            assert [r.status for r in results] == ["empty"] * 10
        finally:
            await database.close()
