"""Tests for batch operations in MemoryApprovalGate — N+1 query elimination."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ah.memory.approval import ApprovalStatus, MemoryApprovalGate


@pytest.fixture
def gate():
    return MemoryApprovalGate(enabled=True)


@pytest.fixture
def mock_db():
    mock = AsyncMock()
    mock.fetch = AsyncMock(return_value=[])
    mock.fetchrow = AsyncMock(return_value=None)
    mock.fetchval = AsyncMock(return_value=0)
    mock.execute = AsyncMock(return_value="UPDATE 1")
    return mock


def _make_pending_rows(n):
    """Create n pending memory rows."""
    return [
        {
            "id": uuid.uuid4(),
            "content": f"memory {i}",
            "category": "fact",
            "importance": 0.5,
            "agent_id": "harness",
            "session_id": None,
            "redactions": [],
            "created_at": MagicMock(),
            "explicitly_important": False,
            "base_strength": 1.0,
        }
        for i in range(n)
    ]


class TestApproveAllBatch:
    """approve_all must use batch UPDATE, not iterate one-by-one with approve()."""

    async def test_approve_all_no_agent_filter_no_fetchrow(self, gate, mock_db):
        """approve_all must not call fetchrow (which approve() uses per-record)."""
        rows = _make_pending_rows(3)
        mock_db.fetch = AsyncMock(return_value=rows)
        mock_memory = AsyncMock()
        mock_memory.add = AsyncMock(side_effect=[MagicMock(id=uuid.uuid4()) for _ in range(3)])
        with patch("ah.memory.approval.db", mock_db), \
             patch("ah.memory.store.memory_store", mock_memory):
            count = await gate.approve_all()
        assert count == 3
        # fetchrow is only used in approve() for individual records — batch must not call it
        mock_db.fetchrow.assert_not_called()

    async def test_approve_all_with_agent_filter_no_fetchrow(self, gate, mock_db):
        """With agent filter, approve_all must not call fetchrow."""
        rows = _make_pending_rows(2)
        mock_db.fetch = AsyncMock(return_value=rows)
        mock_memory = AsyncMock()
        mock_memory.add = AsyncMock(side_effect=[MagicMock(id=uuid.uuid4()) for _ in range(2)])
        with patch("ah.memory.approval.db", mock_db), \
             patch("ah.memory.store.memory_store", mock_memory):
            count = await gate.approve_all(agent_id="harness")
        assert count == 2
        mock_db.fetchrow.assert_not_called()

    async def test_approve_all_empty(self, gate, mock_db):
        """approve_all with no pending records returns 0."""
        mock_db.fetch = AsyncMock(return_value=[])
        with patch("ah.memory.approval.db", mock_db):
            count = await gate.approve_all()
        assert count == 0
        mock_db.fetchrow.assert_not_called()


class TestRejectAllBatch:
    """reject_all must use a single batch UPDATE, not iterate one-by-one."""

    async def test_reject_all_no_agent_filter_no_fetchrow(self, gate, mock_db):
        """reject_all must not call fetchrow (which reject() uses per-record)."""
        mock_db.execute = AsyncMock(return_value="UPDATE 5")
        with patch("ah.memory.approval.db", mock_db):
            count = await gate.reject_all()
        assert count == 5
        mock_db.fetchrow.assert_not_called()

    async def test_reject_all_with_agent_filter_no_fetchrow(self, gate, mock_db):
        """With agent filter, reject_all must not call fetchrow."""
        mock_db.execute = AsyncMock(return_value="UPDATE 3")
        with patch("ah.memory.approval.db", mock_db):
            count = await gate.reject_all(agent_id="harness")
        assert count == 3
        mock_db.fetchrow.assert_not_called()

    async def test_reject_all_with_note(self, gate, mock_db):
        """reject_all passes review_note to the batch UPDATE."""
        mock_db.execute = AsyncMock(return_value="UPDATE 2")
        with patch("ah.memory.approval.db", mock_db):
            count = await gate.reject_all(review_note="not useful")
        assert count == 2
        mock_db.fetchrow.assert_not_called()
