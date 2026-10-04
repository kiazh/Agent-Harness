"""Tests for the identity gate — AgentBelief, IdentityGate, MemoryProvenance."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from ah.memory.identity import (
    AgentBelief,
    IdentityGate,
    MemoryProvenance,
)
from ah.memory.models import MemoryEntry


def _make_belief(**overrides):
    """Create a sample AgentBelief."""
    defaults = {
        "agent_id": "harness",
        "known_facts": {"Python": 0.9, "JavaScript": 0.7},
        "traits": {"analytical": 0.8, "creative": 0.6},
        "values": {"accuracy": 0.9, "helpfulness": 0.8},
        "updated_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    return AgentBelief(**defaults)


def _make_memory(**overrides):
    """Create a sample MemoryEntry."""
    defaults = {
        "id": uuid.uuid4(),
        "session_id": uuid.uuid4(),
        "agent_id": "harness",
        "content": "Python is a great programming language",
        "category": "fact",
        "importance": 0.7,
    }
    defaults.update(overrides)
    return MemoryEntry(**defaults)


def _make_belief_row(**overrides):
    """Create a mock DB row for agent_beliefs."""
    row = {
        "agent_id": "harness",
        "belief": {
            "agent_id": "harness",
            "known_facts": {"Python": 0.9, "JavaScript": 0.7},
            "traits": {"analytical": 0.8, "creative": 0.6},
            "values": {"accuracy": 0.9, "helpfulness": 0.8},
            "updated_at": datetime.now(UTC).isoformat(),
        },
        "version": 1,
        "updated_at": datetime.now(UTC),
    }
    row.update(overrides)
    return row


# ===========================================================================
# AgentBelief Tests
# ===========================================================================


class TestAgentBelief:
    """Tests for AgentBelief dataclass."""

    def test_agent_belief_creation(self):
        """Verify AgentBelief can be created and stored."""
        belief = _make_belief()
        assert belief.agent_id == "harness"
        assert belief.known_facts["Python"] == 0.9
        assert belief.traits["analytical"] == 0.8
        assert belief.values["accuracy"] == 0.9
        assert isinstance(belief.updated_at, datetime)

    def test_agent_belief_to_dict(self):
        """Test AgentBelief serialization to dict."""
        belief = _make_belief()
        d = belief.to_dict()
        assert d["agent_id"] == "harness"
        assert d["known_facts"]["Python"] == 0.9
        assert d["traits"]["analytical"] == 0.8
        assert d["values"]["accuracy"] == 0.9

    def test_agent_belief_from_dict(self):
        """Test AgentBelief deserialization from dict."""
        belief = _make_belief()
        d = belief.to_dict()
        restored = AgentBelief.from_dict(d)
        assert restored.agent_id == belief.agent_id
        assert restored.known_facts == belief.known_facts
        assert restored.traits == belief.traits
        assert restored.values == belief.values


# ===========================================================================
# MemoryProvenance Tests
# ===========================================================================


class TestMemoryProvenance:
    """Tests for MemoryProvenance dataclass."""

    def test_memory_provenance_signature(self, monkeypatch):
        """Verify memory provenance is signed."""
        monkeypatch.setenv("AGENT_HARNESS_PROVENANCE_KEY", "test-provenance-secret")
        memory_id = uuid.uuid4()
        content = "Test memory content"
        source_agent = "harness"

        signature = MemoryProvenance.compute_signature(memory_id, content, source_agent)
        assert signature is not None
        assert len(signature) == 64  # SHA-256 hex digest

        provenance = MemoryProvenance(
            memory_id=memory_id,
            source_agent=source_agent,
            signature=signature,
            parent_memory_id=None,
            created_at=datetime.now(UTC),
        )
        assert provenance.verify(memory_id, content) is True
        assert provenance.verify(memory_id, "Tampered content") is False

    def test_memory_provenance_with_parent(self):
        """Test provenance with parent memory reference."""
        memory_id = uuid.uuid4()
        parent_id = uuid.uuid4()
        provenance = MemoryProvenance(
            memory_id=memory_id,
            source_agent="harness",
            signature="sig",
            parent_memory_id=parent_id,
            created_at=datetime.now(UTC),
        )
        assert provenance.parent_memory_id == parent_id


# ===========================================================================
# IdentityGate Tests
# ===========================================================================


class TestIdentityGate:
    """Tests for IdentityGate validation."""

    @pytest.fixture
    def gate(self):
        return IdentityGate()

    @pytest.fixture
    def mock_db(self):
        mock = AsyncMock()
        mock.fetchrow = AsyncMock(return_value=None)
        mock.fetch = AsyncMock(return_value=[])
        mock.fetchval = AsyncMock(return_value=0)
        mock.execute = AsyncMock(return_value="UPDATE 1")
        return mock

    async def test_validate_incoming_consistent(self, gate, mock_db):
        """Verify consistent memories pass validation."""
        memory = _make_memory(content="Python is a great programming language")

        with patch("ah.memory.identity.db", mock_db):
            mock_db.fetchrow = AsyncMock(
                side_effect=[
                    _make_belief_row(),  # load beliefs
                    None,  # no provenance
                ]
            )
            result = await gate.validate_incoming("harness", memory)

        assert result.is_valid is True
        assert result.confidence > 0.5

    async def test_validate_incoming_inconsistent(self, gate, mock_db):
        """Verify inconsistent memories are rejected."""
        memory = _make_memory(content="Python is terrible and should never be used")

        with patch("ah.memory.identity.db", mock_db):
            mock_db.fetchrow = AsyncMock(return_value=_make_belief_row())
            result = await gate.validate_incoming("harness", memory)

        assert result.is_valid is False
        assert "inconsistent" in result.reason.lower() or "contradict" in result.reason.lower()

    async def test_validate_incoming_irrelevant(self, gate, mock_db):
        """Verify irrelevant memories are rejected."""
        memory = _make_memory(content="The weather is nice today")

        with patch("ah.memory.identity.db", mock_db):
            mock_db.fetchrow = AsyncMock(return_value=_make_belief_row())
            result = await gate.validate_incoming("harness", memory)

        assert result.is_valid is False
        assert "irrelevant" in result.reason.lower() or "relevance" in result.reason.lower()

    def test_drift_detection(self, gate):
        """Verify drift is detected when beliefs diverge."""
        before = _make_belief()
        after = _make_belief(known_facts={"Python": 0.1, "JavaScript": 0.7})

        drift_score = gate.detect_drift(before, after)

        assert drift_score > 0.0
        assert drift_score <= 1.0

    async def test_drift_containment(self, gate, mock_db):
        """Verify affected memories are quarantined on drift."""
        memory_ids = [uuid.uuid4(), uuid.uuid4()]

        with patch("ah.memory.identity.db", mock_db):
            mock_db.execute = AsyncMock(return_value="UPDATE 2")
            result = await gate.contain_drift("harness", memory_ids)

        assert result is True
