"""Tests for orchestrator deadlock guards — hop counter prevents circular delegation."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ah.core.orchestrator import Orchestrator


@pytest.fixture
def orchestrator():
    return Orchestrator(agent_factory=MagicMock())


class TestDeadlockGuard:
    """Orchestrator must prevent circular delegation with a hop counter."""

    async def test_delegate_rejects_excessive_hops(self, orchestrator):
        """delegate() must reject when hop count exceeds maximum."""
        mock_agent = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = "test response"
        mock_response.tokens_used = 10
        mock_response.iterations = 1
        mock_agent.run = AsyncMock(return_value=mock_response)

        mock_factory = MagicMock(return_value=mock_agent)
        orchestrator._agent_factory = mock_factory

        mock_agent_def = MagicMock()
        mock_agent_def.model = None
        mock_agent_def.provider = None
        mock_agent_def.max_iterations = 10
        mock_agent_def.system_prompt = None
        mock_agent_def.tools = None

        with patch("ah.core.orchestrator.agent_registry") as mock_registry, \
             patch("ah.core.orchestrator.session_manager") as mock_session_mgr, \
             patch("ah.core.orchestrator.context_manager") as mock_ctx_mgr, \
             patch("ah.core.orchestrator.db") as mock_db:
            mock_registry.get = AsyncMock(return_value=mock_agent_def)
            mock_session = MagicMock()
            mock_session.id = uuid.uuid4()
            mock_session.agent_id = "test-agent"
            mock_session.model = None
            mock_session.provider = None
            mock_session.context_budget = 8000
            mock_session.goal = None
            mock_session_mgr.create = AsyncMock(return_value=mock_session)
            mock_session_mgr.get = AsyncMock(return_value=mock_session)
            mock_db.execute = AsyncMock(return_value="INSERT 0 1")
            mock_ctx_mgr.get_recent_context = AsyncMock(return_value=[])
            mock_ctx_mgr.add_chunk = AsyncMock()

            # Excessive hop count should raise or return error
            with pytest.raises((ValueError, RecursionError)) as exc_info:
                await orchestrator.delegate(
                    "test-agent", "do something", _hop_count=100
                )
            assert "hop" in str(exc_info.value).lower() or "max" in str(exc_info.value).lower()

    async def test_delegate_allows_normal_hops(self, orchestrator):
        """delegate() must allow normal hop counts."""
        mock_agent = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = "test response"
        mock_response.tokens_used = 10
        mock_response.iterations = 1
        mock_agent.run = AsyncMock(return_value=mock_response)

        mock_factory = MagicMock(return_value=mock_agent)
        orchestrator._agent_factory = mock_factory

        mock_agent_def = MagicMock()
        mock_agent_def.model = None
        mock_agent_def.provider = None
        mock_agent_def.max_iterations = 10
        mock_agent_def.system_prompt = None
        mock_agent_def.tools = None

        with patch("ah.core.orchestrator.agent_registry") as mock_registry, \
             patch("ah.core.orchestrator.session_manager") as mock_session_mgr, \
             patch("ah.core.orchestrator.context_manager") as mock_ctx_mgr, \
             patch("ah.core.orchestrator.db") as mock_db:
            mock_registry.get = AsyncMock(return_value=mock_agent_def)
            mock_session = MagicMock()
            mock_session.id = uuid.uuid4()
            mock_session.agent_id = "test-agent"
            mock_session.model = None
            mock_session.provider = None
            mock_session.context_budget = 8000
            mock_session.goal = None
            mock_session_mgr.create = AsyncMock(return_value=mock_session)
            mock_session_mgr.get = AsyncMock(return_value=mock_session)
            mock_db.execute = AsyncMock(return_value="INSERT 0 1")
            mock_ctx_mgr.get_recent_context = AsyncMock(return_value=[])
            mock_ctx_mgr.add_chunk = AsyncMock()

            # Normal hop count should work fine
            result = await orchestrator.delegate(
                "test-agent", "do something", _hop_count=0
            )
            assert result is not None
            assert result.status == "complete"

    async def test_delegate_rejects_negative_hops(self, orchestrator):
        """delegate() must reject negative hop counts."""
        mock_agent_def = MagicMock()
        mock_agent_def.model = None
        mock_agent_def.provider = None
        mock_agent_def.max_iterations = 10
        mock_agent_def.system_prompt = None
        mock_agent_def.tools = None

        with patch("ah.core.orchestrator.agent_registry") as mock_registry:
            mock_registry.get = AsyncMock(return_value=mock_agent_def)

            with pytest.raises((ValueError, RecursionError)):
                await orchestrator.delegate(
                    "test-agent", "do something", _hop_count=-1
                )
