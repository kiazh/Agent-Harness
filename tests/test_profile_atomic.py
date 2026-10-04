"""Tests for atomic record_interaction in UserProfileStore."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ah.memory.user_profile import UserProfileStore


@pytest.fixture
def store():
    return UserProfileStore()


@pytest.fixture
def mock_db():
    mock = AsyncMock()
    mock.fetch = AsyncMock(return_value=[])
    mock.fetchrow = AsyncMock(return_value=None)
    mock.fetchval = AsyncMock(return_value=0)
    mock.execute = AsyncMock(return_value="UPDATE 1")
    return mock


class TestRecordInteractionAtomic:
    """record_interaction must use a single atomic query, not SELECT then UPDATE."""

    async def test_record_interaction_with_topic_no_select(self, store, mock_db):
        """record_interaction with topic must not do a separate SELECT first."""
        mock_db.fetchrow = AsyncMock(return_value={
            "id": uuid.uuid4(),
            "user_id": "test",
            "display_name": "",
            "preferences": "{}",
            "interaction_count": 1,
            "topics": '{"python": 1}',
            "last_topics": '["python"]',
            "created_at": MagicMock(),
            "updated_at": MagicMock(),
        })
        with patch("ah.memory.user_profile.db", mock_db):
            result = await store.record_interaction(uuid.uuid4(), topic="python")
        assert result is not None
        # Should NOT have a separate SELECT — the atomic version uses a single query
        # Check that fetchrow was called only once (the UPDATE ... RETURNING)
        assert mock_db.fetchrow.call_count == 1

    async def test_record_interaction_without_topic_no_select(self, store, mock_db):
        """record_interaction without topic must not do a separate SELECT first."""
        mock_db.fetchrow = AsyncMock(return_value={
            "id": uuid.uuid4(),
            "user_id": "test",
            "display_name": "",
            "preferences": "{}",
            "interaction_count": 1,
            "topics": "{}",
            "last_topics": "[]",
            "created_at": MagicMock(),
            "updated_at": MagicMock(),
        })
        with patch("ah.memory.user_profile.db", mock_db):
            result = await store.record_interaction(uuid.uuid4())
        assert result is not None
        assert mock_db.fetchrow.call_count == 1

    async def test_record_interaction_returns_none_for_missing_profile(self, store, mock_db):
        """record_interaction returns None when profile doesn't exist."""
        mock_db.fetchrow = AsyncMock(return_value=None)
        with patch("ah.memory.user_profile.db", mock_db):
            result = await store.record_interaction(uuid.uuid4(), topic="python")
        assert result is None
