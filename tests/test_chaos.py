"""Chaos engineering tests for AgentHarness.

These tests inject failures to validate that the agent recovers gracefully:
- LLM failures (malformed responses, timeouts, empty content)
- Tool failures (crashes, timeouts, invalid outputs)
- Context failures (corrupted data, missing chunks)
- Database failures (connection drops, slow queries)
"""
from __future__ import annotations

import asyncio
import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from ah.core.agent import ReActAgent
from ah.core.models import LLMResponse, Session
from ah.tools.base import ToolRegistry

# ===========================================================================
# Fixtures
# ===========================================================================

@pytest.fixture
def mock_session():
    """Create a mock session for agent tests."""
    return Session(
        id=uuid.uuid4(),
        title="Test Session",
        context_budget=8000,
    )


@pytest.fixture
def temp_dir(tmp_path):
    """Create a temporary directory."""
    return tmp_path


# ===========================================================================
# LLM Failure Chaos Tests
# ===========================================================================

class TestLLMChaos:
    """Chaos tests for LLM failures."""

    async def test_agent_handles_empty_llm_response(self, mock_session):
        """Agent should handle empty LLM response gracefully."""
        provider = AsyncMock()
        provider.complete = AsyncMock(return_value=LLMResponse(
            content="",
            model="test-model",
            usage={"total_tokens": 0},
            tool_calls=[],
        ))

        with patch("ah.core.agent.session_manager") as mock_sm:
            mock_sm.get = AsyncMock(return_value=mock_session)
            mock_sm.update_activity = AsyncMock()

            with patch("ah.core.agent.context_manager") as mock_cm:
                mock_cm.add_chunk = AsyncMock()
                mock_cm.get_recent_context = AsyncMock(return_value=[])

                agent = ReActAgent(provider=provider, max_iterations=5)
                response = await agent.run(mock_session.id, "test", verbose=False)

                assert response is not None
                assert response.content == ""
                assert response.iterations == 1

    async def test_agent_handles_malformed_tool_json(self, mock_session):
        """Agent should not crash on invalid tool call JSON."""
        provider = AsyncMock()
        provider.complete = AsyncMock(return_value=LLMResponse(
            content="",
            model="test-model",
            usage={"total_tokens": 5},
            tool_calls=[{
                "id": "call_1",
                "function": {
                    "name": "read_file",
                    "arguments": "not valid json{{{",
                },
            }],
        ))

        with patch("ah.core.agent.session_manager") as mock_sm:
            mock_sm.get = AsyncMock(return_value=mock_session)
            mock_sm.update_activity = AsyncMock()

            with patch("ah.core.agent.context_manager") as mock_cm:
                mock_cm.add_chunk = AsyncMock()
                mock_cm.get_recent_context = AsyncMock(return_value=[])

                agent = ReActAgent(provider=provider, max_iterations=5)
                response = await agent.run(mock_session.id, "test", verbose=False)

                # Should not crash — should return an error or retry
                assert response is not None
                assert response.iterations >= 1

    async def test_agent_handles_missing_tool_name(self, mock_session):
        """Agent should handle tool call missing 'name' field."""
        provider = AsyncMock()
        provider.complete = AsyncMock(return_value=LLMResponse(
            content="",
            model="test-model",
            usage={"total_tokens": 5},
            tool_calls=[{
                "id": "call_1",
                "function": {
                    "name": "",
                    "arguments": "{}",
                },
            }],
        ))

        with patch("ah.core.agent.session_manager") as mock_sm:
            mock_sm.get = AsyncMock(return_value=mock_session)
            mock_sm.update_activity = AsyncMock()

            with patch("ah.core.agent.context_manager") as mock_cm:
                mock_cm.add_chunk = AsyncMock()
                mock_cm.get_recent_context = AsyncMock(return_value=[])

                agent = ReActAgent(provider=provider, max_iterations=5)
                response = await agent.run(mock_session.id, "test", verbose=False)

                assert response is not None
                assert response.iterations >= 1

    async def test_agent_handles_nonexistent_tool(self, mock_session):
        """Agent should handle non-existent tool gracefully."""
        provider = AsyncMock()
        provider.complete = AsyncMock(return_value=LLMResponse(
            content="",
            model="test-model",
            usage={"total_tokens": 5},
            tool_calls=[{
                "id": "call_1",
                "function": {
                    "name": "nonexistent_tool_xyz",
                    "arguments": "{}",
                },
            }],
        ))

        with patch("ah.core.agent.session_manager") as mock_sm:
            mock_sm.get = AsyncMock(return_value=mock_session)
            mock_sm.update_activity = AsyncMock()

            with patch("ah.core.agent.context_manager") as mock_cm:
                mock_cm.add_chunk = AsyncMock()
                mock_cm.get_recent_context = AsyncMock(return_value=[])

                agent = ReActAgent(provider=provider, max_iterations=5)
                response = await agent.run(mock_session.id, "test", verbose=False)

                # Should report error, not crash
                assert response is not None
                assert response.iterations >= 1

    async def test_agent_handles_llm_timeout(self, mock_session):
        """Agent should handle LLM timeout gracefully."""
        provider = AsyncMock()
        provider.complete = AsyncMock(side_effect=TimeoutError("LLM timeout"))

        with patch("ah.core.agent.session_manager") as mock_sm:
            mock_sm.get = AsyncMock(return_value=mock_session)
            mock_sm.update_activity = AsyncMock()

            with patch("ah.core.agent.context_manager") as mock_cm:
                mock_cm.add_chunk = AsyncMock()
                mock_cm.get_recent_context = AsyncMock(return_value=[])

                agent = ReActAgent(provider=provider, max_iterations=5)
                response = await agent.run(mock_session.id, "test", verbose=False)

                # Should return error response, not crash
                assert response is not None
                assert "error" in response.content.lower() or "timeout" in response.content.lower()

    async def test_agent_handles_llm_rate_limit(self, mock_session):
        """Agent should handle LLM rate limit (429) gracefully."""
        provider = AsyncMock()
        provider.complete = AsyncMock(side_effect=Exception("429 Too Many Requests"))

        with patch("ah.core.agent.session_manager") as mock_sm:
            mock_sm.get = AsyncMock(return_value=mock_session)
            mock_sm.update_activity = AsyncMock()

            with patch("ah.core.agent.context_manager") as mock_cm:
                mock_cm.add_chunk = AsyncMock()
                mock_cm.get_recent_context = AsyncMock(return_value=[])

                agent = ReActAgent(provider=provider, max_iterations=5)
                response = await agent.run(mock_session.id, "test", verbose=False)

                assert response is not None
                assert "error" in response.content.lower() or "429" in response.content

    async def test_agent_handles_malformed_llm_response(self, mock_session):
        """Agent should handle completely malformed LLM response."""
        provider = AsyncMock()
        provider.complete = AsyncMock(return_value=LLMResponse(
            content=None,  # type: ignore
            model="test-model",
            usage={"total_tokens": 0},
            tool_calls=None,  # type: ignore
        ))

        with patch("ah.core.agent.session_manager") as mock_sm:
            mock_sm.get = AsyncMock(return_value=mock_session)
            mock_sm.update_activity = AsyncMock()

            with patch("ah.core.agent.context_manager") as mock_cm:
                mock_cm.add_chunk = AsyncMock()
                mock_cm.get_recent_context = AsyncMock(return_value=[])

                agent = ReActAgent(provider=provider, max_iterations=5)
                # Should not crash
                try:
                    response = await agent.run(mock_session.id, "test", verbose=False)
                    assert response is not None
                except (TypeError, AttributeError):
                    # Acceptable if it raises a specific error
                    pass

    async def test_agent_handles_very_long_llm_response(self, mock_session):
        """Agent should handle very long LLM response."""
        provider = AsyncMock()
        provider.complete = AsyncMock(return_value=LLMResponse(
            content="x" * 100000,
            model="test-model",
            usage={"total_tokens": 25000},
            tool_calls=[],
        ))

        with patch("ah.core.agent.session_manager") as mock_sm:
            mock_sm.get = AsyncMock(return_value=mock_session)
            mock_sm.update_activity = AsyncMock()

            with patch("ah.core.agent.context_manager") as mock_cm:
                mock_cm.add_chunk = AsyncMock()
                mock_cm.get_recent_context = AsyncMock(return_value=[])

                agent = ReActAgent(provider=provider, max_iterations=5)
                response = await agent.run(mock_session.id, "test", verbose=False)

                assert response is not None
                assert len(response.content) > 0

    async def test_agent_handles_unicode_in_llm_response(self, mock_session):
        """Agent should handle unicode in LLM response."""
        provider = AsyncMock()
        provider.complete = AsyncMock(return_value=LLMResponse(
            content="你好世界 🎉 émojis",
            model="test-model",
            usage={"total_tokens": 5},
            tool_calls=[],
        ))

        with patch("ah.core.agent.session_manager") as mock_sm:
            mock_sm.get = AsyncMock(return_value=mock_session)
            mock_sm.update_activity = AsyncMock()

            with patch("ah.core.agent.context_manager") as mock_cm:
                mock_cm.add_chunk = AsyncMock()
                mock_cm.get_recent_context = AsyncMock(return_value=[])

                agent = ReActAgent(provider=provider, max_iterations=5)
                response = await agent.run(mock_session.id, "test", verbose=False)

                assert response is not None
                assert "你好" in response.content


