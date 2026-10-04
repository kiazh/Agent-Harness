"""Tests for ApprovalMemory.embedding storage — must not always be None."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

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
        # Check that db.execute was called with the embedding (not None)
        call_args = mock_db.execute.call_args
        # The embedding should be in the call args, not None
        assert call_args is not None
        args = call_args[0]
        # Find the embedding argument — it should be the list we passed
        assert any(arg == embedding for arg in args if isinstance(arg, list))

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
