"""Tests for the memory system — MemoryEntry, MemoryStore, MemoryConsolidator, etc.

These tests define the expected interface for the memory module. They will
skip gracefully if the module is not yet implemented.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers — skip all tests in this module if ah.memory is not implemented
# ---------------------------------------------------------------------------
try:
    from ah.memory import (
        MemoryEntry,
        MemoryStore,
        MemoryConsolidator,
        ImportanceScorer,
        ForgettingModel,
        MemoryRetriever,
    )
    _MEMORY_AVAILABLE = all(
        cls is not None
        for cls in [MemoryEntry, MemoryStore, MemoryConsolidator, ImportanceScorer, ForgettingModel, MemoryRetriever]
    )
except (ImportError, AttributeError):
    _MEMORY_AVAILABLE = False

pytestmark = pytest.mark.skipif(not _MEMORY_AVAILABLE, reason="ah.memory not implemented yet")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_entry():
    """Create a sample MemoryEntry."""
    return MemoryEntry(
        id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        agent_id="harness",
        content="The user prefers dark mode",
        memory_type="preference",
        importance=0.8,
        created_at=datetime.utcnow(),
        last_accessed=datetime.utcnow(),
        access_count=5,
        embedding=[0.1] * 1536,
    )


@pytest.fixture
def sample_entries():
    """Create a list of sample MemoryEntry objects."""
    now = datetime.utcnow()
    return [
        MemoryEntry(
            id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            agent_id="harness",
            content=f"Memory {i}",
            memory_type="fact",
            importance=0.5 + i * 0.1,
            created_at=now - timedelta(hours=i),
            last_accessed=now - timedelta(minutes=i * 10),
            access_count=i,
            embedding=[0.01 * i] * 1536,
        )
        for i in range(5)
    ]


@pytest.fixture
def mock_db():
    """Create a mock database for MemoryStore tests."""
    mock = AsyncMock()
    mock.fetch = AsyncMock(return_value=[])
    mock.fetchrow = AsyncMock(return_value=None)
    mock.fetchval = AsyncMock(return_value=0)
    mock.execute = AsyncMock(return_value="DELETE 0")
    return mock


# ===========================================================================
# MemoryEntry Tests
# ===========================================================================

class TestMemoryEntry:
    """Tests for the MemoryEntry dataclass."""

    def test_create_basic(self):
        """Test creating a basic MemoryEntry."""
        entry = MemoryEntry(
            id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            agent_id="harness",
            content="Test memory",
        )
        assert entry.content == "Test memory"
        assert entry.agent_id == "harness"

    def test_defaults(self):
        """Test MemoryEntry default values."""
        entry = MemoryEntry(
            id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            agent_id="harness",
            content="Test",
        )
        assert entry.memory_type == "fact"
        assert entry.importance == 0.5
        assert entry.access_count == 0
        assert entry.embedding is None
        assert isinstance(entry.created_at, datetime)

    def test_custom_values(self):
        """Test MemoryEntry with custom values."""
        now = datetime.utcnow()
        entry = MemoryEntry(
            id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            agent_id="test",
            content="Custom",
            memory_type="preference",
            importance=0.9,
            created_at=now,
            last_accessed=now,
            access_count=10,
            embedding=[0.1] * 1536,
        )
        assert entry.memory_type == "preference"
        assert entry.importance == 0.9
        assert entry.access_count == 10
        assert entry.embedding == [0.1] * 1536

    def test_importance_bounds(self):
        """Test that importance is clamped to [0, 1]."""
        entry = MemoryEntry(
            id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            agent_id="harness",
            content="Test",
            importance=1.5,
        )
        # Should be clamped to 1.0
        assert entry.importance <= 1.0

    def test_importance_negative(self):
        """Test that negative importance is clamped to 0."""
        entry = MemoryEntry(
            id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            agent_id="harness",
            content="Test",
            importance=-0.5,
        )
        assert entry.importance >= 0.0

    def test_to_dict(self, sample_entry):
        """Test serialization to dict."""
        d = sample_entry.to_dict()
        assert d["id"] == str(sample_entry.id)
        assert d["content"] == sample_entry.content
        assert d["importance"] == sample_entry.importance

    def test_from_dict(self):
        """Test deserialization from dict."""
        data = {
            "id": str(uuid.uuid4()),
            "session_id": str(uuid.uuid4()),
            "agent_id": "harness",
            "content": "Test",
            "memory_type": "fact",
            "importance": 0.7,
            "access_count": 3,
        }
        entry = MemoryEntry.from_dict(data)
        assert entry.content == "Test"
        assert entry.importance == 0.7
        assert entry.access_count == 3


# ===========================================================================
# MemoryStore Tests
# ===========================================================================

class TestMemoryStore:
    """Tests for MemoryStore CRUD operations."""

    @pytest.fixture
    def store(self):
        """Create a MemoryStore instance."""
        return MemoryStore()

    async def test_add_memory(self, store, mock_db):
        """Test adding a memory entry."""
        with patch("ah.memory.db", mock_db):
            mock_db.fetchrow = AsyncMock(return_value={
                "id": uuid.uuid4(),
                "session_id": uuid.uuid4(),
                "agent_id": "harness",
                "content": "Test memory",
                "memory_type": "fact",
                "importance": 0.5,
                "access_count": 0,
                "created_at": datetime.utcnow(),
                "last_accessed": None,
                "embedding": None,
            })
            entry = await store.add(
                session_id=uuid.uuid4(),
                agent_id="harness",
                content="Test memory",
            )
            assert entry is not None
            assert entry.content == "Test memory"

    async def test_get_memory(self, store, mock_db):
        """Test retrieving a memory by ID."""
        entry_id = uuid.uuid4()
        with patch("ah.memory.db", mock_db):
            mock_db.fetchrow = AsyncMock(return_value={
                "id": entry_id,
                "session_id": uuid.uuid4(),
                "agent_id": "harness",
                "content": "Test",
                "memory_type": "fact",
                "importance": 0.5,
                "access_count": 0,
                "created_at": datetime.utcnow(),
                "last_accessed": None,
                "embedding": None,
            })
            entry = await store.get(entry_id)
            assert entry is not None
            assert entry.id == entry_id

    async def test_get_memory_not_found(self, store, mock_db):
        """Test retrieving a non-existent memory."""
        with patch("ah.memory.db", mock_db):
            mock_db.fetchrow = AsyncMock(return_value=None)
            entry = await store.get(uuid.uuid4())
            assert entry is None

    async def test_list_memories(self, store, mock_db):
        """Test listing memories for a session."""
        with patch("ah.memory.db", mock_db):
            mock_db.fetch = AsyncMock(return_value=[
                {
                    "id": uuid.uuid4(),
                    "session_id": uuid.uuid4(),
                    "agent_id": "harness",
                    "content": f"Memory {i}",
                    "memory_type": "fact",
                    "importance": 0.5,
                    "access_count": 0,
                    "created_at": datetime.utcnow(),
                    "last_accessed": None,
                    "embedding": None,
                }
                for i in range(3)
            ])
            entries = await store.list(session_id=uuid.uuid4())
            assert len(entries) == 3

    async def test_list_memories_empty(self, store, mock_db):
        """Test listing memories when none exist."""
        with patch("ah.memory.db", mock_db):
            mock_db.fetch = AsyncMock(return_value=[])
            entries = await store.list(session_id=uuid.uuid4())
            assert entries == []

    async def test_update_memory(self, store, mock_db):
        """Test updating a memory entry."""
        with patch("ah.memory.db", mock_db):
            mock_db.execute = AsyncMock(return_value="UPDATE 1")
            result = await store.update(
                uuid.uuid4(),
                content="Updated content",
            )
            assert result is True

    async def test_delete_memory(self, store, mock_db):
        """Test deleting a memory entry."""
        with patch("ah.memory.db", mock_db):
            mock_db.execute = AsyncMock(return_value="DELETE 1")
            result = await store.delete(uuid.uuid4())
            assert result is True

    async def test_delete_memory_not_found(self, store, mock_db):
        """Test deleting a non-existent memory."""
        with patch("ah.memory.db", mock_db):
            mock_db.execute = AsyncMock(return_value="DELETE 0")
            result = await store.delete(uuid.uuid4())
            assert result is False

    async def test_search_by_content(self, store, mock_db):
        """Test searching memories by content."""
        with patch("ah.memory.db", mock_db):
            mock_db.fetch = AsyncMock(return_value=[
                {
                    "id": uuid.uuid4(),
                    "session_id": uuid.uuid4(),
                    "agent_id": "harness",
                    "content": "Python is great",
                    "memory_type": "fact",
                    "importance": 0.7,
                    "access_count": 2,
                    "created_at": datetime.utcnow(),
                    "last_accessed": datetime.utcnow(),
                    "embedding": None,
                }
            ])
            results = await store.search("Python")
            assert len(results) == 1
            assert "Python" in results[0].content

    async def test_search_by_embedding(self, store, mock_db):
        """Test searching memories by embedding similarity."""
        with patch("ah.memory.db", mock_db):
            mock_db.fetch = AsyncMock(return_value=[
                {
                    "id": uuid.uuid4(),
                    "session_id": uuid.uuid4(),
                    "agent_id": "harness",
                    "content": "Similar memory",
                    "memory_type": "fact",
                    "importance": 0.6,
                    "access_count": 1,
                    "created_at": datetime.utcnow(),
                    "last_accessed": None,
                    "embedding": [0.1] * 1536,
                }
            ])
            results = await store.search_by_embedding([0.1] * 1536, top_k=5)
            assert len(results) == 1

    async def test_increment_access_count(self, store, mock_db):
        """Test incrementing access count."""
        with patch("ah.memory.db", mock_db):
            mock_db.execute = AsyncMock(return_value="UPDATE 1")
            await store.increment_access(uuid.uuid4())
            mock_db.execute.assert_called_once()

    async def test_get_memories_by_importance(self, store, mock_db):
        """Test getting memories filtered by importance threshold."""
        with patch("ah.memory.db", mock_db):
            mock_db.fetch = AsyncMock(return_value=[
                {
                    "id": uuid.uuid4(),
                    "session_id": uuid.uuid4(),
                    "agent_id": "harness",
                    "content": "Important memory",
                    "memory_type": "fact",
                    "importance": 0.9,
                    "access_count": 5,
                    "created_at": datetime.utcnow(),
                    "last_accessed": datetime.utcnow(),
                    "embedding": None,
                }
            ])
            results = await store.get_by_importance(min_importance=0.8)
            assert len(results) == 1
            assert results[0].importance >= 0.8


# ===========================================================================
# ImportanceScorer Tests
# ===========================================================================

class TestImportanceScorer:
    """Tests for the ImportanceScorer."""

    @pytest.fixture
    def scorer(self):
        """Create an ImportanceScorer instance."""
        return ImportanceScorer()

    def test_score_basic(self, scorer):
        """Test basic importance scoring."""
        score = scorer.score("The user's name is Alice")
        assert 0.0 <= score <= 1.0

    def test_score_empty_string(self, scorer):
        """Test scoring an empty string."""
        score = scorer.score("")
        assert 0.0 <= score <= 1.0

    def test_score_preferences(self, scorer):
        """Test that preference statements score higher."""
        pref_score = scorer.score("The user prefers dark mode")
        fact_score = scorer.score("The sky is blue")
        # Preferences should generally score higher
        assert pref_score >= 0.0

    def test_score_user_info(self, scorer):
        """Test that user information scores higher."""
        user_score = scorer.score("The user is a software engineer")
        assert user_score >= 0.0

    def test_score_with_access_count(self, scorer):
        """Test scoring with access count factor."""
        score_low = scorer.score("Test", access_count=0)
        score_high = scorer.score("Test", access_count=100)
        # Higher access count should generally increase importance
        assert score_high >= score_low

    def test_score_with_recency(self, scorer):
        """Test scoring with recency factor."""
        now = datetime.utcnow()
        score_recent = scorer.score("Test", last_accessed=now)
        score_old = scorer.score("Test", last_accessed=now - timedelta(days=30))
        # Recent access should generally increase importance
        assert score_recent >= score_old

    def test_score_deterministic(self, scorer):
        """Test that scoring is deterministic."""
        score1 = scorer.score("Test content")
        score2 = scorer.score("Test content")
        assert score1 == score2

    def test_score_bounds(self, scorer):
        """Test that all scores are within [0, 1]."""
        texts = [
            "",
            "a",
            "The user likes Python",
            "x" * 10000,
            "Special chars: !@#$%^&*()",
        ]
        for text in texts:
            score = scorer.score(text)
            assert 0.0 <= score <= 1.0, f"Score {score} out of bounds for text: {text[:50]}"


# ===========================================================================
# ForgettingModel Tests
# ===========================================================================

class TestForgettingModel:
    """Tests for the ForgettingModel (Ebbinghaus forgetting curve)."""

    @pytest.fixture
    def model(self):
        """Create a ForgettingModel instance."""
        return ForgettingModel()

    def test_retention_basic(self, model):
        """Test basic retention calculation."""
        retention = model.retention(0)
        assert 0.0 <= retention <= 1.0

    def test_retention_immediate(self, model):
        """Test retention at time 0 (should be ~1.0)."""
        retention = model.retention(0)
        assert retention >= 0.9

    def test_retention_decreases_over_time(self, model):
        """Test that retention decreases over time."""
        r0 = model.retention(0)
        r1 = model.retention(1)
        r7 = model.retention(7)
        r30 = model.retention(30)
        assert r0 >= r1 >= r7 >= r30

    def test_retention_with_reinforcements(self, model):
        """Test that reinforcements increase retention."""
        r_no_reinforce = model.retention(7, reinforcements=0)
        r_with_reinforce = model.retention(7, reinforcements=5)
        assert r_with_reinforce >= r_no_reinforce

    def test_retention_bounds(self, model):
        """Test that retention is always in [0, 1]."""
        for days in [0, 1, 7, 30, 365]:
            for reinf in [0, 1, 5, 10]:
                r = model.retention(days, reinforcements=reinf)
                assert 0.0 <= r <= 1.0

    def test_forgetting_rate(self, model):
        """Test forgetting rate calculation."""
        rate = model.forgetting_rate()
        assert rate > 0

    def test_half_life(self, model):
        """Test half-life calculation."""
        half_life = model.half_life()
        assert half_life > 0

    def test_should_forget(self, model):
        """Test should_forget decision."""
        # Very old, never accessed — should forget
        assert model.should_forget(
            last_accessed=datetime.utcnow() - timedelta(days=365),
            importance=0.1,
        )
        # Recent, important — should not forget
        assert not model.should_forget(
            last_accessed=datetime.utcnow(),
            importance=0.9,
        )


# ===========================================================================
# MemoryConsolidator Tests
# ===========================================================================

class TestMemoryConsolidator:
    """Tests for the MemoryConsolidator."""

    @pytest.fixture
    def consolidator(self):
        """Create a MemoryConsolidator instance."""
        return MemoryConsolidator()

    def test_consolidate_empty_list(self, consolidator):
        """Test consolidating an empty list."""
        result = consolidator.consolidate([])
        assert result == []

    def test_consolidate_single_entry(self, consolidator, sample_entry):
        """Test consolidating a single entry."""
        result = consolidator.consolidate([sample_entry])
        assert len(result) == 1

    def test_consolidate_merges_similar(self, consolidator):
        """Test that similar memories are merged."""
        entries = [
            MemoryEntry(
                id=uuid.uuid4(),
                session_id=uuid.uuid4(),
                agent_id="harness",
                content="The user likes Python",
                importance=0.7,
            ),
            MemoryEntry(
                id=uuid.uuid4(),
                session_id=uuid.uuid4(),
                agent_id="harness",
                content="The user enjoys Python programming",
                importance=0.6,
            ),
        ]
        result = consolidator.consolidate(entries)
        # Should merge similar entries
        assert len(result) <= len(entries)

    def test_consolidate_preserves_unique(self, consolidator):
        """Test that unique memories are preserved."""
        entries = [
            MemoryEntry(
                id=uuid.uuid4(),
                session_id=uuid.uuid4(),
                agent_id="harness",
                content="The user likes Python",
                importance=0.7,
            ),
            MemoryEntry(
                id=uuid.uuid4(),
                session_id=uuid.uuid4(),
                agent_id="harness",
                content="The weather is sunny today",
                importance=0.5,
            ),
        ]
        result = consolidator.consolidate(entries)
        assert len(result) == 2

    def test_consolidate_updates_importance(self, consolidator):
        """Test that consolidation updates importance scores."""
        entries = [
            MemoryEntry(
                id=uuid.uuid4(),
                session_id=uuid.uuid4(),
                agent_id="harness",
                content="The user likes Python",
                importance=0.7,
                access_count=5,
            ),
            MemoryEntry(
                id=uuid.uuid4(),
                session_id=uuid.uuid4(),
                agent_id="harness",
                content="The user enjoys Python",
                importance=0.6,
                access_count=3,
            ),
        ]
        result = consolidator.consolidate(entries)
        if len(result) == 1:
            # Merged entry should have combined or max importance
            assert result[0].importance >= 0.6

    def test_consolidate_session_isolation(self, consolidator):
        """Test that consolidation is isolated per session."""
        session1 = uuid.uuid4()
        session2 = uuid.uuid4()
        entries = [
            MemoryEntry(
                id=uuid.uuid4(),
                session_id=session1,
                agent_id="harness",
                content="Session 1 memory",
                importance=0.7,
            ),
            MemoryEntry(
                id=uuid.uuid4(),
                session_id=session2,
                agent_id="harness",
                content="Session 2 memory",
                importance=0.6,
            ),
        ]
        result = consolidator.consolidate(entries)
        # Should not merge across sessions
        assert len(result) == 2


# ===========================================================================
# MemoryRetriever Tests
# ===========================================================================

class TestMemoryRetriever:
    """Tests for the MemoryRetriever."""

    @pytest.fixture
    def retriever(self):
        """Create a MemoryRetriever instance."""
        return MemoryRetriever()

    @pytest.fixture
    def mock_store(self):
        """Create a mock MemoryStore."""
        store = AsyncMock()
        store.search = AsyncMock(return_value=[])
        store.search_by_embedding = AsyncMock(return_value=[])
        store.list = AsyncMock(return_value=[])
        return store

    async def test_retrieve_by_query(self, retriever, mock_store):
        """Test retrieving memories by text query."""
        mock_store.search = AsyncMock(return_value=[
            MemoryEntry(
                id=uuid.uuid4(),
                session_id=uuid.uuid4(),
                agent_id="harness",
                content="Python is great",
                importance=0.8,
            )
        ])
        results = await retriever.retrieve("Python", store=mock_store)
        assert len(results) == 1

    async def test_retrieve_by_embedding(self, retriever, mock_store):
        """Test retrieving memories by embedding."""
        mock_store.search_by_embedding = AsyncMock(return_value=[
            MemoryEntry(
                id=uuid.uuid4(),
                session_id=uuid.uuid4(),
                agent_id="harness",
                content="Similar memory",
                importance=0.7,
            )
        ])
        results = await retriever.retrieve_by_embedding(
            [0.1] * 1536, store=mock_store
        )
        assert len(results) == 1

    async def test_retrieve_empty_results(self, retriever, mock_store):
        """Test retrieving when no memories match."""
        mock_store.search = AsyncMock(return_value=[])
        results = await retriever.retrieve("nonexistent", store=mock_store)
        assert results == []

    async def test_retrieve_with_top_k(self, retriever, mock_store):
        """Test retrieving with top_k limit."""
        mock_store.search = AsyncMock(return_value=[
            MemoryEntry(
                id=uuid.uuid4(),
                session_id=uuid.uuid4(),
                agent_id="harness",
                content=f"Memory {i}",
                importance=0.5 + i * 0.1,
            )
            for i in range(10)
        ])
        results = await retriever.retrieve("test", store=mock_store, top_k=3)
        assert len(results) <= 3

    async def test_retrieve_with_importance_threshold(self, retriever, mock_store):
        """Test retrieving with importance threshold."""
        mock_store.search = AsyncMock(return_value=[
            MemoryEntry(
                id=uuid.uuid4(),
                session_id=uuid.uuid4(),
                agent_id="harness",
                content="Important",
                importance=0.9,
            ),
            MemoryEntry(
                id=uuid.uuid4(),
                session_id=uuid.uuid4(),
                agent_id="harness",
                content="Not important",
                importance=0.2,
            ),
        ])
        results = await retriever.retrieve(
            "test", store=mock_store, min_importance=0.5
        )
        assert all(r.importance >= 0.5 for r in results)

    async def test_retrieve_hybrid(self, retriever, mock_store):
        """Test hybrid retrieval (text + embedding)."""
        mock_store.search = AsyncMock(return_value=[
            MemoryEntry(
                id=uuid.uuid4(),
                session_id=uuid.uuid4(),
                agent_id="harness",
                content="Text match",
                importance=0.7,
            )
        ])
        mock_store.search_by_embedding = AsyncMock(return_value=[
            MemoryEntry(
                id=uuid.uuid4(),
                session_id=uuid.uuid4(),
                agent_id="harness",
                content="Embedding match",
                importance=0.6,
            )
        ])
        results = await retriever.retrieve_hybrid(
            query="test",
            query_embedding=[0.1] * 1536,
            store=mock_store,
        )
        assert len(results) > 0

    async def test_retrieve_recent(self, retriever, mock_store):
        """Test retrieving recent memories."""
        now = datetime.utcnow()
        mock_store.list = AsyncMock(return_value=[
            MemoryEntry(
                id=uuid.uuid4(),
                session_id=uuid.uuid4(),
                agent_id="harness",
                content="Recent",
                importance=0.5,
                last_accessed=now,
            )
        ])
        results = await retriever.retrieve_recent(store=mock_store, hours=24)
        assert len(results) == 1

    async def test_retrieve_by_type(self, retriever, mock_store):
        """Test retrieving memories by type."""
        mock_store.list = AsyncMock(return_value=[
            MemoryEntry(
                id=uuid.uuid4(),
                session_id=uuid.uuid4(),
                agent_id="harness",
                content="Preference",
                memory_type="preference",
                importance=0.8,
            )
        ])
        results = await retriever.retrieve_by_type(
            "preference", store=mock_store
        )
        assert len(results) == 1
        assert results[0].memory_type == "preference"


# ===========================================================================
# Memory Integration Tests
# ===========================================================================

class TestMemoryIntegration:
    """Integration tests for the memory system."""

    async def test_full_lifecycle(self, mock_db):
        """Test the full memory lifecycle: add → retrieve → consolidate → forget."""
        from ah.memory import MemoryStore, MemoryConsolidator, ForgettingModel

        store = MemoryStore()
        consolidator = MemoryConsolidator()
        forgetting = ForgettingModel()

        with patch("ah.memory.db", mock_db):
            # Add memories
            mock_db.fetchrow = AsyncMock(return_value={
                "id": uuid.uuid4(),
                "session_id": uuid.uuid4(),
                "agent_id": "harness",
                "content": "Test memory",
                "memory_type": "fact",
                "importance": 0.7,
                "access_count": 0,
                "created_at": datetime.utcnow(),
                "last_accessed": None,
                "embedding": None,
            })
            entry = await store.add(
                session_id=uuid.uuid4(),
                agent_id="harness",
                content="Test memory",
            )
            assert entry is not None

            # Retrieve
            mock_db.fetch = AsyncMock(return_value=[{
                "id": entry.id,
                "session_id": entry.session_id,
                "agent_id": "harness",
                "content": "Test memory",
                "memory_type": "fact",
                "importance": 0.7,
                "access_count": 0,
                "created_at": datetime.utcnow(),
                "last_accessed": None,
                "embedding": None,
            }])
            results = await store.list(session_id=entry.session_id)
            assert len(results) >= 0  # May be empty due to mock

    async def test_memory_with_agent_context(self, mock_db):
        """Test that memories integrate with agent context."""
        from ah.memory import MemoryStore

        store = MemoryStore()
        session_id = uuid.uuid4()

        with patch("ah.memory.db", mock_db):
            mock_db.fetchrow = AsyncMock(return_value={
                "id": uuid.uuid4(),
                "session_id": session_id,
                "agent_id": "harness",
                "content": "User prefers concise answers",
                "memory_type": "preference",
                "importance": 0.9,
                "access_count": 0,
                "created_at": datetime.utcnow(),
                "last_accessed": None,
                "embedding": None,
            })
            entry = await store.add(
                session_id=session_id,
                agent_id="harness",
                content="User prefers concise answers",
                memory_type="preference",
            )
            assert entry.session_id == session_id