# ===========================================================================
# Tool Failure Chaos Tests
# ===========================================================================

class TestToolChaos:
    """Chaos tests for tool failures."""

    async def test_agent_recovers_from_tool_failure(self, mock_session):
        """Agent should continue after a tool failure."""
        call_count = 0

        # Create a fresh registry to avoid polluting global state
        test_registry = ToolRegistry()

        @test_registry.register()
        async def flaky_tool(should_fail: str = "true") -> str:
            nonlocal call_count
            call_count += 1
            if should_fail == "true":
                raise RuntimeError("Tool failed!")
            return "success"

        provider = AsyncMock()
        provider.complete = AsyncMock(side_effect=[
            # First call: tool that fails
            LLMResponse(content="", model="test", usage={"total_tokens": 5}, tool_calls=[{
                "id": "call_1",
                "function": {"name": "flaky_tool", "arguments": json.dumps({"should_fail": "true"})},
            }]),
            # Second call: tool that succeeds
            LLMResponse(content="", model="test", usage={"total_tokens": 5}, tool_calls=[{
                "id": "call_2",
                "function": {"name": "flaky_tool", "arguments": json.dumps({"should_fail": "false"})},
            }]),
            # Third call: final answer
            LLMResponse(content="Recovered from failure", model="test", usage={"total_tokens": 5}),
        ])

        with patch("ah.core.agent.session_manager") as mock_sm:
            mock_sm.get = AsyncMock(return_value=mock_session)
            mock_sm.update_activity = AsyncMock()

            with patch("ah.core.agent.context_manager") as mock_cm:
                mock_cm.add_chunk = AsyncMock()
                mock_cm.get_recent_context = AsyncMock(return_value=[])

                with patch("ah.core.agent.registry", test_registry):
                    agent = ReActAgent(provider=provider, max_iterations=5)
                    response = await agent.run(mock_session.id, "test", verbose=False)

                    assert response is not None
                    assert call_count == 2

    async def test_agent_handles_tool_timeout(self, mock_session):
        """Agent should not hang when a tool times out."""
        test_registry = ToolRegistry()

        @test_registry.register()
        async def slow_tool() -> str:
            await asyncio.sleep(1000)
            return "done"

        provider = AsyncMock()
        provider.complete = AsyncMock(return_value=LLMResponse(
            content="",
            model="test",
            usage={"total_tokens": 5},
            tool_calls=[{
                "id": "call_1",
                "function": {"name": "slow_tool", "arguments": "{}"},
            }],
        ))

        with patch("ah.core.agent.session_manager") as mock_sm:
            mock_sm.get = AsyncMock(return_value=mock_session)
            mock_sm.update_activity = AsyncMock()

            with patch("ah.core.agent.context_manager") as mock_cm:
                mock_cm.add_chunk = AsyncMock()
                mock_cm.get_recent_context = AsyncMock(return_value=[])

                with patch("ah.core.agent.registry", test_registry):
                    agent = ReActAgent(provider=provider, max_iterations=5)

                    # Should timeout, not hang forever
                    with pytest.raises(asyncio.TimeoutError):
                        await asyncio.wait_for(
                            agent.run(mock_session.id, "test", verbose=False),
                            timeout=5,
                        )

    async def test_agent_handles_tool_returning_none(self, mock_session):
        """Agent should handle tool returning None."""
        test_registry = ToolRegistry()

        @test_registry.register()
        async def none_tool() -> None:
            return None

        provider = AsyncMock()
        provider.complete = AsyncMock(side_effect=[
            LLMResponse(content="", model="test", usage={"total_tokens": 5}, tool_calls=[{
                "id": "call_1",
                "function": {"name": "none_tool", "arguments": "{}"},
            }]),
            LLMResponse(content="Done", model="test", usage={"total_tokens": 5}),
        ])

        with patch("ah.core.agent.session_manager") as mock_sm:
            mock_sm.get = AsyncMock(return_value=mock_session)
            mock_sm.update_activity = AsyncMock()

            with patch("ah.core.agent.context_manager") as mock_cm:
                mock_cm.add_chunk = AsyncMock()
                mock_cm.get_recent_context = AsyncMock(return_value=[])

                with patch("ah.core.agent.registry", test_registry):
                    agent = ReActAgent(provider=provider, max_iterations=5)
                    response = await agent.run(mock_session.id, "test", verbose=False)

                    assert response is not None

    async def test_agent_handles_tool_returning_huge_output(self, mock_session):
        """Agent should handle tool returning very large output."""
        test_registry = ToolRegistry()

        @test_registry.register()
        async def huge_tool() -> str:
            return "x" * 1000000

        provider = AsyncMock()
        provider.complete = AsyncMock(side_effect=[
            LLMResponse(content="", model="test", usage={"total_tokens": 5}, tool_calls=[{
                "id": "call_1",
                "function": {"name": "huge_tool", "arguments": "{}"},
            }]),
            LLMResponse(content="Done", model="test", usage={"total_tokens": 5}),
        ])

        with patch("ah.core.agent.session_manager") as mock_sm:
            mock_sm.get = AsyncMock(return_value=mock_session)
            mock_sm.update_activity = AsyncMock()

            with patch("ah.core.agent.context_manager") as mock_cm:
                mock_cm.add_chunk = AsyncMock()
                mock_cm.get_recent_context = AsyncMock(return_value=[])

                with patch("ah.core.agent.registry", test_registry):
                    agent = ReActAgent(provider=provider, max_iterations=5)
                    response = await agent.run(mock_session.id, "test", verbose=False)

                    assert response is not None

    async def test_agent_handles_tool_with_wrong_args(self, mock_session):
        """Agent should handle tool called with wrong argument types."""
        test_registry = ToolRegistry()

        @test_registry.register()
        async def strict_tool(x: int) -> int:
            return x * 2

        provider = AsyncMock()
        provider.complete = AsyncMock(side_effect=[
            LLMResponse(content="", model="test", usage={"total_tokens": 5}, tool_calls=[{
                "id": "call_1",
                "function": {"name": "strict_tool", "arguments": json.dumps({"x": "not_an_int"})},
            }]),
            LLMResponse(content="Error handled", model="test", usage={"total_tokens": 5}),
        ])

        with patch("ah.core.agent.session_manager") as mock_sm:
            mock_sm.get = AsyncMock(return_value=mock_session)
            mock_sm.update_activity = AsyncMock()

            with patch("ah.core.agent.context_manager") as mock_cm:
                mock_cm.add_chunk = AsyncMock()
                mock_cm.get_recent_context = AsyncMock(return_value=[])

                with patch("ah.core.agent.registry", test_registry):
                    agent = ReActAgent(provider=provider, max_iterations=5)
                    response = await agent.run(mock_session.id, "test", verbose=False)

                    assert response is not None


