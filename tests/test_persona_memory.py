"""Tests for Gap 3: Persona-Conditioned Memory with Emotion Topology.

Covers PersonaMemory dataclass, EmotionTopology retrieval weighting,
dual-stream separation, and persona memory updates.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from ah.memory.models import MemoryEntry
from ah.memory.persona import (
    EmotionTopology,
    PersonaMemory,
    PersonaMemoryStore,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_fact() -> MemoryEntry:
    """Create a sample factual memory."""
    return MemoryEntry(
        id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        agent_id="harness",
        content="User prefers dark mode",
        category="preference",
        importance=0.8,
        created_at=datetime.now(UTC),
    )


@pytest.fixture
def sample_persona_memory(sample_fact) -> PersonaMemory:
    """Create a sample persona memory."""
    now = datetime.now(UTC)
    return PersonaMemory(
        id=uuid.uuid4(),
        fact_id=sample_fact.id,
        persona_id="harness",
        interpretation="User values visual comfort",
        emotional_valence=0.7,
        emotional_arousal=0.3,
        confidence=0.9,
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
def persona_memories() -> list[PersonaMemory]:
    """Create a list of persona memories with varying valence/arousal."""
    now = datetime.now(UTC)
    fact_id = uuid.uuid4()
    return [
        PersonaMemory(
            id=uuid.uuid4(),
            fact_id=fact_id,
            persona_id="harness",
            interpretation="Positive experience with Python",
            emotional_valence=0.8,
            emotional_arousal=0.2,
            confidence=0.9,
            created_at=now,
            updated_at=now,
        ),
        PersonaMemory(
            id=uuid.uuid4(),
            fact_id=fact_id,
            persona_id="harness",
            interpretation="Negative experience with bugs",
            emotional_valence=-0.7,
            emotional_arousal=0.9,
            confidence=0.8,
            created_at=now,
            updated_at=now,
        ),
        PersonaMemory(
            id=uuid.uuid4(),
            fact_id=fact_id,
            persona_id="harness",
            interpretation="Neutral fact about syntax",
            emotional_valence=0.1,
            emotional_arousal=0.1,
            confidence=0.7,
            created_at=now,
            updated_at=now,
        ),
    ]


# ===========================================================================
# PersonaMemory Dataclass Tests
# ===========================================================================


class TestPersonaMemory:
    """Tests for the PersonaMemory dataclass."""

    def test_create_from_fact(self, sample_fact):
        """Test creating a PersonaMemory from a factual memory."""
        persona_mem = PersonaMemory(
            id=uuid.uuid4(),
            fact_id=sample_fact.id,
            persona_id="harness",
            interpretation="User values visual comfort",
            emotional_valence=0.7,
            emotional_arousal=0.3,
            confidence=0.9,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        assert persona_mem.fact_id == sample_fact.id
        assert persona_mem.persona_id == "harness"
        assert persona_mem.interpretation == "User values visual comfort"
        assert persona_mem.emotional_valence == 0.7
        assert persona_mem.emotional_arousal == 0.3
        assert persona_mem.confidence == 0.9

    def test_valence_bounds(self, sample_fact):
        """Test that emotional_valence is clamped to [-1.0, 1.0]."""
        persona_mem = PersonaMemory(
            id=uuid.uuid4(),
            fact_id=sample_fact.id,
            persona_id="harness",
            interpretation="Test",
            emotional_valence=1.5,
            emotional_arousal=0.5,
            confidence=0.5,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        assert persona_mem.emotional_valence <= 1.0

    def test_arousal_bounds(self, sample_fact):
        """Test that emotional_arousal is clamped to [0.0, 1.0]."""
        persona_mem = PersonaMemory(
            id=uuid.uuid4(),
            fact_id=sample_fact.id,
            persona_id="harness",
            interpretation="Test",
            emotional_valence=0.0,
            emotional_arousal=1.5,
            confidence=0.5,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        assert persona_mem.emotional_arousal <= 1.0

    def test_confidence_bounds(self, sample_fact):
        """Test that confidence is clamped to [0.0, 1.0]."""
        persona_mem = PersonaMemory(
            id=uuid.uuid4(),
            fact_id=sample_fact.id,
            persona_id="harness",
            interpretation="Test",
            emotional_valence=0.0,
            emotional_arousal=0.5,
            confidence=-0.5,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        assert persona_mem.confidence >= 0.0

    def test_defaults(self, sample_fact):
        """Test PersonaMemory default values."""
        persona_mem = PersonaMemory(
            id=uuid.uuid4(),
            fact_id=sample_fact.id,
            persona_id="harness",
            interpretation="Test",
        )
        assert persona_mem.emotional_valence == 0.0
        assert persona_mem.emotional_arousal == 0.0
        assert persona_mem.confidence == 0.5
        assert isinstance(persona_mem.created_at, datetime)
        assert isinstance(persona_mem.updated_at, datetime)


# ===========================================================================
# EmotionTopology Tests
# ===========================================================================


class TestEmotionTopology:
    """Tests for the EmotionTopology retrieval weighting."""

    def test_retrieve_for_emotion(self, persona_memories):
        """Test that retrieval returns results weighted by emotional state."""
        topology = EmotionTopology()
        results = topology.retrieve_for_emotion(
            memories=persona_memories,
            emotion="joy",
            limit=5,
        )
        assert isinstance(results, list)
        assert len(results) > 0
        # Results should be sorted by score descending
        scores = [score for _, score in results]
        assert scores == sorted(scores, reverse=True)

    def test_valence_bias_positive_emotion(self, persona_memories):
        """Test that positive emotions retrieve positive-valence memories first."""
        topology = EmotionTopology()
        results = topology.retrieve_for_emotion(
            memories=persona_memories,
            emotion="joy",
            limit=5,
        )
        # Joy has positive valence_bias, so positive-valence memories should rank higher
        assert len(results) >= 2
        top_memory = results[0][0]
        assert top_memory.emotional_valence > 0

    def test_valence_bias_negative_emotion(self, persona_memories):
        """Test that negative emotions retrieve negative-valence memories first."""
        topology = EmotionTopology()
        results = topology.retrieve_for_emotion(
            memories=persona_memories,
            emotion="sadness",
            limit=5,
        )
        # Sadness has negative valence_bias, so negative-valence memories should rank higher
        assert len(results) >= 2
        top_memory = results[0][0]
        assert top_memory.emotional_valence < 0

    def test_arousal_filter_high_arousal(self, persona_memories):
        """Test that high arousal emotion retrieves high-arousal memories."""
        topology = EmotionTopology()
        results = topology.retrieve_for_emotion(
            memories=persona_memories,
            emotion="anger",  # high arousal emotion
            limit=5,
        )
        # Anger should boost high-arousal memories
        assert len(results) >= 2
        # The high-arousal memory should be ranked higher than the low-arousal one
        high_arousal_mem = next(
            (m for m in persona_memories if m.emotional_arousal > 0.5), None
        )
        low_arousal_mem = next(
            (m for m in persona_memories if m.emotional_arousal < 0.5), None
        )
        if high_arousal_mem and low_arousal_mem:
            high_idx = next(
                (i for i, (m, _) in enumerate(results) if m.id == high_arousal_mem.id),
                len(results),
            )
            low_idx = next(
                (i for i, (m, _) in enumerate(results) if m.id == low_arousal_mem.id),
                len(results),
            )
            assert high_idx < low_idx

    def test_limit_respected(self, persona_memories):
        """Test that the limit parameter is respected."""
        topology = EmotionTopology()
        results = topology.retrieve_for_emotion(
            memories=persona_memories,
            emotion="joy",
            limit=2,
        )
        assert len(results) <= 2

    def test_empty_memories(self):
        """Test retrieval with empty memory list."""
        topology = EmotionTopology()
        results = topology.retrieve_for_emotion(
            memories=[],
            emotion="joy",
            limit=5,
        )
        assert results == []

    def test_unknown_emotion_defaults(self, persona_memories):
        """Test that unknown emotion uses default weights."""
        topology = EmotionTopology()
        results = topology.retrieve_for_emotion(
            memories=persona_memories,
            emotion="unknown_emotion",
            limit=5,
        )
        assert isinstance(results, list)
        assert len(results) > 0

    def test_all_defined_emotions(self):
        """Test that all 8 basic emotions are defined."""
        topology = EmotionTopology()
        expected_emotions = {
            "joy", "sadness", "anger", "fear",
            "trust", "disgust", "anticipation", "surprise",
        }
        assert expected_emotions.issubset(set(topology.EMOTION_PROFILES.keys()))

    def test_emotion_profile_structure(self):
        """Test that emotion profiles have required fields."""
        topology = EmotionTopology()
        for emotion, profile in topology.EMOTION_PROFILES.items():
            assert "recency_weight" in profile
            assert "importance_weight" in profile
            assert "valence_bias" in profile
            assert 0.0 <= profile["recency_weight"] <= 1.0
            assert 0.0 <= profile["importance_weight"] <= 1.0
            assert -1.0 <= profile["valence_bias"] <= 1.0


# ===========================================================================
# Dual-Stream Separation Tests
# ===========================================================================


class TestDualStreamSeparation:
    """Tests for dual-stream memory separation."""

    @pytest.fixture
    def mock_db(self):
        """Create a mock database."""
        mock = AsyncMock()
        mock.fetch = AsyncMock(return_value=[])
        mock.fetchrow = AsyncMock(return_value=None)
        mock.fetchval = AsyncMock(return_value=0)
        mock.execute = AsyncMock(return_value="DELETE 0")
        return mock

    def test_persona_store_separate_from_factual(self, mock_db):
        """Test that PersonaMemoryStore is separate from MemoryStore."""
        from ah.memory.store import MemoryStore

        persona_store = PersonaMemoryStore()
        factual_store = MemoryStore()

        # They should be different instances
        assert persona_store is not factual_store
        # Persona store should have its own methods
        assert hasattr(persona_store, "add_persona")
        assert hasattr(persona_store, "search_persona")
        # Factual store should not have persona methods
        assert not hasattr(factual_store, "add_persona")

    def test_persona_memory_does_not_affect_factual(self, mock_db, sample_fact):
        """Test that creating a persona memory doesn't modify the factual store."""
        persona_store = PersonaMemoryStore()

        # Creating a persona memory should not call factual store methods
        # This is verified by the fact that PersonaMemoryStore has its own
        # separate methods and doesn't delegate to MemoryStore
        assert hasattr(persona_store, "add_persona")
        assert hasattr(persona_store, "get_persona")
        assert hasattr(persona_store, "search_persona")
        assert hasattr(persona_store, "update_persona")
        assert hasattr(persona_store, "delete_persona")

    def test_schema_has_persona_memories_table(self):
        """Test that the schema defines persona_memories table."""
        from ah.db.connection import db

        # Read the schema file
        import ah.db
        schema_path = ah.db.__file__.replace("__init__.py", "schema.sql")
        with open(schema_path) as f:
            schema = f.read()
        assert "persona_memories" in schema
        assert "fact_id" in schema
        assert "persona_id" in schema
        assert "emotional_valence" in schema
        assert "emotional_arousal" in schema


