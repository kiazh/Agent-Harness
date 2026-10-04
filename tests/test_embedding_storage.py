"""Tests for ApprovalMemory.embedding storage — must not always be None."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ah.memory.approval import MemoryApprovalGate


@pytest.fixture
def gate():
    return MemoryApprovalGate(enabled=True)


@pytest.fixture
def mock_db():
    mock = AsyncMock()
    mock.fetch = AsyncMock(return_value=[])
    mock.fetchrow = AsyncMock(return_value=None)
    mock.fetchval = AsyncMock(return_value=0)
    mock.execute = AsyncMock(return_value="INSERT 0 1")
    return mock


class TestEmbeddingStorage:
    """submit() must store the embedding, not always None."""

    async def test_submit_stores_embedding(self, gate, mock_db):
        """When embedding is provided, it must be stored in the database."""
        embedding = [0.1, 0.2, 0.3, 0.4]
        with patch("ah.memory.approval.db", mock_db):
            pending = await gate.submit(
                content="test memory",
                category="fact",
                embedding=embedding,
            )
        assert pending is not None
        # PostgreSQL's vector parameter is sent in pgvector text format.
        assert mock_db.execute.await_args.args[-1] == "[0.1,0.2,0.3,0.4]"

    async def test_submit_without_embedding_stores_none(self, gate, mock_db):
        """When no embedding is provided, None should be stored."""
        with patch("ah.memory.approval.db", mock_db):
            pending = await gate.submit(
                content="test memory",
                category="fact",
            )
        assert pending is not None
        # The embedding arg should be None
        call_args = mock_db.execute.call_args
        args = call_args[0]
        # Find the embedding position (last arg before any trailing ones)
        # The embedding should be None when not provided
        assert None in args

    async def test_submit_with_empty_embedding(self, gate, mock_db):
        """When empty list embedding is provided, it should be stored."""
        with patch("ah.memory.approval.db", mock_db):
            pending = await gate.submit(
                content="test memory",
                category="fact",
                embedding=[],
            )
        assert pending is not None


def _approval_connection():
    database = AsyncMock()
    connection = AsyncMock()
    connection.transaction = MagicMock(return_value=AsyncMock())
    manager = AsyncMock()
    manager.__aenter__.return_value = connection
    database.acquire = MagicMock(return_value=manager)
    return database, connection


@pytest.mark.asyncio
async def test_single_approval_preserves_pending_embedding(gate):
    database, connection = _approval_connection()
    pending_id = uuid.uuid4()
    row = {
        "id": pending_id,
        "status": "pending",
        "session_id": None,
        "agent_id": "harness",
        "content": "important fact",
        "category": "fact",
        "importance": 0.5,
        "explicitly_important": False,
        "base_strength": 1.0,
        "embedding": "[0.1,0.2,0.3]",
    }

    async def fetchrow(query, *_args):
        assert "embedding" in query
        return row

    connection.fetchrow = AsyncMock(side_effect=fetchrow)
    memory = MagicMock(id=uuid.uuid4())
    store = MagicMock(add=AsyncMock(return_value=memory))
    with patch("ah.memory.approval.db", database), patch("ah.memory.approval.memory_store", store):
        approved = await gate.approve(pending_id)

    assert approved is memory
    assert store.add.await_args.kwargs["embedding"] == [0.1, 0.2, 0.3]


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_id", [None, "harness"])
async def test_batch_approval_preserves_pending_embedding(gate, agent_id):
    database, connection = _approval_connection()
    row = {
        "id": uuid.uuid4(),
        "status": "pending",
        "session_id": None,
        "agent_id": "harness",
        "content": "important fact",
        "category": "fact",
        "importance": 0.5,
        "explicitly_important": False,
        "base_strength": 1.0,
        "embedding": "[0.4,0.5,0.6]",
    }

    async def fetch(query, *_args):
        assert "embedding" in query
        return [row]

    connection.fetch = AsyncMock(side_effect=fetch)
    store = MagicMock(add=AsyncMock(return_value=MagicMock(id=uuid.uuid4())))
    with patch("ah.memory.approval.db", database), patch("ah.memory.approval.memory_store", store):
        count = await gate.approve_all(agent_id=agent_id)

    assert count == 1
    assert store.add.await_args.kwargs["embedding"] == [0.4, 0.5, 0.6]
