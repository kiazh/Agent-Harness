"""Usage budgets and durable accounting regressions."""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ah.core.agent import ReActAgent
from ah.core.config import config
from ah.core.exceptions import UsageBudgetExceededError
from ah.core.models import LLMResponse, Session, StreamEvent
from ah.core.usage import UsageStore, _check_limit
from ah.gateway.server import Gateway


def test_budget_check_counts_reservations_and_requests():
    with pytest.raises(UsageBudgetExceededError, match="request budget"):
        _check_limit("session", 2, 0, 2, 0, 100)
    with pytest.raises(UsageBudgetExceededError, match="token budget"):
        _check_limit("agent", 0, 90, 0, 100, 11)
    _check_limit("session", 0, 0, 0, 0, 100)


@pytest.mark.asyncio
async def test_agent_does_not_call_provider_after_budget_rejection(monkeypatch):
    provider = SimpleNamespace(model="test", complete=AsyncMock())
    agent = ReActAgent(provider=provider)
    reserve = AsyncMock(side_effect=UsageBudgetExceededError("session request budget exceeded"))
    monkeypatch.setattr("ah.core.agent.usage_store.reserve", reserve)

    with pytest.raises(UsageBudgetExceededError, match="session request budget"):
        await agent._call_llm_with_retry([{"role": "user", "content": "hello"}], [], uuid.uuid4())

    provider.complete.assert_not_awaited()
    assert reserve.await_count == 1  # A denied call is not retried.


@pytest.mark.asyncio
async def test_budget_error_reaches_regular_and_streaming_clients(monkeypatch):
    provider = SimpleNamespace(model="test", complete=AsyncMock())
    agent = ReActAgent(provider=provider)
    session_id = uuid.uuid4()
    session = Session(id=session_id, context_budget=8000)
    monkeypatch.setattr(
        agent, "_prepare_context",
        AsyncMock(return_value=(session, [{"role": "user", "content": "hello"}])),
    )
    monkeypatch.setattr("ah.core.agent.session_manager.get", AsyncMock(return_value=session))
    monkeypatch.setattr("ah.core.agent.plugin_registry.dispatch", AsyncMock())
    monkeypatch.setattr(
        "ah.core.agent.usage_store.reserve",
        AsyncMock(side_effect=UsageBudgetExceededError("session request budget exceeded")),
    )

    regular = await agent.run(session_id, "hello", verbose=False)
    streamed = [event async for event in agent.run_stream(session_id, "hello", verbose=False)]
    assert regular.content == "session request budget exceeded"
    assert streamed[-1].response.content == regular.content
    provider.complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_usage_retains_reservation(monkeypatch):
    execute = AsyncMock()
    monkeypatch.setattr("ah.core.usage.db", SimpleNamespace(execute=execute))
    store = UsageStore()
    call_id = uuid.uuid4()

    await store.finish(call_id, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
    assert execute.await_args.args[5] is None  # COALESCE retains reserved_tokens.
    await store.finish(call_id, {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7})
    assert execute.await_args.args[5] == 7


@pytest.mark.asyncio
async def test_gateway_usage_get_returns_persisted_summary(monkeypatch):
    sid = uuid.uuid4()
    session = Session(id=sid, agent_id="reviewer")
    frames = []
    gateway = Gateway(frames.append, owns_db=False)
    gateway._db_ready = True
    monkeypatch.setattr("ah.gateway.server.session_manager.get", AsyncMock(return_value=session))
    summary = AsyncMock(return_value={"sessionId": str(sid), "session": {"requests": 3}})
    monkeypatch.setattr("ah.core.usage.usage_store.summary", summary)

    await gateway.handle_line(json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "usage.get", "params": {"sessionId": str(sid)},
    }))
    assert frames[0]["result"]["session"]["requests"] == 3
    summary.assert_awaited_once_with(sid, "reviewer")


@pytest.mark.asyncio
async def test_database_budget_survives_new_store_instance(db_pool, monkeypatch):
    monkeypatch.setattr("ah.core.usage.db", db_pool)
    monkeypatch.setattr(config, "usage_session_request_limit", 1)
    monkeypatch.setattr(config, "usage_session_token_limit", 0)
    monkeypatch.setattr(config, "usage_agent_request_limit", 0)
    monkeypatch.setattr(config, "usage_agent_token_limit", 0)
    session_id = uuid.uuid4()
    agent_id = f"usage-test-{session_id}"
    messages = [{"role": "user", "content": "hello"}]
    try:
        first = await UsageStore().reserve(session_id, agent_id, "fake", "fake", messages, [], 8)
        await UsageStore().finish(first, {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7})
        summary = await UsageStore().summary(session_id, agent_id)
        assert summary["session"]["requests"] == 1
        assert summary["session"]["accountedTokens"] == 7
        assert summary["session"]["requestsRemaining"] == 0
        with pytest.raises(UsageBudgetExceededError, match="session request budget"):
            await UsageStore().reserve(session_id, agent_id, "fake", "fake", messages, [], 8)
    finally:
        await db_pool.execute("DELETE FROM llm_usage WHERE session_id = $1", session_id)