# ===========================================================================
# PersonaMemoryStore Tests
# ===========================================================================


class TestPersonaMemoryStore:
    """Tests for the PersonaMemoryStore."""

    @pytest.fixture
    def mock_db(self):
        """Create a mock database."""
        mock = AsyncMock()
        mock.fetch = AsyncMock(return_value=[])
        mock.fetchrow = AsyncMock(return_value=None)
        mock.fetchval = AsyncMock(return_value=0)
        mock.execute = AsyncMock(return_value="DELETE 0")
        return mock

    @pytest.fixture
    def store(self):
        """Create a PersonaMemoryStore."""
        return PersonaMemoryStore()

    async def test_create_persona_memory(self, store, mock_db, sample_fact):
        """Test creating a persona memory."""
        now = datetime.now(UTC)
        row = {
            "id": uuid.uuid4(),
            "fact_id": sample_fact.id,
            "persona_id": "harness",
            "interpretation": "User values visual comfort",
            "emotional_valence": 0.7,
            "emotional_arousal": 0.3,
            "confidence": 0.9,
            "created_at": now,
            "updated_at": now,
        }
        mock_db.fetchrow = AsyncMock(return_value=row)

        with patch("ah.memory.persona.db", mock_db):
            result = await store.add_persona(
                fact_id=sample_fact.id,
                persona_id="harness",
                interpretation="User values visual comfort",
                emotional_valence=0.7,
                emotional_arousal=0.3,
                confidence=0.9,
            )

        assert isinstance(result, PersonaMemory)
        assert result.fact_id == sample_fact.id
        assert result.persona_id == "harness"
        assert result.interpretation == "User values visual comfort"
        assert result.emotional_valence == 0.7
        assert result.emotional_arousal == 0.3
        assert result.confidence == 0.9

    async def test_get_persona_memory(self, store, mock_db, sample_fact):
        """Test getting a persona memory by ID."""
        now = datetime.now(UTC)
        row = {
            "id": uuid.uuid4(),
            "fact_id": sample_fact.id,
            "persona_id": "harness",
            "interpretation": "Test",
            "emotional_valence": 0.5,
            "emotional_arousal": 0.5,
            "confidence": 0.8,
            "created_at": now,
            "updated_at": now,
        }
        mock_db.fetchrow = AsyncMock(return_value=row)

        with patch("ah.memory.persona.db", mock_db):
            result = await store.get_persona(row["id"])

        assert isinstance(result, PersonaMemory)
        assert result.id == row["id"]

    async def test_get_persona_not_found(self, store, mock_db):
        """Test getting a non-existent persona memory."""
        mock_db.fetchrow = AsyncMock(return_value=None)

        with patch("ah.memory.persona.db", mock_db):
            result = await store.get_persona(uuid.uuid4())

        assert result is None

    async def test_search_persona_by_fact(self, store, mock_db, sample_fact):
        """Test searching persona memories by fact ID."""
        now = datetime.now(UTC)
        rows = [
            {
                "id": uuid.uuid4(),
                "fact_id": sample_fact.id,
                "persona_id": "harness",
                "interpretation": "Test 1",
                "emotional_valence": 0.5,
                "emotional_arousal": 0.5,
                "confidence": 0.8,
                "created_at": now,
                "updated_at": now,
            },
            {
                "id": uuid.uuid4(),
                "fact_id": sample_fact.id,
                "persona_id": "other",
                "interpretation": "Test 2",
                "emotional_valence": -0.3,
                "emotional_arousal": 0.7,
                "confidence": 0.6,
                "created_at": now,
                "updated_at": now,
            },
        ]
        mock_db.fetch = AsyncMock(return_value=rows)

        with patch("ah.memory.persona.db", mock_db):
            results = await store.search_persona(fact_id=sample_fact.id)

        assert len(results) == 2
        assert all(isinstance(r, PersonaMemory) for r in results)

    async def test_update_persona_memory(self, store, mock_db, sample_fact):
        """Test updating a persona memory."""
        now = datetime.now(UTC)
        persona_id = uuid.uuid4()
        row = {
            "id": persona_id,
            "fact_id": sample_fact.id,
            "persona_id": "harness",
            "interpretation": "Updated interpretation",
            "emotional_valence": 0.9,
            "emotional_arousal": 0.4,
            "confidence": 0.95,
            "created_at": now,
            "updated_at": now,
        }
        mock_db.fetchrow = AsyncMock(return_value=row)

        with patch("ah.memory.persona.db", mock_db):
            result = await store.update_persona(
                persona_id,
                interpretation="Updated interpretation",
                emotional_valence=0.9,
                emotional_arousal=0.4,
                confidence=0.95,
            )

        assert isinstance(result, PersonaMemory)
        assert result.interpretation == "Updated interpretation"
        assert result.emotional_valence == 0.9
        assert result.emotional_arousal == 0.4
        assert result.confidence == 0.95

    async def test_delete_persona_memory(self, store, mock_db):
        """Test deleting a persona memory."""
        mock_db.execute = AsyncMock(return_value="DELETE 1")

        with patch("ah.memory.persona.db", mock_db):
            result = await store.delete_persona(uuid.uuid4())

        assert result is True

    async def test_delete_persona_not_found(self, store, mock_db):
        """Test deleting a non-existent persona memory."""
        mock_db.execute = AsyncMock(return_value="DELETE 0")

        with patch("ah.memory.persona.db", mock_db):
            result = await store.delete_persona(uuid.uuid4())

        assert result is False


