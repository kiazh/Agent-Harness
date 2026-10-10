"""Integration tests for AgentHarness using FakeProvider + real tools + real DB.

These tests verify component interactions with real dependencies where feasible:
- Agent loop + real tools (mocked LLM)
- Agent loop + real database (mocked LLM)
- Provider + HTTP mocking
- Session + Context + Agent full flow
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ah.core.agent import ReActAgent
from ah.core.context import context_manager
from ah.core.models import LLMResponse, Session, StreamEvent
from ah.core.provider import LLMProvider
from ah.core.session import session_manager
from ah.tools import (
    builtins,  # noqa: F401
    file,  # noqa: F401
    terminal,  # noqa: F401
)
from ah.tools.base import ToolRegistry

# ===========================================================================
# FakeProvider — Deterministic provider for testing
# ===========================================================================


class FakeProvider(LLMProvider):
    """Deterministic provider for testing — no API calls.

    Returns predetermined responses based on call count.
    Records all calls for verification.
    """

    def __init__(self, responses: list[LLMResponse]):
        self.responses = responses
        self.call_count = 0
        self.calls: list[dict] = []

    async def complete(self, messages, **kwargs) -> LLMResponse:
        self.calls.append({"messages": messages, "kwargs": kwargs})
        if self.call_count >= len(self.responses):
            raise RuntimeError("FakeProvider ran out of responses")
        response = self.responses[self.call_count]
        self.call_count += 1
        return response

    async def stream_complete(self, messages, **kwargs):
        response = await self.complete(messages, **kwargs)
        yield StreamEvent(type="text", content=response.content)
        yield StreamEvent(type="done", response=response)

    async def embed(self, text: str) -> list[float]:
        return [0.1] * 1536  # Dummy embedding


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture
def temp_dir():
    """Create a temporary directory for file tests."""
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def mock_session():
    """Create a mock session."""
    return Session(
        id=uuid.uuid4(),
        title="Test Session",
        context_budget=8000,
    )


@pytest.fixture
def simple_fake_provider():
    """Create a FakeProvider with a simple text response."""
    return FakeProvider(
        [
            LLMResponse(content="Test response", model="fake", usage={"total_tokens": 10}),
        ]
    )


@pytest.fixture
def tool_call_fake_provider():
    """Create a FakeProvider that returns a tool call then final answer."""
    return FakeProvider(
        [
            LLMResponse(
                content="",
                model="fake",
                usage={"total_tokens": 5},
                tool_calls=[
                    {
                        "id": "call_1",
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps({"path": "/tmp/test"}),
                        },
                    }
                ],
            ),
            LLMResponse(content="File content", model="fake", usage={"total_tokens": 5}),
        ]
    )


# ===========================================================================
# Agent + Real Tools Integration Tests
# ===========================================================================


class TestAgentWithRealTools:
    """Integration tests for agent loop with real tools."""

    async def test_agent_executes_real_file_read(self, mock_session, temp_dir):
        """Test that agent actually executes read_file tool."""
        # Create a real temp file
        test_file = temp_dir / "test.txt"
        test_file.write_text("secret content for testing", encoding="utf-8")

        provider = FakeProvider(
            [
                LLMResponse(
                    content="",
                    model="fake",
                    usage={"total_tokens": 5},
                    tool_calls=[
                        {
                            "id": "call_1",
                            "function": {
                                "name": "read_file",
                                "arguments": json.dumps({"path": str(test_file)}),
                            },
                        }
                    ],
                ),
                LLMResponse(
                    content="The file contains: secret content for testing",
                    model="fake",
                    usage={"total_tokens": 5},
                ),
            ]
        )

        with patch("ah.core.agent.session_manager") as mock_sm:
            mock_sm.get = AsyncMock(return_value=mock_session)
            mock_sm.update_activity = AsyncMock()

            with patch("ah.core.agent.context_manager") as mock_cm:
                mock_cm.add_chunk = AsyncMock()
                mock_cm.add_chunks_batch = AsyncMock()
                mock_cm.get_recent_context = AsyncMock(return_value=[])

                agent = ReActAgent(provider=provider, max_iterations=5)
                response = await agent.run(
                    mock_session.id,
                    f"Read the file {test_file}",
                    verbose=False,
                )

                assert response is not None
                assert len(response.tool_calls) == 1
                assert response.tool_calls[0]["tool"] == "read_file"

    async def test_agent_executes_real_file_write(self, mock_session, temp_dir, monkeypatch):
        """Test that agent actually executes write_file tool."""
        test_file = temp_dir / "output.txt"
        from ah.core.config import config

        monkeypatch.setattr(config, "execution_mode", "workspace")
        monkeypatch.setattr(config, "workspace_root", str(temp_dir))

        provider = FakeProvider(
            [
                LLMResponse(
                    content="",
                    model="fake",
                    usage={"total_tokens": 5},
                    tool_calls=[
                        {
                            "id": "call_1",
                            "function": {
                                "name": "write_file",
                                "arguments": json.dumps(
                                    {
                                        "path": str(test_file),
                                        "content": "written by agent",
                                    }
                                ),
                            },
                        }
                    ],
                ),
                LLMResponse(
                    content="File written successfully", model="fake", usage={"total_tokens": 5}
                ),
            ]
        )

        with patch("ah.core.agent.session_manager") as mock_sm:
            mock_sm.get = AsyncMock(return_value=mock_session)
            mock_sm.update_activity = AsyncMock()

            with patch("ah.core.agent.context_manager") as mock_cm:
                mock_cm.add_chunk = AsyncMock()
                mock_cm.add_chunks_batch = AsyncMock()
                mock_cm.get_recent_context = AsyncMock(return_value=[])

                # Patch _BASE_DIR to allow writing to temp_dir
                with patch("ah.tools.file._BASE_DIR", temp_dir):
                    agent = ReActAgent(provider=provider, max_iterations=5)
                    response = await agent.run(
                        mock_session.id,
                        f"Write 'written by agent' to {test_file}",
                        verbose=False,
                    )

                    assert response is not None
                    assert not response.needs_approval
                    # Verify the file was actually written
                    assert test_file.exists()
                    assert test_file.read_text() == "written by agent"

    async def test_agent_executes_real_list_files(self, mock_session, temp_dir):
        """Test that agent actually executes list_files tool."""
        # Create some test files
        (temp_dir / "file1.txt").write_text("content1")
        (temp_dir / "file2.txt").write_text("content2")

        provider = FakeProvider(
            [
                LLMResponse(
                    content="",
                    model="fake",
                    usage={"total_tokens": 5},
                    tool_calls=[
                        {
                            "id": "call_1",
                            "function": {
                                "name": "list_files",
                                "arguments": json.dumps({"path": str(temp_dir)}),
                            },
                        }
                    ],
                ),
                LLMResponse(content="Found 2 files", model="fake", usage={"total_tokens": 5}),
            ]
        )

        with patch("ah.core.agent.session_manager") as mock_sm:
            mock_sm.get = AsyncMock(return_value=mock_session)
            mock_sm.update_activity = AsyncMock()

            with patch("ah.core.agent.context_manager") as mock_cm:
                mock_cm.add_chunk = AsyncMock()
                mock_cm.add_chunks_batch = AsyncMock()
                mock_cm.get_recent_context = AsyncMock(return_value=[])

                agent = ReActAgent(provider=provider, max_iterations=5)
                response = await agent.run(
                    mock_session.id,
                    f"List files in {temp_dir}",
                    verbose=False,
                )

                assert response is not None
                assert len(response.tool_calls) == 1

    async def test_agent_executes_real_terminal_command(self, mock_session):
        """Test that agent actually executes terminal tool."""
        provider = FakeProvider(
            [
                LLMResponse(
                    content="",
                    model="fake",
                    usage={"total_tokens": 5},
                    tool_calls=[
                        {
                            "id": "call_1",
                            "function": {
                                "name": "terminal",
                                "arguments": json.dumps({"command": "echo hello"}),
                            },
                        }
                    ],
                ),
                LLMResponse(content="Command executed", model="fake", usage={"total_tokens": 5}),
            ]
        )

        with patch("ah.core.agent.session_manager") as mock_sm:
            mock_sm.get = AsyncMock(return_value=mock_session)
            mock_sm.update_activity = AsyncMock()

            with patch("ah.core.agent.context_manager") as mock_cm:
                mock_cm.add_chunk = AsyncMock()
                mock_cm.add_chunks_batch = AsyncMock()
                mock_cm.get_recent_context = AsyncMock(return_value=[])

                agent = ReActAgent(provider=provider, max_iterations=5)
                response = await agent.run(
                    mock_session.id,
                    "Run echo hello",
                    verbose=False,
                )

                assert response is not None
                assert len(response.tool_calls) == 1
                assert response.tool_calls[0]["tool"] == "terminal"

    async def test_agent_multiple_tool_calls(self, mock_session, temp_dir):
        """Test agent executing multiple tools in sequence."""
        test_file = temp_dir / "multi.txt"

        provider = FakeProvider(
            [
                # First: write file
                LLMResponse(
                    content="",
                    model="fake",
                    usage={"total_tokens": 5},
                    tool_calls=[
                        {
                            "id": "call_1",
                            "function": {
                                "name": "write_file",
                                "arguments": json.dumps(
                                    {
                                        "path": str(test_file),
                                        "content": "test content",
                                    }
                                ),
                            },
                        }
                    ],
                ),
                # Second: read file
                LLMResponse(
                    content="",
                    model="fake",
                    usage={"total_tokens": 5},
                    tool_calls=[
                        {
                            "id": "call_2",
                            "function": {
                                "name": "read_file",
                                "arguments": json.dumps({"path": str(test_file)}),
                            },
                        }
                    ],
                ),
                # Third: final answer
                LLMResponse(content="Done", model="fake", usage={"total_tokens": 5}),
            ]
        )

        with patch("ah.core.agent.session_manager") as mock_sm:
            mock_sm.get = AsyncMock(return_value=mock_session)
            mock_sm.update_activity = AsyncMock()

            with patch("ah.core.agent.context_manager") as mock_cm:
                mock_cm.add_chunk = AsyncMock()
                mock_cm.add_chunks_batch = AsyncMock()
                mock_cm.get_recent_context = AsyncMock(return_value=[])

                agent = ReActAgent(provider=provider, max_iterations=10)
                response = await agent.run(
                    mock_session.id,
                    f"Write then read {test_file}",
                    verbose=False,
                )

                assert response is not None
                assert len(response.tool_calls) == 2
                assert response.tool_calls[0]["tool"] == "write_file"
                assert response.tool_calls[1]["tool"] == "read_file"


# ===========================================================================
# Agent + Mocked DB Integration Tests
# ===========================================================================


class TestAgentWithMockedDB:
    """Integration tests for agent with mocked database."""

    async def test_agent_stores_context_chunks(self, mock_session):
        """Test that agent stores context chunks during execution."""
        provider = FakeProvider(
            [
                LLMResponse(content="Test response", model="fake", usage={"total_tokens": 10}),
            ]
        )

        with patch("ah.core.agent.session_manager") as mock_sm:
            mock_sm.get = AsyncMock(return_value=mock_session)
            mock_sm.update_activity = AsyncMock()

            with patch("ah.core.agent.context_manager") as mock_cm:
                mock_cm.add_chunk = AsyncMock()
                mock_cm.add_chunks_batch = AsyncMock()
                mock_cm.get_recent_context = AsyncMock(return_value=[])

                agent = ReActAgent(provider=provider, max_iterations=5)
                await agent.run(mock_session.id, "test message", verbose=False)

                # Should have stored user message and assistant response
                assert mock_cm.add_chunks_batch.call_count >= 1

    async def test_agent_retrieves_recent_context(self, mock_session):
        """Test that agent retrieves recent context for prompt assembly."""
        provider = FakeProvider(
            [
                LLMResponse(content="Test response", model="fake", usage={"total_tokens": 10}),
            ]
        )

        with patch("ah.core.agent.session_manager") as mock_sm:
            mock_sm.get = AsyncMock(return_value=mock_session)
            mock_sm.update_activity = AsyncMock()

            with patch("ah.core.agent.context_manager") as mock_cm:
                mock_cm.add_chunk = AsyncMock()
                mock_cm.add_chunks_batch = AsyncMock()
                mock_cm.get_recent_context = AsyncMock(
                    return_value=[
                        {"type": "user_message", "payload": {"content": "previous"}, "tokens": 1},
                    ]
                )

                agent = ReActAgent(provider=provider, max_iterations=5)
                await agent.run(mock_session.id, "test message", verbose=False)

                mock_cm.get_recent_context.assert_called_once()

    async def test_agent_updates_session_activity(self, mock_session):
        """Test that agent updates session activity after completion."""
        provider = FakeProvider(
            [
                LLMResponse(content="Test response", model="fake", usage={"total_tokens": 10}),
            ]
        )

        with patch("ah.core.agent.session_manager") as mock_sm:
            mock_sm.get = AsyncMock(return_value=mock_session)
            mock_sm.update_activity = AsyncMock()

            with patch("ah.core.agent.context_manager") as mock_cm:
                mock_cm.add_chunk = AsyncMock()
                mock_cm.add_chunks_batch = AsyncMock()
                mock_cm.get_recent_context = AsyncMock(return_value=[])

                agent = ReActAgent(provider=provider, max_iterations=5)
                await agent.run(mock_session.id, "test message", verbose=False)

                mock_sm.update_activity.assert_called_once_with(mock_session.id)


# ===========================================================================
# Session + Context Integration Tests
# ===========================================================================


class TestSessionContextIntegration:
    """Integration tests for session and context managers."""

    async def test_session_crud_with_mock_db(self):
        """Test session CRUD operations with mocked database."""
        mock_db = AsyncMock()
        mock_db.fetchrow = AsyncMock(
            return_value={
                "id": uuid.uuid4(),
                "title": "Test Session",
                "agent_id": "harness",
                "status": "active",
                "state_msgpack": b"\x80",
                "goal": "Test goal",
                "model": "gpt-4",
                "provider": "openai",
                "context_budget": 8000,
                "created_at": datetime.utcnow(),
                "last_activity": datetime.utcnow(),
            }
        )
        mock_db.fetch = AsyncMock(
            return_value=[
                {
                    "id": uuid.uuid4(),
                    "title": "Test Session",
                    "agent_id": "harness",
                    "status": "active",
                    "state_msgpack": b"\x80",
                    "goal": "Test goal",
                    "model": "gpt-4",
                    "provider": "openai",
                    "context_budget": 8000,
                    "created_at": datetime.utcnow(),
                    "last_activity": datetime.utcnow(),
                }
            ]
        )
        mock_db.execute = AsyncMock(return_value="UPDATE 1")

        with patch("ah.core.session.db", mock_db):
            # Create
            session = await session_manager.create(title="Test Session")
            assert session.title == "Test Session"
            assert session.status == "active"

            # Get
            loaded = await session_manager.get(session.id)
            assert loaded is not None
            assert loaded.title == "Test Session"

            # Update state
            await session_manager.update_state(session.id, {"key": "value"})
            mock_db.execute.assert_called()

            # List
            sessions = await session_manager.list_sessions()
            assert len(sessions) == 1

            # Archive
            await session_manager.archive(session.id)
            assert mock_db.execute.call_count >= 2

    async def test_context_crud_with_mock_db(self):
        """Test context chunk CRUD operations with mocked database."""
        import msgpack

        mock_db = AsyncMock()
        session_id = uuid.uuid4()

        mock_db.fetchrow = AsyncMock(
            return_value={
                "id": uuid.uuid4(),
                "session_id": session_id,
                "agent_id": "harness",
                "chunk_type": "user_message",
                "payload_msgpack": msgpack.packb({"content": "hello"}, use_bin_type=True),
                "token_count": 5,
                "embedding": None,
                "created_at": datetime.utcnow(),
                "accessed_at": None,
            }
        )
        mock_db.fetch = AsyncMock(
            return_value=[
                {
                    "id": uuid.uuid4(),
                    "session_id": session_id,
                    "agent_id": "harness",
                    "chunk_type": "user_message",
                    "payload_msgpack": msgpack.packb({"content": "hello"}, use_bin_type=True),
                    "token_count": 5,
                    "embedding": None,
                    "created_at": datetime.utcnow(),
                    "accessed_at": None,
                }
            ]
        )
        mock_db.fetchval = AsyncMock(return_value=150)
        mock_db.execute = AsyncMock(return_value="DELETE 5")

        with patch("ah.core.context.db", mock_db):
            # Add chunk
            chunk = await context_manager.add_chunk(
                session_id=session_id,
                agent_id="harness",
                chunk_type="user_message",
                payload={"content": "hello"},
            )
            assert chunk.chunk_type == "user_message"
            assert chunk.payload == {"content": "hello"}

            # Get chunks
            chunks = await context_manager.get_chunks(session_id)
            assert len(chunks) == 1

            # Get recent context
            recent = await context_manager.get_recent_context(session_id)
            assert len(recent) == 1
            assert recent[0]["type"] == "user_message"

            # Get token usage
            tokens = await context_manager.get_token_usage(session_id)
            assert tokens == 150

            # Delete chunks
            count = await context_manager.delete_chunks(session_id)
            assert count == 5

    async def test_context_batch_insert(self):
        """Test batch insert of context chunks."""
        import msgpack

        mock_db = AsyncMock()
        session_id = uuid.uuid4()

        mock_db.fetch = AsyncMock(
            return_value=[
                {
                    "id": uuid.uuid4(),
                    "session_id": session_id,
                    "agent_id": "harness",
                    "chunk_type": "user_message",
                    "payload_msgpack": msgpack.packb({"content": f"msg {i}"}, use_bin_type=True),
                    "token_count": 5,
                    "embedding": None,
                    "created_at": datetime.utcnow(),
                    "accessed_at": None,
                }
                for i in range(5)
            ]
        )

        with patch("ah.core.context.db", mock_db):
            chunks = [
                {
                    "session_id": session_id,
                    "agent_id": "harness",
                    "chunk_type": "user_message",
                    "payload": {"content": f"msg {i}"},
                }
                for i in range(5)
            ]
            result = await context_manager.add_chunks_batch(chunks)
            assert len(result) == 5
            mock_db.fetch.assert_called_once()


# ===========================================================================
# Provider + HTTP Mocking Integration Tests
# ===========================================================================


class TestProviderHTTPIntegration:
    """Integration tests for providers with mocked HTTP."""

    async def test_openrouter_full_request_response(self):
        """Test OpenRouter provider with mocked HTTP transport."""
        from ah.core.provider import OpenRouterProvider

        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
            provider = OpenRouterProvider(api_key="test-key", model="test-model")

            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "choices": [
                    {
                        "message": {
                            "content": "Test response",
                            "tool_calls": [],
                        }
                    }
                ],
                "model": "test-model",
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
            }
            mock_response.raise_for_status = MagicMock()

            with patch.object(provider.client, "post", return_value=mock_response):
                response = await provider.complete(
                    messages=[{"role": "user", "content": "hello"}],
                )
                assert response.content == "Test response"
                assert response.usage["total_tokens"] == 15

            await provider.close()

    async def test_ollama_full_request_response(self):
        """Test Ollama provider with mocked HTTP transport."""
        from ah.core.provider import OllamaProvider

        provider = OllamaProvider(model="llama3.1", base_url="http://localhost:11434")

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "message": {
                "content": "Test response",
                "tool_calls": [],
            },
            "model": "llama3.1",
            "prompt_eval_count": 10,
            "eval_count": 5,
        }
        mock_response.raise_for_status = MagicMock()

        with patch.object(provider.client, "post", return_value=mock_response):
            response = await provider.complete(
                messages=[{"role": "user", "content": "hello"}],
            )
            assert response.content == "Test response"
            assert response.usage["total_tokens"] == 15

        await provider.close()

    async def test_openrouter_streaming(self):
        """Test OpenRouter streaming with mocked HTTP."""
        from ah.core.provider import OpenRouterProvider

        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
            provider = OpenRouterProvider(api_key="test-key", model="test-model")

            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.raise_for_status = MagicMock()

            async def mock_aiter_lines():
                yield 'data: {"choices": [{"delta": {"content": "Hello"}}]}'
                yield 'data: {"choices": [{"delta": {"content": " world"}}]}'
                yield "data: [DONE]"

            mock_response.aiter_lines = mock_aiter_lines

            with patch.object(provider.client, "stream") as mock_stream:
                mock_stream.return_value.__aenter__ = AsyncMock(return_value=mock_response)
                mock_stream.return_value.__aexit__ = AsyncMock(return_value=False)

                events = []
                async for event in provider.stream_complete(
                    messages=[{"role": "user", "content": "hello"}],
                ):
                    events.append(event)

                # Should have text events and done event
                assert any(e.type == "text" for e in events)
                assert any(e.type == "done" for e in events)

            await provider.close()


# ===========================================================================
# Full Flow Integration Tests
# ===========================================================================


class TestFullFlowIntegration:
    """Full flow integration tests: session → context → agent → response."""

    async def test_full_agent_flow_with_mocked_db(self, mock_session, temp_dir):
        """Test the complete agent flow with mocked database."""
        test_file = temp_dir / "flow_test.txt"
        test_file.write_text("integration test content", encoding="utf-8")

        provider = FakeProvider(
            [
                LLMResponse(
                    content="",
                    model="fake",
                    usage={"total_tokens": 5},
                    tool_calls=[
                        {
                            "id": "call_1",
                            "function": {
                                "name": "read_file",
                                "arguments": json.dumps({"path": str(test_file)}),
                            },
                        }
                    ],
                ),
                LLMResponse(
                    content="The file says: integration test content",
                    model="fake",
                    usage={"total_tokens": 5},
                ),
            ]
        )

        with patch("ah.core.agent.session_manager") as mock_sm:
            mock_sm.get = AsyncMock(return_value=mock_session)
            mock_sm.update_activity = AsyncMock()

            with patch("ah.core.agent.context_manager") as mock_cm:
                mock_cm.add_chunk = AsyncMock()
                mock_cm.add_chunks_batch = AsyncMock()
                mock_cm.get_recent_context = AsyncMock(return_value=[])

                agent = ReActAgent(provider=provider, max_iterations=5)
                response = await agent.run(
                    mock_session.id,
                    f"Read {test_file} and tell me what it says",
                    verbose=False,
                )

                # Verify the flow completed
                assert response is not None
                assert response.iterations == 2
                assert len(response.tool_calls) == 1
                assert response.tool_calls[0]["tool"] == "read_file"

                # Verify context was stored
                assert mock_cm.add_chunks_batch.call_count >= 1

    async def test_multi_turn_conversation(self, mock_session):
        """Test multi-turn conversation with context accumulation."""
        responses = [
            LLMResponse(content="First response", model="fake", usage={"total_tokens": 10}),
            LLMResponse(content="Second response", model="fake", usage={"total_tokens": 10}),
            LLMResponse(content="Third response", model="fake", usage={"total_tokens": 10}),
        ]
        provider = FakeProvider(responses)

        with patch("ah.core.agent.session_manager") as mock_sm:
            mock_sm.get = AsyncMock(return_value=mock_session)
            mock_sm.update_activity = AsyncMock()

            with patch("ah.core.agent.context_manager") as mock_cm:
                mock_cm.add_chunk = AsyncMock()
                mock_cm.add_chunks_batch = AsyncMock()
                mock_cm.get_recent_context = AsyncMock(return_value=[])

                agent = ReActAgent(provider=provider, max_iterations=5)

                # Turn 1
                r1 = await agent.run(mock_session.id, "First message", verbose=False)
                assert r1.content == "First response"

                # Turn 2
                r2 = await agent.run(mock_session.id, "Second message", verbose=False)
                assert r2.content == "Second response"

                # Turn 3
                r3 = await agent.run(mock_session.id, "Third message", verbose=False)
                assert r3.content == "Third response"

                # Verify all turns completed
                assert provider.call_count == 3

    async def test_agent_with_streaming(self, mock_session):
        """Test agent with streaming output."""
        provider = FakeProvider(
            [
                LLMResponse(content="Streamed response", model="fake", usage={"total_tokens": 10}),
            ]
        )

        with patch("ah.core.agent.session_manager") as mock_sm:
            mock_sm.get = AsyncMock(return_value=mock_session)
            mock_sm.update_activity = AsyncMock()

            with patch("ah.core.agent.context_manager") as mock_cm:
                mock_cm.add_chunk = AsyncMock()
                mock_cm.add_chunks_batch = AsyncMock()
                mock_cm.get_recent_context = AsyncMock(return_value=[])

                agent = ReActAgent(provider=provider, max_iterations=5)
                events = []
                async for event in agent.run_stream(mock_session.id, "test", verbose=False):
                    events.append(event)

                # Should have text and done events
                assert any(e.type == "text" for e in events)
                assert any(e.type == "done" for e in events)


# ===========================================================================
# Tool Registry Integration Tests
# ===========================================================================


class TestToolRegistryIntegration:
    """Integration tests for tool registry."""

    async def test_register_and_execute_multiple_tools(self):
        """Test registering and executing multiple tools."""
        reg = ToolRegistry()

        @reg.register(
            parameters={
                "type": "object",
                "properties": {
                    "a": {"type": "integer"},
                    "b": {"type": "integer"},
                },
                "required": ["a", "b"],
            }
        )
        async def add(a: int, b: int) -> int:
            return a + b

        @reg.register(
            parameters={
                "type": "object",
                "properties": {
                    "a": {"type": "integer"},
                    "b": {"type": "integer"},
                },
                "required": ["a", "b"],
            }
        )
        async def multiply(a: int, b: int) -> int:
            return a * b

        @reg.register()
        async def greet(username: str) -> str:
            return f"Hello, {username}!"

        assert await reg.execute("add", a=2, b=3) == 5
        assert await reg.execute("multiply", a=2, b=3) == 6
        assert await reg.execute("greet", username="World") == "Hello, World!"

    async def test_tool_validation_integration(self):
        """Test tool argument validation."""
        reg = ToolRegistry()

        @reg.register(
            parameters={
                "type": "object",
                "properties": {
                    "x": {"type": "integer"},
                    "y": {"type": "string"},
                },
                "required": ["x", "y"],
            }
        )
        async def strict_tool(x: int, y: str) -> str:
            return f"{y}: {x}"

        # Valid args
        result = await reg.execute("strict_tool", x=42, y="answer")
        assert result == "answer: 42"

        # Missing required arg
        with pytest.raises(ValueError):
            await reg.execute("strict_tool", x=42)

        # Wrong type
        with pytest.raises(ValueError):
            await reg.execute("strict_tool", x="not_int", y="answer")

    async def test_tool_definitions_for_llm(self):
        """Test that tool definitions are valid for LLM function calling."""
        reg = ToolRegistry()

        @reg.register(description="Add two numbers")
        async def add(a: int, b: int) -> int:
            return a + b

        defs = reg.get_tool_definitions()
        assert len(defs) == 1
        assert defs[0].name == "add"
        assert defs[0].description == "Add two numbers"
        assert defs[0].parameters["type"] == "object"
        assert "a" in defs[0].parameters["properties"]
        assert "b" in defs[0].parameters["properties"]


# ===========================================================================
# Container Integration Tests
# ===========================================================================


class TestContainerIntegration:
    """Integration tests for the dependency injection container."""

    async def test_container_testing_mode(self):
        """Test container in testing mode."""
        from ah.core.container import Container

        container = Container.testing()
        assert container._provider is None
        assert container._started is False

    async def test_container_production_mode(self):
        """Test container in production mode."""
        from ah.core.container import Container

        container = Container.production()
        assert container._db is not None
        assert container._session_manager is not None
        assert container._context_manager is not None


# ===========================================================================
# Real Database Tests (Skipped if PostgreSQL not available)
# ===========================================================================


class TestRealDatabase:
    """Integration tests with real PostgreSQL database.

    These tests are skipped if PostgreSQL is not available.
    """

    async def test_real_session_crud(self):
        """Test session CRUD with real database."""
        from ah.db.connection import db

        try:
            await db.connect()
        except Exception:
            pytest.skip("PostgreSQL not available")

        try:
            # Create
            session = await session_manager.create(title="Integration Test")
            assert session.id is not None
            assert session.status == "active"

            # Get
            loaded = await session_manager.get(session.id)
            assert loaded is not None
            assert loaded.title == "Integration Test"

            # Update
            await session_manager.update_state(session.id, {"test": "value"})
            loaded = await session_manager.get(session.id)
            assert loaded.state == {"test": "value"}

            # List
            sessions = await session_manager.list_sessions(limit=10)
            assert len(sessions) > 0

            # Archive
            await session_manager.archive(session.id)
            loaded = await session_manager.get(session.id)
            assert loaded.status == "archived"

        finally:
            await db.close()

    async def test_real_context_crud(self):
        """Test context chunk CRUD with real database."""
        from ah.db.connection import db

        try:
            await db.connect()
        except Exception:
            pytest.skip("PostgreSQL not available")

        try:
            session = await session_manager.create(title="Context Test")
            session_id = session.id

            # Add chunk
            chunk = await context_manager.add_chunk(
                session_id=session_id,
                agent_id="harness",
                chunk_type="user_message",
                payload={"content": "hello"},
                token_count=5,
            )
            assert chunk.id is not None
            assert chunk.chunk_type == "user_message"

            # Get chunks
            chunks = await context_manager.get_chunks(session_id)
            assert len(chunks) >= 1

            # Get recent context
            recent = await context_manager.get_recent_context(session_id)
            assert len(recent) >= 1

            # Get token usage
            tokens = await context_manager.get_token_usage(session_id)
            assert tokens >= 5

            # Delete chunks
            count = await context_manager.delete_chunks(session_id)
            assert count >= 1

        finally:
            await db.close()

    async def test_real_agent_flow(self):
        """Test full agent flow with real database."""
        from ah.db.connection import db

        try:
            await db.connect()
        except Exception:
            pytest.skip("PostgreSQL not available")

        try:
            # Create session
            session = await session_manager.create(title="Agent Flow Test")

            # Create provider
            provider = FakeProvider(
                [
                    LLMResponse(
                        content="Real DB response", model="fake", usage={"total_tokens": 10}
                    ),
                ]
            )

            # Run agent
            agent = ReActAgent(provider=provider, max_iterations=5)
            response = await agent.run(session.id, "test message", verbose=False)

            assert response is not None
            assert response.content == "Real DB response"

            # Verify context was stored
            chunks = await context_manager.get_chunks(session.id)
            assert len(chunks) >= 1

        finally:
            await db.close()