@pytest.mark.asyncio
async def test_agent_complete_and_stream_calls_are_persisted(db_pool, monkeypatch):
    monkeypatch.setattr("ah.core.usage.db", db_pool)
    for key in (
        "usage_session_request_limit", "usage_session_token_limit",
        "usage_agent_request_limit", "usage_agent_token_limit",
    ):
        monkeypatch.setattr(config, key, 0)

    class Provider:
        model = "router"

        async def complete(self, **_kwargs):
            return LLMResponse(
                content="OK", model="routed-model",
                usage={"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
            )

        async def stream_complete(self, **_kwargs):
            yield StreamEvent(type="text", content="OK")
            yield StreamEvent(
                type="done",
                response=LLMResponse(
                    content="OK", model="routed-model",
                    usage={"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
                ),
            )

    sid = uuid.uuid4()
    agent = ReActAgent(provider=Provider(), agent_id=f"agent-{sid}")
    try:
        await agent._call_llm_with_retry([{"role": "user", "content": "Hi"}], [], sid)
        events = [event async for event in agent._stream_llm_with_retry(
            [{"role": "user", "content": "Hi"}], [], sid,
        )]
        assert [event.type for event in events] == ["text", "done"]
        summary = await UsageStore().summary(sid, agent.agent_id)
        assert summary["session"]["requests"] == 2
        assert summary["session"]["accountedTokens"] == 13
        assert summary["session"]["unknownCalls"] == 0
        rows = await db_pool.fetch("SELECT model FROM llm_usage WHERE session_id = $1", sid)
        assert {row["model"] for row in rows} == {"routed-model"}
    finally:
        await db_pool.execute("DELETE FROM llm_usage WHERE session_id = $1", sid)


@pytest.mark.asyncio
async def test_failed_provider_call_keeps_conservative_usage(db_pool, monkeypatch):
    monkeypatch.setattr("ah.core.usage.db", db_pool)
    for key in (
        "usage_session_request_limit", "usage_session_token_limit",
        "usage_agent_request_limit", "usage_agent_token_limit",
    ):
        monkeypatch.setattr(config, key, 0)
    provider = SimpleNamespace(model="fake", complete=AsyncMock(side_effect=RuntimeError("offline")))
    sid = uuid.uuid4()
    try:
        with pytest.raises(RuntimeError, match="offline"):
            await UsageStore().complete_call(
                provider, sid, "harness", [{"role": "user", "content": "Hi"}], max_tokens=8,
            )
        row = await db_pool.fetchrow(
            "SELECT status, prompt_tokens, accounted_tokens, reserved_tokens "
            "FROM llm_usage WHERE session_id = $1",
            sid,
        )
        assert row["status"] == "error"
        assert row["prompt_tokens"] is None
        assert row["accounted_tokens"] == row["reserved_tokens"]
    finally:
        await db_pool.execute("DELETE FROM llm_usage WHERE session_id = $1", sid)


@pytest.mark.asyncio
async def test_concurrent_agent_budget_allows_one_reservation(db_pool, monkeypatch):
    monkeypatch.setattr("ah.core.usage.db", db_pool)
    monkeypatch.setattr(config, "usage_session_request_limit", 0)
    monkeypatch.setattr(config, "usage_session_token_limit", 0)
    monkeypatch.setattr(config, "usage_agent_request_limit", 1)
    monkeypatch.setattr(config, "usage_agent_token_limit", 0)
    agent_id = f"usage-concurrent-{uuid.uuid4()}"
    sessions = [uuid.uuid4(), uuid.uuid4()]
    messages = [{"role": "user", "content": "hello"}]
    try:
        results = await asyncio.gather(
            *(UsageStore().reserve(sid, agent_id, "fake", "fake", messages, [], 8)
              for sid in sessions),
            return_exceptions=True,
        )
        assert sum(isinstance(result, uuid.UUID) for result in results) == 1
        assert sum(isinstance(result, UsageBudgetExceededError) for result in results) == 1
    finally:
        await db_pool.execute("DELETE FROM llm_usage WHERE agent_id = $1", agent_id)
