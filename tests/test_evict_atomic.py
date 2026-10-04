"""Tests for evict_weak_memories atomicity — race condition fix."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from ah.memory.store import MemoryStore


@pytest.fixture
def store():
    return MemoryStore()


@pytest.fixture
def mock_db():
    mock = AsyncMock()
    mock.fetch = AsyncMock(return_value=[])
    mock.fetchrow = AsyncMock(return_value=None)
    mock.fetchval = AsyncMock(return_value=0)
    mock.execute = AsyncMock(return_value="DELETE 0")
    return mock


class TestEvictWeakMemoriesAtomic:
    """evict_weak_memories with max_memories must use a single CTE query."""

    async def test_evict_with_max_memories_no_agent_single_query(self, store, mock_db):
        """With max_memories and no agent filter, should use single CTE query."""
        mock_db.fetchval = AsyncMock(return_value=100)
        mock_db.execute = AsyncMock(return_value="DELETE 90")
        with patch("ah.memory.store.db", mock_db):
            count = await store.evict_weak_memories(max_memories=10)
        assert count == 90
        # Should NOT have separate COUNT then DELETE — should use CTE
        # fetchval is only used in the non-atomic version
        mock_db.fetchval.assert_not_called()

    async def test_evict_with_max_memories_agent_filter_single_query(self, store, mock_db):
        """With max_memories and agent filter, should use single CTE query."""
        mock_db.fetchval = AsyncMock(return_value=50)
        mock_db.execute = AsyncMock(return_value="DELETE 40")
        with patch("ah.memory.store.db", mock_db):
            count = await store.evict_weak_memories(max_memories=10, agent_id="harness")
        assert count == 40
        mock_db.fetchval.assert_not_called()

    async def test_evict_with_max_memories_no_eviction_needed(self, store, mock_db):
        """When count <= max_memories, should return 0 without DELETE."""
        mock_db.fetchval = AsyncMock(return_value=5)
        mock_db.execute = AsyncMock(return_value="DELETE 0")
        with patch("ah.memory.store.db", mock_db):
            count = await store.evict_weak_memories(max_memories=10)
        assert count == 0

    async def test_evict_by_threshold_still_works(self, store, mock_db):
        """Threshold-based eviction should still work as before."""
        mock_db.execute = AsyncMock(return_value="DELETE 3")
        with patch("ah.memory.store.db", mock_db):
            count = await store.evict_weak_memories(threshold=0.05)
        assert count == 3

    async def test_evict_by_threshold_with_agent(self, store, mock_db):
        """Threshold-based eviction with agent filter should still work."""
        mock_db.execute = AsyncMock(return_value="DELETE 2")
        with patch("ah.memory.store.db", mock_db):
            count = await store.evict_weak_memories(threshold=0.05, agent_id="harness")
        assert count == 2
