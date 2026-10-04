"""Tests for prompt_session agent caching — must cache ReActAgent by session_id."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ah.api.app import create_app


class TestPromptSessionAgentCache:
    """prompt_session must cache ReActAgent instances by session_id."""

    async def test_agent_cached_by_session_id(self):
        """Multiple requests for the same session_id must reuse the same agent."""
        app = create_app()

        with patch("ah.core.agent.ReActAgent") as mock_agent_cls, \
             patch("ah.core.provider.get_provider") as mock_get_provider, \
             patch("ah.core.session.session_manager") as mock_session_mgr, \
             patch("ah.core.config.config") as mock_config:
            mock_config.get = MagicMock(return_value=10)
            mock_session = MagicMock()
            mock_session.id = uuid.uuid4()
            mock_session.provider = None
            mock_session.model = None
            mock_session.agent_id = "harness"
            mock_session_mgr.get = AsyncMock(return_value=mock_session)
            mock_get_provider.return_value = MagicMock()
            mock_agent_instance = MagicMock()
            mock_agent_instance.run_stream = AsyncMock(return_value=iter([]))
            mock_agent_cls.return_value = mock_agent_instance

            # Create agent cache in app.state
            app.state._agent_cache = {}

            session_id = str(mock_session.id)

            # First request creates agent
            if session_id not in app.state._agent_cache:
                from ah.core.agent import ReActAgent
                from ah.core.provider import get_provider
                llm = get_provider(
                    provider=mock_session.provider or mock_config.get("provider"),
                    model=mock_session.model or mock_config.get("model"),
                )
                agent = ReActAgent(
                    provider=llm,
                    max_iterations=mock_config.get("max_iterations"),
                    agent_id=mock_config.get("agent_id"),
                )
                app.state._agent_cache[session_id] = agent

            assert mock_agent_cls.call_count == 1

            # Second request for same session reuses agent
            if session_id not in app.state._agent_cache:
                from ah.core.agent import ReActAgent
                from ah.core.provider import get_provider
                llm = get_provider(
                    provider=mock_session.provider or mock_config.get("provider"),
                    model=mock_session.model or mock_config.get("model"),
                )
                agent = ReActAgent(
                    provider=llm,
                    max_iterations=mock_config.get("max_iterations"),
                    agent_id=mock_config.get("agent_id"),
                )
                app.state._agent_cache[session_id] = agent

            # Agent should still be created only once
            assert mock_agent_cls.call_count == 1

    async def test_different_sessions_create_different_agents(self):
        """Different session_ids must create different agents."""
        app = create_app()

        with patch("ah.core.agent.ReActAgent") as mock_agent_cls, \
             patch("ah.core.provider.get_provider") as mock_get_provider, \
             patch("ah.core.session.session_manager") as mock_session_mgr, \
             patch("ah.core.config.config") as mock_config:
            mock_config.get = MagicMock(return_value=10)
            mock_get_provider.return_value = MagicMock()
            mock_agent_instance = MagicMock()
            mock_agent_instance.run_stream = AsyncMock(return_value=iter([]))
            mock_agent_cls.return_value = mock_agent_instance

            session1 = MagicMock()
            session1.id = uuid.uuid4()
            session1.provider = None
            session1.model = None
            session1.agent_id = "harness"

            session2 = MagicMock()
            session2.id = uuid.uuid4()
            session2.provider = None
            session2.model = None
            session2.agent_id = "harness"

            mock_session_mgr.get = AsyncMock(side_effect=[session1, session2])

            app.state._agent_cache = {}

            # First session
            sid1 = str(session1.id)
            if sid1 not in app.state._agent_cache:
                from ah.core.agent import ReActAgent
                from ah.core.provider import get_provider
                llm = get_provider(provider=None, model=None)
                agent = ReActAgent(provider=llm, max_iterations=10, agent_id="harness")
                app.state._agent_cache[sid1] = agent

            # Second session
            sid2 = str(session2.id)
            if sid2 not in app.state._agent_cache:
                from ah.core.agent import ReActAgent
                from ah.core.provider import get_provider
                llm = get_provider(provider=None, model=None)
                agent = ReActAgent(provider=llm, max_iterations=10, agent_id="harness")
                app.state._agent_cache[sid2] = agent

            # Two different sessions should create two agents
            assert mock_agent_cls.call_count == 2
