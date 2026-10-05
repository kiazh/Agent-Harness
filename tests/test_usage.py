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
from ah.core.exceptions import DatabaseError, UsageBudgetExceededError, ValidationError
from ah.core.models import LLMResponse, Session, StreamEvent
from ah.core.usage import UsageStore, _check_limit, _estimate_tokens, _limits
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


# ---------------------------------------------------------------------------
# Additional coverage tests
# ---------------------------------------------------------------------------


def test_limits_rejects_negative_values(monkeypatch):
    """_limits() raises ValidationError when any budget is negative."""
    monkeypatch.setattr(config, "usage_session_token_limit", -1)
    with pytest.raises(ValidationError, match="non-negative"):
        _limits()


def test_estimate_tokens_fallback_on_value_error(monkeypatch):
    """_estimate_tokens falls back to len(payload)//3 when tiktoken rejects input."""
    import ah.core.usage as usage_mod

    monkeypatch.setattr(usage_mod, "get_token_count", lambda _text: (_ for _ in ()).throw(ValueError("bad token")))
    result = _estimate_tokens([{"role": "user", "content": "hello"}], [], 100)
    assert result > 0


@pytest.mark.asyncio
async def test_reserve_raises_database_error_when_db_not_connected(monkeypatch):
    """reserve() raises DatabaseError when budgets are set but DB is not connected."""
    monkeypatch.setattr(config, "usage_session_request_limit", 10)
    monkeypatch.setattr("ah.core.usage.db", SimpleNamespace(connected=False))
    store = UsageStore()
    with pytest.raises(DatabaseError, match="database required"):
        await store.reserve(uuid.uuid4(), "agent", "fake", "fake", [], [], 8)


@pytest.mark.asyncio
async def test_reserve_propagates_db_exceptions(monkeypatch):
    """reserve() propagates DB exceptions directly (wrapping happens in complete_call)."""
    monkeypatch.setattr(config, "usage_session_request_limit", 0)
    monkeypatch.setattr(config, "usage_session_token_limit", 0)
    monkeypatch.setattr(config, "usage_agent_request_limit", 0)
    monkeypatch.setattr(config, "usage_agent_token_limit", 0)

    class FakeTransaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def execute(self, *args, **kwargs):
            raise RuntimeError("connection lost")

        async def fetchrow(self, *args, **kwargs):
            return {"requests": 0, "tokens": 0, "id": uuid.uuid4()}

    class FakeConn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def transaction(self):
            return FakeTransaction()

        async def execute(self, *args, **kwargs):
            raise RuntimeError("connection lost")

        async def fetchrow(self, *args, **kwargs):
            return {"requests": 0, "tokens": 0, "id": uuid.uuid4()}

        async def fetch(self, *args, **kwargs):
            return [
                {"scope": "agent", "requests": 0, "tokens": 0},
                {"scope": "session", "requests": 0, "tokens": 0},
            ]

    class FakeDB:
        connected = True

        def acquire(self):
            return FakeConn()

    monkeypatch.setattr("ah.core.usage.db", FakeDB())
    store = UsageStore()
    with pytest.raises(RuntimeError, match="connection lost"):
        await store.reserve(uuid.uuid4(), "agent", "fake", "fake", [], [], 8)


@pytest.mark.asyncio
async def test_complete_call_wraps_reserve_exception_as_database_error(monkeypatch):
    """complete_call() wraps non-budget reserve exceptions in DatabaseError."""
    provider = SimpleNamespace(model="test", complete=AsyncMock())
    monkeypatch.setattr(UsageStore, "reserve", AsyncMock(side_effect=RuntimeError("boom")))
    store = UsageStore()
    with pytest.raises(DatabaseError, match="usage accounting unavailable"):
        await store.complete_call(provider, uuid.uuid4(), "agent", [], max_tokens=8)
    provider.complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_complete_call_wraps_finish_exception_after_provider_error(monkeypatch):
    """complete_call() wraps finish exceptions (after provider failure) in DatabaseError."""
    provider = SimpleNamespace(model="test", complete=AsyncMock(side_effect=RuntimeError("offline")))
    monkeypatch.setattr(UsageStore, "reserve", AsyncMock(return_value=uuid.uuid4()))
    monkeypatch.setattr(UsageStore, "finish", AsyncMock(side_effect=RuntimeError("finish failed")))
    store = UsageStore()
    with pytest.raises(DatabaseError, match="usage accounting unavailable"):
        await store.complete_call(provider, uuid.uuid4(), "agent", [], max_tokens=8)


@pytest.mark.asyncio
async def test_complete_call_wraps_finish_exception_after_provider_success(monkeypatch):
    """complete_call() wraps finish exceptions (after provider success) in DatabaseError."""
    provider = SimpleNamespace(
        model="test",
        complete=AsyncMock(
            return_value=LLMResponse(
                content="ok", model="test",
                usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            )
        ),
    )
    monkeypatch.setattr(UsageStore, "reserve", AsyncMock(return_value=uuid.uuid4()))
    monkeypatch.setattr(UsageStore, "finish", AsyncMock(side_effect=RuntimeError("finish failed")))
    store = UsageStore()
    with pytest.raises(DatabaseError, match="usage accounting unavailable"):
        await store.complete_call(provider, uuid.uuid4(), "agent", [], max_tokens=8)


