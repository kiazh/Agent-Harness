"""Context capacity bounds one request, not cumulative billed usage."""

import uuid
from unittest.mock import AsyncMock

import pytest

from ah.core.agent import ReActAgent, _effective_turn_budget
from ah.core.config import config
from ah.core.models import LLMResponse, Session, StreamEvent


async def test_provider_metadata_supplies_window_and_is_cached(monkeypatch):
    import httpx
    from cachetools import TTLCache

    from ah.core import context_windows
    from ah.core.provider import OpenRouterProvider

    monkeypatch.setattr(
        context_windows, "_provider_windows", TTLCache(maxsize=4096, ttl=300), raising=False
    )
    monkeypatch.setattr(context_windows, "_refresh_after", 0, raising=False)
    calls = []

    async def handle(req):
        calls.append(req)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "fixture/million", "context_length": 1_000_000},
                    {"id": "fixture/small", "context_length": 16000},
                ]
            },
        )

    provider = OpenRouterProvider.__new__(OpenRouterProvider)
    provider.model = "fixture/million"
    async with httpx.AsyncClient(
        base_url=OpenRouterProvider.BASE_URL, transport=httpx.MockTransport(handle)
    ) as client:
        provider.client = client
        assert await provider.context_window() == 1_000_000
        assert await provider.context_window("fixture/small") == 16000
        assert len(calls) == 1


async def test_offline_metadata_uses_known_window_without_turn_cap(monkeypatch):
    import httpx
    from cachetools import TTLCache

    from ah.core import context_windows
    from ah.core.provider import OpenRouterProvider

    monkeypatch.setattr(
        context_windows, "_provider_windows", TTLCache(maxsize=4096, ttl=300), raising=False
    )
    monkeypatch.setattr(context_windows, "_refresh_after", 0, raising=False)

    async def handle(req):
        raise httpx.ConnectError("fixture offline")

    provider = OpenRouterProvider.__new__(OpenRouterProvider)
    provider.model = "google/gemini-2.0-flash"
    async with httpx.AsyncClient(
        base_url=OpenRouterProvider.BASE_URL, transport=httpx.MockTransport(handle)
    ) as client:
        provider.client = client
        assert await provider.context_window() == 1_048_576


def test_million_token_window_has_no_quarter_window_or_50k_ceiling():
    session = Session(id=uuid.uuid4(), model="google/gemini-2.0-flash")
    assert _effective_turn_budget(session, object()) == 1_048_576


