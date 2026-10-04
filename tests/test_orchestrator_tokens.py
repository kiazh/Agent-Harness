"""Tests for orchestrator token estimation — must use assembler.get_token_count()."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ah.core.orchestrator import Orchestrator, DelegationResult


@pytest.fixture
def orchestrator():
    return Orchestrator(agent_factory=MagicMock())


class TestTokenEstimation:
    """Orchestrator must use assembler.get_token_count() for token estimation."""

    async def test_delegate_uses_get_token_count(self, orchestrator):
        """delegate() must use assembler.get_token_count() for token estimation."""
        mock_agent = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = "test response content"
        mock_response.tokens_used = 42
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
             patch("ah.core.orchestrator.db") as mock_db, \
             patch("ah.core.orchestrator.get_token_count", return_value=42) as mock_get_tokens:
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

            result = await orchestrator.delegate(
                "test-agent", "do something", parent_session_id=uuid.uuid4()
            )

        assert result is not None
        # The token count in the result should come from response.tokens_used
        assert result.tokens == 42
        # get_token_count should have been called for the context chunk
        mock_get_tokens.assert_called()

    async def test_delegate_token_count_accurate(self, orchestrator):
        """Token count must use assembler.get_token_count(), not len//4."""
        mock_agent = AsyncMock()
        mock_response = MagicMock()
        # Content where len//4 differs significantly from actual token count
        mock_response.content = "a" * 100  # len//4 = 25, but actual tokens may differ
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
             patch("ah.core.orchestrator.db") as mock_db, \
             patch("ah.core.orchestrator.get_token_count", return_value=10) as mock_get_tokens:
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

            result = await orchestrator.delegate(
                "test-agent", "do something", parent_session_id=uuid.uuid4()
            )

        assert result.tokens == 10
        # Verify get_token_count was called (not len//4)
        mock_get_tokens.assert_called()