@pytest.mark.asyncio
async def test_summary_filters_to_complete_status(db_pool, monkeypatch):
    """summary() only counts rows with status='complete' for requests and tokens."""
    monkeypatch.setattr("ah.core.usage.db", db_pool)
    for key in (
        "usage_session_request_limit", "usage_session_token_limit",
        "usage_agent_request_limit", "usage_agent_token_limit",
    ):
        monkeypatch.setattr(config, key, 0)
    store = UsageStore()
    sid = uuid.uuid4()
    agent_id = f"summary-filter-{sid}"
    try:
        # One complete call
        rid = await store.reserve(sid, agent_id, "fake", "fake", [{"role": "user", "content": "hi"}], [], 8)
        await store.finish(rid, {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5})
        # One reserved (unfinished) call — should not appear in summary
        await store.reserve(sid, agent_id, "fake", "fake", [{"role": "user", "content": "hi"}], [], 8)
        summary = await store.summary(sid, agent_id)
        assert summary["session"]["requests"] == 1
        assert summary["session"]["accountedTokens"] == 5
        assert summary["session"]["chargedRequests"] == 2
        assert summary["session"]["unknownCalls"] == 1
    finally:
        await db_pool.execute("DELETE FROM llm_usage WHERE session_id = $1", sid)


@pytest.mark.asyncio
async def test_totals_aggregation(monkeypatch):
    """totals() returns aggregated counts across all rows."""
    class FakeRow:
        def __getitem__(self, key):
            return {"requests": 10, "tokens": 500, "unknown_calls": 3}[key]

    fetchrow = AsyncMock(return_value=FakeRow())
    monkeypatch.setattr("ah.core.usage.db", SimpleNamespace(fetchrow=fetchrow))
    store = UsageStore()
    result = await store.totals()
    assert result == {"requests": 10, "accounted_tokens": 500, "unknown_calls": 3}


@pytest.mark.asyncio
async def test_cleanup_orphaned_reservations_parses_update_count(monkeypatch):
    """cleanup_orphaned_reservations() parses the UPDATE count from the result string."""
    execute = AsyncMock(return_value="UPDATE 5")
    monkeypatch.setattr("ah.core.usage.db", SimpleNamespace(execute=execute))
    store = UsageStore()
    result = await store.cleanup_orphaned_reservations()
    assert result == 5


@pytest.mark.asyncio
async def test_cleanup_orphaned_reservations_returns_zero_on_bad_result(monkeypatch):
    """cleanup_orphaned_reservations() returns 0 when the result string is unparseable."""
    execute = AsyncMock(return_value="UPDATE")
    monkeypatch.setattr("ah.core.usage.db", SimpleNamespace(execute=execute))
    store = UsageStore()
    result = await store.cleanup_orphaned_reservations()
    assert result == 0


@pytest.mark.asyncio
async def test_reserve_locks_agent_before_session(monkeypatch):
    """reserve() acquires advisory locks in fixed order: agent then session."""
    monkeypatch.setattr(config, "usage_session_request_limit", 0)
    monkeypatch.setattr(config, "usage_session_token_limit", 0)
    monkeypatch.setattr(config, "usage_agent_request_limit", 0)
    monkeypatch.setattr(config, "usage_agent_token_limit", 0)

    lock_calls = []

    class FakeConn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def transaction(self):
            return self

        async def execute(self, sql, *args):
            if "pg_advisory_xact_lock" in sql:
                lock_calls.append(args[0])

        async def fetchrow(self, *args, **kwargs):
            return {"requests": 0, "tokens": 0, "id": uuid.uuid4()}

        async def fetch(self, *args, **kwargs):
            return [
                {"scope": "agent", "requests": 0, "tokens": 0},
                {"scope": "session", "requests": 0, "tokens": 0},
            ]

    class FakeDB:
        connected = True

        def acquire(self):
            return FakeConn()

    monkeypatch.setattr("ah.core.usage.db", FakeDB())
    store = UsageStore()
    sid = uuid.uuid4()
    agent_id = "test-agent"
    await store.reserve(sid, agent_id, "fake", "fake", [], [], 8)
    assert len(lock_calls) == 2
    assert lock_calls[0] == f"llm_usage:agent:{agent_id}"
    assert lock_calls[1] == f"llm_usage:session:{sid}"


@pytest.mark.asyncio
async def test_reserve_returns_none_without_session(monkeypatch):
    """reserve() returns None when session_id is None (no session-scoped budget)."""
    store = UsageStore()
    result = await store.reserve(None, "agent", "fake", "fake", [], [], 8)
    assert result is None
