"""Regression coverage for provider accounting and retained tool history."""

import copy
import json
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from machina.core.agent import BaseReActAgent
from machina.core.config import config
from machina.core.models import LLMResponse, StreamEvent, ToolDefinition
from machina.core.provider import (
    PROVIDER_SPECS,
    AnthropicProvider,
    DirectProvider,
    OllamaProvider,
    OpenRouterProvider,
)
from machina.core.usage import _estimate_tokens, usage_store


def _stream_provider(chunks):
    provider = DirectProvider.__new__(DirectProvider)
    provider.spec = PROVIDER_SPECS["groq"]
    provider.api_key = ""
    provider.model = provider.spec.default_model
    provider._default_effort = ""
    provider._rate_limiter = AsyncMock()
    provider.client = MagicMock()
    response = MagicMock()

    async def lines():
        for chunk in chunks:
            yield "data: " + json.dumps(chunk)
        yield "data: [DONE]"

    response.aiter_lines = lines

    @asynccontextmanager
    async def stream(*args, **kwargs):
        yield response

    provider.client.stream.side_effect = stream
    return provider


@pytest.mark.asyncio
async def test_missing_vendor_usage_is_estimated_and_includes_tools(monkeypatch):
    tool = ToolDefinition("large_tool", "description " * 500, {"type": "object"})
    arguments = json.dumps({"data": "x " * 700})
    provider = _stream_provider(
        [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-a",
                                    "function": {"name": "large_tool", "arguments": arguments},
                                }
                            ]
                        }
                    }
                ]
            },
        ]
    )
    finish = AsyncMock()
    monkeypatch.setattr(usage_store, "reserve", AsyncMock(return_value=uuid.uuid4()))
    monkeypatch.setattr(usage_store, "finish", finish)
    messages = [{"role": "user", "content": "hello"}]
    events = [
        event
        async for event in provider.stream_complete(
            messages, tools=[tool], session_id=uuid.uuid4(), agent_id="test"
        )
    ]
    usage = events[-1].response.usage
    assert usage.get("estimated") is True
    assert usage["prompt_tokens"] >= _estimate_tokens(messages, [tool], 0)
    assert usage["completion_tokens"] > 100
    assert finish.await_args.args[1].get("estimated") is True


@pytest.mark.asyncio
async def test_stream_special_token_text_completes_without_vendor_usage():
    provider = _stream_provider([{"choices": [{"delta": {"content": "<|endoftext|>"}}]}])
    events = [
        event
        async for event in provider.stream_complete([{"role": "user", "content": "<|endoftext|>"}])
    ]
    assert [event.type for event in events] == ["text", "done"]
    assert events[-1].response.usage["total_tokens"] > 0


@pytest.mark.asyncio
async def test_agent_configured_output_limit_reaches_both_provider_paths(monkeypatch):
    monkeypatch.setattr(config, "max_tokens", 123)
    provider = MagicMock()
    provider.model = "fake"
    provider.complete = AsyncMock(return_value=LLMResponse(content="done", model="fake"))
    calls = []

    async def stream(**kwargs):
        calls.append(kwargs)
        yield StreamEvent(type="done", response=LLMResponse(content="done", model="fake"))

    provider.stream_complete = stream
    agent = BaseReActAgent.__new__(BaseReActAgent)
    agent.provider = provider
    agent.agent_id = "test"
    reserve = AsyncMock(return_value=None)
    monkeypatch.setattr(usage_store, "reserve", reserve)
    monkeypatch.setattr(usage_store, "finish", AsyncMock())
    messages = [{"role": "user", "content": "hello"}]
    session_id = uuid.uuid4()
    await agent._call_llm_with_retry(messages, [], session_id)
    assert provider.complete.await_args.kwargs["max_tokens"] == 123
    assert reserve.await_args.args[-1] == 123
    _ = [event async for event in agent._stream_llm_with_retry(messages, [], session_id)]
    assert calls[0]["max_tokens"] == 123
    assert reserve.await_args.args[-1] == 123


def test_latest_tool_batch_preserves_all_results_and_valid_message_order():
    agent = BaseReActAgent.__new__(BaseReActAgent)
    calls = [
        {
            "id": f"c{i}",
            "type": "function",
            "function": {
                "name": "read",
                "arguments": "{}",
            },
        }
        for i in range(2)
    ]
    response = LLMResponse(content="reading", model="fake", tool_calls=calls)
    messages = [{"role": "user", "content": "read two files"}]
    for i, tc in enumerate(calls):
        agent._append_tool_messages(messages, response, tc, str(i) * 1000)
    BaseReActAgent._condense_history(messages)
    assert [message["role"] for message in messages] == ["user", "assistant", "tool", "tool"]
    assert messages[1]["tool_calls"] == calls
    assert [m["content"] for m in messages if m["role"] == "tool"] == ["0" * 1000, "1" * 1000]