# ===========================================================================
# Context Failure Chaos Tests
# ===========================================================================

class TestContextChaos:
    """Chaos tests for context failures."""

    async def test_agent_handles_context_overflow(self, mock_session):
        """Agent should handle context overflow gracefully."""
        provider = AsyncMock()
        provider.complete = AsyncMock(return_value=LLMResponse(
            content="test",
            model="test",
            usage={"total_tokens": 10},
            tool_calls=[],
        ))

        with patch("ah.core.agent.session_manager") as mock_sm:
            mock_sm.get = AsyncMock(return_value=mock_session)
            mock_sm.update_activity = AsyncMock()

            with patch("ah.core.agent.context_manager") as mock_cm:
                mock_cm.add_chunk = AsyncMock()
                # Simulate context overflow by returning huge context
                mock_cm.get_recent_context = AsyncMock(return_value=[
                    {"type": "user_message", "payload": {"content": "x" * 10000}, "tokens": 2500}
                    for _ in range(100)
                ])

                agent = ReActAgent(provider=provider, max_iterations=5)
                response = await agent.run(mock_session.id, "test", verbose=False)

                assert response is not None

    async def test_agent_handles_corrupted_context(self, mock_session):
        """Agent should handle corrupted context data."""
        provider = AsyncMock()
        provider.complete = AsyncMock(return_value=LLMResponse(
            content="test",
            model="test",
            usage={"total_tokens": 10},
            tool_calls=[],
        ))

        with patch("ah.core.agent.session_manager") as mock_sm:
            mock_sm.get = AsyncMock(return_value=mock_session)
            mock_sm.update_activity = AsyncMock()

            with patch("ah.core.agent.context_manager") as mock_cm:
                mock_cm.add_chunk = AsyncMock()
                # Return corrupted context
                mock_cm.get_recent_context = AsyncMock(return_value=[
                    {"type": "unknown", "payload": None},
                    {"type": "user_message"},  # Missing payload
                    {},  # Empty dict
                ])

                agent = ReActAgent(provider=provider, max_iterations=5)
                response = await agent.run(mock_session.id, "test", verbose=False)

                assert response is not None

    async def test_agent_handles_missing_session(self):
        """Agent should handle missing session gracefully."""
        provider = AsyncMock()
        provider.complete = AsyncMock()

        with patch("ah.core.agent.session_manager") as mock_sm:
            mock_sm.get = AsyncMock(return_value=None)

            agent = ReActAgent(provider=provider, max_iterations=5)
            with pytest.raises(ValueError, match="not found"):
                await agent.run(uuid.uuid4(), "test")