@pytest.mark.parametrize("streaming", [False, True])
async def test_cumulative_usage_does_not_stop_a_small_current_request(monkeypatch, streaming):
    session = Session(id=uuid.uuid4(), model="google/gemini-2.0-flash")

    class Provider:
        model = session.model

    agent = ReActAgent(provider=Provider(), allowed_tools=[])
    monkeypatch.setattr(
        agent,
        "_prepare_context",
        AsyncMock(
            return_value=(
                session,
                [
                    {"role": "user", "content": "small request"},
                ],
            )
        ),
    )
    monkeypatch.setattr(agent, "_flush_pending_chunks", AsyncMock())
    monkeypatch.setattr("ah.core.agent.session_manager.update_activity", AsyncMock())
    monkeypatch.setattr("ah.core.agent.session_manager.get", AsyncMock(return_value=session))
    monkeypatch.setattr(agent, "_schedule_memory_consolidation", lambda *_: None)
    monkeypatch.setattr(agent, "_schedule_learning_review", lambda *_: None)
    calls = []

    async def complete(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return LLMResponse(
                content="",
                model=session.model,
                usage={"total_tokens": 2_000_000},
                tool_calls=[{"id": "next", "function": {"name": "unavailable", "arguments": "{}"}}],
            )
        return LLMResponse(content="finished", model=session.model, usage={"total_tokens": 7})

    async def stream(**kwargs):
        yield StreamEvent(type="done", response=await complete(**kwargs))

    monkeypatch.setattr(agent, "_call_llm_with_retry", complete)
    monkeypatch.setattr(agent, "_stream_llm_with_retry", stream)
    if streaming:
        events = [event async for event in agent.run_stream(session.id, "work", verbose=False)]
        response = events[-1].response
    else:
        response = await agent.run(session.id, "work", verbose=False)
    assert response.content == "finished"
    assert response.tokens_used == 2_000_007
    assert len(calls) == 2


@pytest.mark.parametrize("streaming", [False, True])
async def test_true_request_overflow_stops_before_provider_call(monkeypatch, streaming):
    session = Session(id=uuid.uuid4(), model="codellama")
    provider = type("Provider", (), {"model": "codellama"})()
    agent = ReActAgent(provider=provider, allowed_tools=[])
    monkeypatch.setattr(config, "max_tokens", 4096)
    monkeypatch.setattr(
        agent,
        "_prepare_context",
        AsyncMock(
            return_value=(
                session,
                [
                    {"role": "user", "content": "large " * 18000},
                ],
            )
        ),
    )
    monkeypatch.setattr(agent, "_flush_pending_chunks", AsyncMock())
    monkeypatch.setattr("ah.core.agent.session_manager.update_activity", AsyncMock())
    monkeypatch.setattr("ah.core.agent.session_manager.get", AsyncMock(return_value=session))
    monkeypatch.setattr(agent, "_schedule_memory_consolidation", lambda *_: None)
    call = AsyncMock(return_value=LLMResponse(content="wrong", model="codellama"))
    monkeypatch.setattr(agent, "_call_llm_with_retry", call)

    async def stream(**kwargs):
        yield StreamEvent(type="done", response=await call(**kwargs))

    monkeypatch.setattr(agent, "_stream_llm_with_retry", stream)
    if streaming:
        events = [event async for event in agent.run_stream(session.id, "work", verbose=False)]
        response = events[-1].response
    else:
        response = await agent.run(session.id, "work", verbose=False)
    assert "model context window" in response.content.lower()
    call.assert_not_awaited()


@pytest.mark.parametrize("streaming", [False, True])
async def test_real_loop_preserves_long_context_and_executes_reported_chain(
    tmp_path, monkeypatch, streaming
):
    from ah.core import context as context_module
    from ah.core.usage import usage_store
    from ah.permissions import store

    session = Session(id=uuid.uuid4(), model="fixture/million")
    monkeypatch.setattr(config, "workspace_root", str(tmp_path))
    monkeypatch.setattr(config, "terminal_sandbox", "local")
    for field in (
        "memory_enabled",
        "rag_enabled",
        "personality_growth_enabled",
        "memory_consolidation_enabled",
        "learning_review_enabled",
        "compression_enabled",
    ):
        monkeypatch.setattr(config, field, False)
    monkeypatch.setattr("ah.core.agent.session_manager.get", AsyncMock(return_value=session))
    monkeypatch.setattr("ah.core.agent.session_manager.update_activity", AsyncMock())
    for method in ("add_chunk", "add_chunks_batch", "search_archive_text"):
        monkeypatch.setattr(context_module.context_manager, method, AsyncMock(return_value=[]))
    monkeypatch.setattr(
        context_module.context_manager,
        "get_recent_context",
        AsyncMock(
            return_value=[
                {
                    "type": "user_message",
                    "payload": {"content": "saved " * 12000 + "tail-sentinel"},
                },
            ]
        ),
    )
    monkeypatch.setattr(usage_store, "reserve", AsyncMock(return_value=None))
    monkeypatch.setattr(usage_store, "finish", AsyncMock())
    monkeypatch.setattr(store, "_db", AsyncMock(return_value=None))

    class Provider:
        model = "fixture/million"

        def __init__(self):
            self.calls = []

        async def context_window(self, model=None):
            return 1_000_000

        async def complete(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                return LLMResponse(
                    content="",
                    model=self.model,
                    usage={"total_tokens": 90000},
                    tool_calls=[
                        {
                            "id": "shell",
                            "function": {
                                "name": "terminal",
                                "arguments": '{"command":"echo shell-ok && uname -s && date -u && pwd"}',
                            },
                        }
                    ],
                )
            return LLMResponse(content="finished", model=self.model, usage={"total_tokens": 7})

        async def stream_complete(self, **kwargs):
            yield StreamEvent(type="done", response=await self.complete(**kwargs))

    provider = Provider()
    agent = ReActAgent(provider=provider, allowed_tools=["terminal"])
    if streaming:
        events = [
            event async for event in agent.run_stream(session.id, "verify shell", verbose=False)
        ]
        response = events[-1].response
    else:
        response = await agent.run(session.id, "verify shell", verbose=False)
    assert response.content == "finished" and response.tokens_used == 90007
    assert len(provider.calls) == 2
    assert "tail-sentinel" in provider.calls[0]["messages"][0]["content"]
    assert str(tmp_path) in provider.calls[1]["messages"][-1]["content"]
    assert not response.needs_approval