def test_condensing_stale_results_is_idempotent():
    messages = [
        {"role": "assistant", "tool_calls": [{"id": "old"}]},
        {"role": "tool", "content": "head" * 300 + "tail" * 300},
        {"role": "assistant", "tool_calls": [{"id": "latest"}]},
        {"role": "tool", "content": "latest"},
    ]
    BaseReActAgent._condense_history(messages)
    once = copy.deepcopy(messages)
    BaseReActAgent._condense_history(messages)
    assert messages == once


def _complete_provider(provider_class):
    provider = provider_class.__new__(provider_class)
    provider.model = "test-model"
    provider._default_effort = ""
    provider._rate_limiter = AsyncMock()
    if provider_class is DirectProvider:
        provider.spec = PROVIDER_SPECS["openai"]
        provider.api_key = ""
    provider.client = MagicMock()
    response = MagicMock()
    response.json.return_value = {
        "model": provider.model,
        "choices": [{"message": {"content": "OK"}}],
        "message": {"content": "OK"},
        "content": [{"type": "text", "text": "OK"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
    }
    response.status_code = 200
    response.headers = {}
    provider.client.post = AsyncMock(return_value=response)
    return provider


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider_class", [DirectProvider, OpenRouterProvider, OllamaProvider, AnthropicProvider]
)
async def test_response_cache_tracks_effective_output_and_sampling_parameters(provider_class):
    provider = _complete_provider(provider_class)
    messages = [{"role": "user", "content": "hello"}]
    for kwargs in (
        {"max_tokens": 123},
        {"max_tokens": 124},
        {"max_tokens": 124, "temperature": 0.8},
        {"max_tokens": 124, "temperature": 0.8, "reasoning_effort": "high"},
    ):
        await provider.complete(messages, **kwargs)
    assert provider.client.post.await_count == 4
    await provider.complete(messages, max_tokens=124, temperature=0.8, reasoning_effort="high")
    assert provider.client.post.await_count == 4


def _anthropic_stream_client(lines):
    """Mock client whose stream yields Anthropic SSE lines (terminal included)."""
    response = MagicMock()
    response.status_code = 200
    response.headers = {}
    response.raise_for_status = MagicMock()

    async def _lines():
        for line in lines:
            yield line

    response.aiter_lines = _lines

    @asynccontextmanager
    async def stream(*args, **kwargs):
        yield response

    client = MagicMock()
    client.stream.side_effect = stream
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_anthropic_direct_reservation_covers_thinking_output(monkeypatch, streaming):
    provider = _complete_provider(AnthropicProvider)
    reserve = AsyncMock(return_value=None)
    monkeypatch.setattr(usage_store, "reserve", reserve)
    kwargs = {
        "max_tokens": 123,
        "reasoning_effort": "high",
        "session_id": uuid.uuid4(),
        "agent_id": "test",
    }
    messages = [{"role": "user", "content": "hello"}]
    if streaming:
        provider.client = _anthropic_stream_client(
            [
                "event: message_start",
                'data: {"message": {"model": "test-model", "usage": {"input_tokens": 1}}}',
                "event: message_stop",
                'data: {"type": "message_stop"}',
            ]
        )
        _ = [event async for event in provider.stream_complete(messages, **kwargs)]
        payload = provider.client.stream.call_args.kwargs["json"]
    else:
        await provider.complete(messages, **kwargs)
        payload = provider.client.post.await_args.kwargs["json"]
    assert payload["max_tokens"] == 11024
    assert reserve.await_args.args[-1] == payload["max_tokens"]


@pytest.mark.asyncio
async def test_agent_reservation_covers_anthropic_default_thinking(monkeypatch):
    provider = _complete_provider(AnthropicProvider)
    provider._default_effort = "high"
    monkeypatch.setattr(config, "max_tokens", 123)
    agent = BaseReActAgent.__new__(BaseReActAgent)
    agent.provider = provider
    agent.agent_id = "test"
    reserve = AsyncMock(return_value=None)
    monkeypatch.setattr(usage_store, "reserve", reserve)
    await agent._call_llm_with_retry([{"role": "user", "content": "hello"}], [], uuid.uuid4())
    assert reserve.await_args.args[-1] == 11024
    await agent._reserve_usage(uuid.uuid4(), [{"role": "user", "content": "hello"}], [])
    assert reserve.await_args.args[-1] == 11024