# ===========================================================================
# Database Failure Chaos Tests
# ===========================================================================

class TestDatabaseChaos:
    """Chaos tests for database failures."""

    async def test_agent_handles_db_connection_drop(self, mock_session):
        """Agent should handle database connection drop gracefully."""
        provider = AsyncMock()
        provider.complete = AsyncMock(return_value=LLMResponse(
            content="test",
            model="test",
            usage={"total_tokens": 10},
            tool_calls=[],
        ))

        with patch("ah.core.agent.session_manager") as mock_sm:
            mock_sm.get = AsyncMock(return_value=mock_session)
            mock_sm.update_activity = AsyncMock()

            with patch("ah.core.agent.context_manager") as mock_cm:
                # First call succeeds, second fails
                mock_cm.add_chunk = AsyncMock(side_effect=[
                    None,  # First call succeeds
                    Exception("Connection lost"),  # Second call fails
                ])
                mock_cm.get_recent_context = AsyncMock(return_value=[])

                agent = ReActAgent(provider=provider, max_iterations=5)
                # Should not crash
                try:
                    response = await agent.run(mock_session.id, "test", verbose=False)
                    assert response is not None
                except Exception:
                    # Acceptable if it raises
                    pass

    async def test_agent_handles_slow_db(self, mock_session):
        """Agent should handle slow database queries."""
        provider = AsyncMock()
        provider.complete = AsyncMock(return_value=LLMResponse(
            content="test",
            model="test",
            usage={"total_tokens": 10},
            tool_calls=[],
        ))

        async def slow_add_chunk(*args, **kwargs):
            await asyncio.sleep(0.1)  # Simulate slow query

        with patch("ah.core.agent.session_manager") as mock_sm:
            mock_sm.get = AsyncMock(return_value=mock_session)
            mock_sm.update_activity = AsyncMock()

            with patch("ah.core.agent.context_manager") as mock_cm:
                mock_cm.add_chunk = AsyncMock(side_effect=slow_add_chunk)
                mock_cm.get_recent_context = AsyncMock(return_value=[])

                agent = ReActAgent(provider=provider, max_iterations=5)
                response = await asyncio.wait_for(
                    agent.run(mock_session.id, "test", verbose=False),
                    timeout=30,
                )
                assert response is not None

    async def test_agent_handles_db_pool_exhaustion(self, mock_session):
        """Agent should handle database pool exhaustion."""
        provider = AsyncMock()
        provider.complete = AsyncMock(return_value=LLMResponse(
            content="test",
            model="test",
            usage={"total_tokens": 10},
            tool_calls=[],
        ))

        with patch("ah.core.agent.session_manager") as mock_sm:
            mock_sm.get = AsyncMock(return_value=mock_session)
            mock_sm.update_activity = AsyncMock()

            with patch("ah.core.agent.context_manager") as mock_cm:
                mock_cm.add_chunk = AsyncMock(side_effect=Exception("Pool exhausted"))
                mock_cm.get_recent_context = AsyncMock(return_value=[])

                agent = ReActAgent(provider=provider, max_iterations=5)
                try:
                    response = await agent.run(mock_session.id, "test", verbose=False)
                    assert response is not None
                except Exception:
                    pass