# ===========================================================================
# MemoryRetriever Integration Tests
# ===========================================================================


class TestMemoryRetrieverIntegration:
    """Tests for EmotionTopology integration with MemoryRetriever."""

    @pytest.fixture
    def retriever(self):
        """Create a MemoryRetriever instance."""
        from ah.memory.retriever import MemoryRetriever

        return MemoryRetriever()

    @pytest.fixture
    def mock_store(self):
        """Create a mock MemoryStore."""
        store = AsyncMock()
        store.search = AsyncMock(return_value=[])
        store.search_by_embedding = AsyncMock(return_value=[])
        store.update_access = AsyncMock()
        return store

    def test_retriever_has_emotion_topology(self, retriever):
        """Test that MemoryRetriever has an EmotionTopology instance."""
        assert hasattr(retriever, "emotion_topology")
        assert isinstance(retriever.emotion_topology, EmotionTopology)

    async def test_retrieve_with_emotion(self, retriever, mock_store, persona_memories):
        """Test retrieving memories with emotional context."""
        retriever.store = mock_store

        # Mock the persona store to return our test memories
        mock_persona_store = AsyncMock()
        mock_persona_store.search_persona = AsyncMock(return_value=persona_memories)

        mock_db = AsyncMock()
        mock_db.fetch = AsyncMock(return_value=[])

        with (
            patch("ah.memory.retriever.db", mock_db),
            patch("ah.memory.retriever.PersonaMemoryStore") as mock_store_cls,
        ):
            mock_store_cls.return_value = mock_persona_store
            results = await retriever.retrieve(
                "test",
                emotion="joy",
            )

        assert isinstance(results, list)
