"""Provider usage tracking: reserve/finish when session_id and agent_id are provided."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from ah.core.models import LLMResponse, StreamEvent


def _make_openrouter_provider():
    """Create an OpenRouterProvider with a mocked HTTP client."""
    from ah.core.provider import OpenRouterProvider

    provider = OpenRouterProvider.__new__(OpenRouterProvider)
    provider.api_key = "test-key"
    provider.model = "test-model"
    provider._rate_limiter = AsyncMock()
    provider.client = MagicMock()

    mock_response = MagicMock()
    mock_response.json.return_value = {
        "model": "test-model",
        "choices": [{"message": {"content": "OK", "tool_calls": []}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
    }
    mock_response.raise_for_status = MagicMock()
    provider.client.post = AsyncMock(return_value=mock_response)
    return provider


def _make_ollama_provider():
    """Create an OllamaProvider with a mocked HTTP client."""
    from ah.core.provider import OllamaProvider

    provider = OllamaProvider.__new__(OllamaProvider)
    provider.model = "test-model"
    provider.base_url = "http://localhost:11434"
    provider._rate_limiter = AsyncMock()
    provider.client = MagicMock()

    mock_response = MagicMock()
    mock_response.json.return_value = {
        "model": "test-model",
        "message": {"role": "assistant", "content": "OK", "tool_calls": []},
        "prompt_eval_count": 5,
        "eval_count": 3,
    }
    mock_response.raise_for_status = MagicMock()
    provider.client.post = AsyncMock(return_value=mock_response)
    return provider


@pytest.mark.asyncio
async def test_openrouter_complete_calls_usage_store_when_session_id_provided(monkeypatch):
    """OpenRouterProvider.complete() should call usage_store.reserve() and usage_store.finish() when session_id is provided."""
    provider = _make_openrouter_provider()

    reserve = AsyncMock(return_value=uuid.uuid4())
    finish = AsyncMock()
    monkeypatch.setattr("ah.core.provider.usage_store.reserve", reserve)
    monkeypatch.setattr("ah.core.provider.usage_store.finish", finish)

    session_id = uuid.uuid4()
    agent_id = "test-agent"

    result = await provider.complete(
        messages=[{"role": "user", "content": "hello"}],
        session_id=session_id,
        agent_id=agent_id,
    )

    reserve.assert_awaited_once()
    finish.assert_awaited_once()
    assert result.content == "OK"


@pytest.mark.asyncio
async def test_openrouter_complete_skips_usage_store_when_no_session_id(monkeypatch):
    """OpenRouterProvider.complete() should NOT call usage_store when session_id is not provided."""
    provider = _make_openrouter_provider()

    reserve = AsyncMock(return_value=uuid.uuid4())
    finish = AsyncMock()
    monkeypatch.setattr("ah.core.provider.usage_store.reserve", reserve)
    monkeypatch.setattr("ah.core.provider.usage_store.finish", finish)

    result = await provider.complete(
        messages=[{"role": "user", "content": "hello"}],
    )

    reserve.assert_not_awaited()
    finish.assert_not_awaited()
    assert result.content == "OK"


@pytest.mark.asyncio
async def test_ollama_complete_calls_usage_store_when_session_id_provided(monkeypatch):
    """OllamaProvider.complete() should call usage_store.reserve() and usage_store.finish() when session_id is provided."""
    provider = _make_ollama_provider()

    reserve = AsyncMock(return_value=uuid.uuid4())
    finish = AsyncMock()
    monkeypatch.setattr("ah.core.provider.usage_store.reserve", reserve)
    monkeypatch.setattr("ah.core.provider.usage_store.finish", finish)

    session_id = uuid.uuid4()
    agent_id = "test-agent"

    result = await provider.complete(
        messages=[{"role": "user", "content": "hello"}],
        session_id=session_id,
        agent_id=agent_id,
    )

    reserve.assert_awaited_once()
    finish.assert_awaited_once()
    assert result.content == "OK"


@pytest.mark.asyncio
async def test_ollama_complete_skips_usage_store_when_no_session_id(monkeypatch):
    """OllamaProvider.complete() should NOT call usage_store when session_id is not provided."""
    provider = _make_ollama_provider()

    reserve = AsyncMock(return_value=uuid.uuid4())
    finish = AsyncMock()
    monkeypatch.setattr("ah.core.provider.usage_store.reserve", reserve)
    monkeypatch.setattr("ah.core.provider.usage_store.finish", finish)

    result = await provider.complete(
        messages=[{"role": "user", "content": "hello"}],
    )

    reserve.assert_not_awaited()
    finish.assert_not_awaited()
    assert result.content == "OK"


@pytest.mark.asyncio
async def test_openrouter_complete_calls_finish_with_failed_on_error(monkeypatch):
    """OpenRouterProvider.complete() should call usage_store.finish(failed=True) on API error."""
    from ah.core.provider import OpenRouterProvider

    provider = OpenRouterProvider.__new__(OpenRouterProvider)
    provider.api_key = "test-key"
    provider.model = "test-model"
    provider._rate_limiter = AsyncMock()
    provider.client = MagicMock()
    provider.client.post = AsyncMock(side_effect=RuntimeError("API down"))

    reserve = AsyncMock(return_value=uuid.uuid4())
    finish = AsyncMock()
    monkeypatch.setattr("ah.core.provider.usage_store.reserve", reserve)
    monkeypatch.setattr("ah.core.provider.usage_store.finish", finish)

    session_id = uuid.uuid4()
    agent_id = "test-agent"

    with pytest.raises(RuntimeError, match="API down"):
        await provider.complete(
            messages=[{"role": "user", "content": "hello"}],
            session_id=session_id,
            agent_id=agent_id,
        )

    reserve.assert_awaited_once()
    finish.assert_awaited_once()
    # Verify finish was called with failed=True
    assert finish.call_args.kwargs.get("failed") is True


@pytest.mark.asyncio
async def test_openrouter_stream_complete_calls_usage_store_when_session_id_provided(monkeypatch):
    """OpenRouterProvider.stream_complete() should call usage_store.reserve() and usage_store.finish() when session_id is provided."""
    from ah.core.provider import OpenRouterProvider

    provider = OpenRouterProvider.__new__(OpenRouterProvider)
    provider.api_key = "test-key"
    provider.model = "test-model"
    provider._rate_limiter = AsyncMock()
    provider.client = MagicMock()

    # Mock the streaming response
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()

    async def mock_aiter_lines():
        yield 'data: {"model": "test-model", "choices": [{"delta": {"content": "Hello"}}]}'
        yield 'data: {"model": "test-model", "choices": [{"delta": {"content": " world"}}]}'
        yield 'data: {"model": "test-model", "choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}}'
        yield "data: [DONE]"

    mock_response.aiter_lines = mock_aiter_lines

    class MockStreamContext:
        async def __aenter__(self):
            return mock_response
        async def __aexit__(self, *args):
            pass

    provider.client.stream = MagicMock(return_value=MockStreamContext())

    reserve = AsyncMock(return_value=uuid.uuid4())
    finish = AsyncMock()
    monkeypatch.setattr("ah.core.provider.usage_store.reserve", reserve)
    monkeypatch.setattr("ah.core.provider.usage_store.finish", finish)

    session_id = uuid.uuid4()
    agent_id = "test-agent"

    events = []
    async for event in provider.stream_complete(
        messages=[{"role": "user", "content": "hello"}],
        session_id=session_id,
        agent_id=agent_id,
    ):
        events.append(event)

    reserve.assert_awaited_once()
    finish.assert_awaited_once()
    assert len(events) == 3  # 2 text + 1 done
    assert events[-1].type == "done"
    assert events[-1].response.content == "Hello world"


@pytest.mark.asyncio
async def test_openrouter_stream_complete_skips_usage_store_when_no_session_id(monkeypatch):
    """OpenRouterProvider.stream_complete() should NOT call usage_store when session_id is not provided."""
    from ah.core.provider import OpenRouterProvider

    provider = OpenRouterProvider.__new__(OpenRouterProvider)
    provider.api_key = "test-key"
    provider.model = "test-model"
    provider._rate_limiter = AsyncMock()
    provider.client = MagicMock()

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()

    async def mock_aiter_lines():
        yield 'data: {"model": "test-model", "choices": [{"delta": {"content": "Hello"}}]}'
        yield "data: [DONE]"

    mock_response.aiter_lines = mock_aiter_lines

    class MockStreamContext:
        async def __aenter__(self):
            return mock_response
        async def __aexit__(self, *args):
            pass

    provider.client.stream = MagicMock(return_value=MockStreamContext())

    reserve = AsyncMock(return_value=uuid.uuid4())
    finish = AsyncMock()
    monkeypatch.setattr("ah.core.provider.usage_store.reserve", reserve)
    monkeypatch.setattr("ah.core.provider.usage_store.finish", finish)

    events = []
    async for event in provider.stream_complete(
        messages=[{"role": "user", "content": "hello"}],
    ):
        events.append(event)

    reserve.assert_not_awaited()
    finish.assert_not_awaited()
    assert len(events) == 2  # 1 text + 1 done