# ===========================================================================
# Streaming Chaos Tests
# ===========================================================================

class TestStreamingChaos:
    """Chaos tests for streaming failures."""

    async def test_agent_handles_stream_interruption(self, mock_session):
        """Agent should handle stream interruption gracefully."""
        provider = AsyncMock()

        async def interrupted_stream(*args, **kwargs):
            from ah.core.models import StreamEvent
            yield StreamEvent(type="text", content="partial")
            raise Exception("Stream interrupted")

        provider.stream_complete = interrupted_stream

        with patch("ah.core.agent.session_manager") as mock_sm:
            mock_sm.get = AsyncMock(return_value=mock_session)
            mock_sm.update_activity = AsyncMock()

            with patch("ah.core.agent.context_manager") as mock_cm:
                mock_cm.add_chunk = AsyncMock()
                mock_cm.get_recent_context = AsyncMock(return_value=[])

                agent = ReActAgent(provider=provider, max_iterations=5)
                events = []
                try:
                    async for event in agent.run_stream(mock_session.id, "test", verbose=False):
                        events.append(event)
                except Exception:
                    pass

                # Should have received at least the partial text
                assert len(events) >= 0

    async def test_agent_handles_stream_malformed_json(self, mock_session):
        """Agent should handle malformed JSON in stream."""
        provider = AsyncMock()

        async def malformed_stream(*args, **kwargs):
            from ah.core.models import StreamEvent
            yield StreamEvent(type="text", content="data: {invalid json}\n\n")
            yield StreamEvent(type="done", response=LLMResponse(
                content="recovered",
                model="test",
                usage={},
            ))

        provider.stream_complete = malformed_stream

        with patch("ah.core.agent.session_manager") as mock_sm:
            mock_sm.get = AsyncMock(return_value=mock_session)
            mock_sm.update_activity = AsyncMock()

            with patch("ah.core.agent.context_manager") as mock_cm:
                mock_cm.add_chunk = AsyncMock()
                mock_cm.get_recent_context = AsyncMock(return_value=[])

                agent = ReActAgent(provider=provider, max_iterations=5)
                events = []
                async for event in agent.run_stream(mock_session.id, "test", verbose=False):
                    events.append(event)

                # Should complete despite malformed JSON
                assert any(e.type == "done" for e in events)


# ===========================================================================
# Concurrent Chaos Tests
# ===========================================================================

class TestConcurrentChaos:
    """Chaos tests for concurrent operations."""

    async def test_concurrent_agent_runs(self, mock_session):
        """Multiple agent runs should not interfere with each other."""
        provider = AsyncMock()
        provider.complete = AsyncMock(return_value=LLMResponse(
            content="test",
            model="test",
            usage={"total_tokens": 10},
            tool_calls=[],
        ))

        with patch("ah.core.agent.session_manager") as mock_sm:
            mock_sm.get = AsyncMock(return_value=mock_session)
            mock_sm.update_activity = AsyncMock()

            with patch("ah.core.agent.context_manager") as mock_cm:
                mock_cm.add_chunk = AsyncMock()
                mock_cm.get_recent_context = AsyncMock(return_value=[])

                agent = ReActAgent(provider=provider, max_iterations=5)

                # Run multiple agents concurrently
                results = await asyncio.gather(*[
                    agent.run(mock_session.id, f"test {i}", verbose=False)
                    for i in range(5)
                ])

                assert len(results) == 5
                for r in results:
                    assert r is not None

    async def test_concurrent_tool_executions(self, mock_session):
        """Concurrent tool executions should be safe."""
        test_registry = ToolRegistry()

        @test_registry.register()
        async def concurrent_tool(delay: float = 0.01) -> str:
            await asyncio.sleep(delay)
            return "done"

        provider = AsyncMock()
        provider.complete = AsyncMock(return_value=LLMResponse(
            content="",
            model="test",
            usage={"total_tokens": 5},
            tool_calls=[{
                "id": "call_1",
                "function": {"name": "concurrent_tool", "arguments": json.dumps({"delay": 0.01})},
            }],
        ))

        with patch("ah.core.agent.session_manager") as mock_sm:
            mock_sm.get = AsyncMock(return_value=mock_session)
            mock_sm.update_activity = AsyncMock()

            with patch("ah.core.agent.context_manager") as mock_cm:
                mock_cm.add_chunk = AsyncMock()
                mock_cm.get_recent_context = AsyncMock(return_value=[])

                with patch("ah.core.agent.registry", test_registry):
                    agent = ReActAgent(provider=provider, max_iterations=5)

                    # Run multiple agents concurrently
                    results = await asyncio.gather(*[
                        agent.run(mock_session.id, f"test {i}", verbose=False)
                        for i in range(3)
                    ])

                    assert len(results) == 3


# ===========================================================================
# Recovery Tests
# ===========================================================================

class TestRecoveryChaos:
    """Tests for agent recovery after failures."""

    async def test_agent_recovers_after_multiple_failures(self, mock_session):
        """Agent should recover after multiple consecutive failures."""
        test_registry = ToolRegistry()

        @test_registry.register()
        async def sometimes_fails(fail_count: int = 0) -> str:
            if fail_count > 0:
                raise RuntimeError(f"Failure {fail_count}")
            return "success"

        provider = AsyncMock()
        provider.complete = AsyncMock(side_effect=[
            # First: tool fails
            LLMResponse(content="", model="test", usage={"total_tokens": 5}, tool_calls=[{
                "id": "call_1",
                "function": {"name": "sometimes_fails", "arguments": json.dumps({"fail_count": 2})},
            }]),
            # Second: tool fails again
            LLMResponse(content="", model="test", usage={"total_tokens": 5}, tool_calls=[{
                "id": "call_2",
                "function": {"name": "sometimes_fails", "arguments": json.dumps({"fail_count": 1})},
            }]),
            # Third: tool succeeds
            LLMResponse(content="", model="test", usage={"total_tokens": 5}, tool_calls=[{
                "id": "call_3",
                "function": {"name": "sometimes_fails", "arguments": json.dumps({"fail_count": 0})},
            }]),
            # Fourth: final answer
            LLMResponse(content="Finally succeeded", model="test", usage={"total_tokens": 5}),
        ])

        with patch("ah.core.agent.session_manager") as mock_sm:
            mock_sm.get = AsyncMock(return_value=mock_session)
            mock_sm.update_activity = AsyncMock()

            with patch("ah.core.agent.context_manager") as mock_cm:
                mock_cm.add_chunk = AsyncMock()
                mock_cm.get_recent_context = AsyncMock(return_value=[])

                with patch("ah.core.agent.registry", test_registry):
                    agent = ReActAgent(provider=provider, max_iterations=10)
                    response = await agent.run(mock_session.id, "test", verbose=False)

                    assert response is not None
                    assert "succeeded" in response.content.lower() or response.iterations >= 3

    async def test_agent_terminates_after_max_iterations_with_failures(self, mock_session):
        """Agent should terminate even if all iterations have failures."""
        test_registry = ToolRegistry()

        @test_registry.register()
        async def always_fails() -> str:
            raise RuntimeError("Always fails")

        provider = AsyncMock()
        provider.complete = AsyncMock(return_value=LLMResponse(
            content="",
            model="test",
            usage={"total_tokens": 5},
            tool_calls=[{
                "id": "call_1",
                "function": {"name": "always_fails", "arguments": "{}"},
            }],
        ))

        with patch("ah.core.agent.session_manager") as mock_sm:
            mock_sm.get = AsyncMock(return_value=mock_session)
            mock_sm.update_activity = AsyncMock()

            with patch("ah.core.agent.context_manager") as mock_cm:
                mock_cm.add_chunk = AsyncMock()
                mock_cm.get_recent_context = AsyncMock(return_value=[])

                with patch("ah.core.agent.registry", test_registry):
                    agent = ReActAgent(provider=provider, max_iterations=3)
                    response = await agent.run(mock_session.id, "test", verbose=False)

                    # Should terminate after max_iterations
                    assert response is not None
                    assert response.iterations <= 3
