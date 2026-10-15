"""Final-review core/runtime fixes: F02 cancellation recording + flush contract.

F02 (``docs/verification/CODEBASE_REVIEW_FOLLOWUP_2026-10-15.md``): tool
completions are emitted as tasks finish, but context recording,
``tool_calls_made`` bookkeeping and approval retirement happened only in
Phase D after the whole batch. Cancelling the turn skipped Phase D, so an
already-completed effect was lost and an approved action could stay claimed.

``code_review.md`` #6: ``_flush_pending_chunks``'s docstring claimed failed
chunks were retained, but ``_flush_pending_cancellation_safe`` flushed a COPY
and then always cleared the original, silently dropping a failed batch.

The agent tool loop, models and flush paths are real; only the tool (a
sleeping stub) and — where noted — the permission broker / context store are
faked at their boundary. No database is required.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from machina.core.agent import BaseReActAgent
from machina.core.models import AgentResponse, LLMResponse
from machina.tools.base import Tool, registry


class _StubAgent(BaseReActAgent):
    """Minimal concrete agent so the base class's tool loop can be exercised."""

    def __init__(self) -> None:  # noqa: D107 - test double
        self.agent_id = "test-agent"
        self.allowed_tools = None


def _tool_call(name: str, args: dict, idx: int) -> dict:
    return {
        "id": f"call-{idx}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


@pytest.fixture
def sleep_tool(monkeypatch):
    """Register an async tool that sleeps, so completion order is controllable."""

    async def _sleeper(delay: float = 0.2, tag: str = "x") -> str:
        await asyncio.sleep(delay)
        return f"done-{tag}"

    monkeypatch.setitem(
        registry._tools,
        "sleep_probe",
        Tool(
            name="sleep_probe",
            description="sleep",
            parameters={
                "type": "object",
                "properties": {"delay": {"type": "number"}, "tag": {"type": "string"}},
                "required": [],
            },
            func=_sleeper,
            is_async=True,
        ),
    )
    registry._definitions_cache = None
    return None


async def _drive_until_first_result(agent, response, made, pending, session_id):
    """Drive the tool loop in a task; return once the first result is emitted.

    The generator is left suspended awaiting the still-running slow tool, so
    cancelling the returned task delivers CancelledError *inside* the generator
    (the documented F02 window).
    """
    events: list = []

    async def drive():
        async for event in agent._execute_tool_calls_stream(
            response, [], made, session_id, pending
        ):
            events.append(event)

    task = asyncio.create_task(drive())
    for _ in range(500):
        if any(event.type == "tool_result" for event in events):
            break
        await asyncio.sleep(0.01)
    return task, events


async def test_cancelled_batch_records_completed_tool(sleep_tool):
    """A tool that finished before cancellation keeps its durable record."""
    agent = _StubAgent()
    response = LLMResponse(
        content="",
        model="test-model",
        tool_calls=[
            _tool_call("sleep_probe", {"delay": 5.0, "tag": "slow"}, 0),
            _tool_call("sleep_probe", {"delay": 0.01, "tag": "fast"}, 1),
        ],
    )
    pending: list[dict] = []
    made: list[dict] = []

    task, events = await _drive_until_first_result(agent, response, made, pending, uuid.uuid4())
    assert any(
        event.type == "tool_result" and event.tool_result == "done-fast" for event in events
    ), events
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    recorded = [c for c in pending if c["payload"]["result_preview"] == "done-fast"]
    assert len(recorded) == 1, "completed tool record was dropped on cancellation"
    assert [entry["tool"] for entry in made] == ["sleep_probe"]
    assert made[0]["outcome"] == "completed"


async def test_cancelled_batch_retires_completed_approval(sleep_tool, monkeypatch):
    """A completed approved action must not stay in the claimed state."""

    class _FakeBroker:
        def __init__(self) -> None:
            self._n = 0
            self.completed: list[tuple[str, str]] = []

        async def guard(self, req):
            self._n += 1
            req.approval_id = f"appr-{self._n}"
            return req

        async def complete(self, request_id, outcome):
            self.completed.append((request_id, outcome))
            return True

    fake = _FakeBroker()
    monkeypatch.setattr("machina.permissions.broker.permission_broker", fake)

    agent = _StubAgent()
    response = LLMResponse(
        content="",
        model="test-model",
        tool_calls=[
            _tool_call("sleep_probe", {"delay": 5.0, "tag": "slow"}, 0),
            _tool_call("sleep_probe", {"delay": 0.01, "tag": "fast"}, 1),
        ],
    )
    pending: list[dict] = []
    made: list[dict] = []

    task, _events = await _drive_until_first_result(agent, response, made, pending, uuid.uuid4())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    outcomes = {outcome for _rid, outcome in fake.completed}
    assert "completed" in outcomes, "a completed approved action stayed claimed"
    assert "cancelled" in outcomes, "the cancelled in-flight action was not retired"


async def test_flush_retains_on_failure_and_clears_on_success(monkeypatch):
    """code_review #6: clear only on a confirmed flush; retain on failure."""
    import machina.core.agent as agent_mod

    agent = BaseReActAgent.__new__(BaseReActAgent)
    agent.agent_id = "harness"
    chunks = [
        {
            "session_id": uuid.uuid4(),
            "agent_id": "harness",
            "chunk_type": "user_message",
            "payload": {"content": "x"},
            "token_count": 1,
        }
    ]
    calls = {"n": 0}

    async def flaky(_batch):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("db down")
        return []

    monkeypatch.setattr(agent_mod.context_manager, "add_chunks_batch", flaky)

    assert await agent._flush_pending_chunks(chunks) is False
    assert chunks, "failed flush must retain pending chunks"
    assert await agent._flush_pending_chunks(chunks) is True
    assert not chunks, "successful flush must clear pending chunks"


async def test_cancellation_safe_flush_keeps_chunks_on_failure(monkeypatch):
    """The cancellation path must not clear a batch it never persisted."""
    import machina.core.agent as agent_mod

    agent = BaseReActAgent.__new__(BaseReActAgent)
    agent.agent_id = "harness"
    chunks = [
        {
            "session_id": uuid.uuid4(),
            "agent_id": "harness",
            "chunk_type": "user_message",
            "payload": {"content": "x"},
            "token_count": 1,
        }
    ]
    markers: list[dict] = []

    async def failing(_batch):
        raise RuntimeError("db down")

    async def add_chunk(**kwargs):
        markers.append(kwargs)
        return None

    monkeypatch.setattr(agent_mod.context_manager, "add_chunks_batch", failing)
    monkeypatch.setattr(agent_mod.context_manager, "add_chunk", add_chunk)

    await agent._flush_pending_cancellation_safe(chunks, uuid.uuid4())
    assert chunks, "failed cancellation flush must retain chunks, not clear them"
    assert markers, "the interruption marker must still be recorded"


async def test_cancellation_safe_flush_clears_on_success(monkeypatch):
    import machina.core.agent as agent_mod

    agent = BaseReActAgent.__new__(BaseReActAgent)
    agent.agent_id = "harness"
    chunks = [
        {
            "session_id": uuid.uuid4(),
            "agent_id": "harness",
            "chunk_type": "user_message",
            "payload": {"content": "x"},
            "token_count": 1,
        }
    ]

    async def ok(_batch):
        return []

    async def add_chunk(**kwargs):
        return None

    monkeypatch.setattr(agent_mod.context_manager, "add_chunks_batch", ok)
    monkeypatch.setattr(agent_mod.context_manager, "add_chunk", add_chunk)

    await agent._flush_pending_cancellation_safe(chunks, uuid.uuid4())
    assert not chunks, "a confirmed flush must clear pending chunks"


def test_agent_response_derives_all_plural_pauses():
    """F3: every delegated pause contributes, not only the singular first."""
    p1 = {"request_id": "r1", "operation": "a", "session_id": "s"}
    p2 = {"request_id": "r2", "operation": "b", "session_id": "s"}
    derived = AgentResponse(
        content="x",
        tool_calls=[
            {"tool": "delegate", "needs_approvals": [p1, p2]},
            {"tool": "read_file", "needs_approval": {"request_id": "r3", "operation": "c"}},
        ],
    )
    assert [p["request_id"] for p in derived.needs_approval] == ["r1", "r2", "r3"]

    # Explicit + derived pauses merge and dedupe by request_id.
    merged = AgentResponse(
        content="x",
        needs_approval=[p1],
        tool_calls=[{"tool": "delegate", "needs_approvals": [p1, p2]}],
    )
    assert [p["request_id"] for p in merged.needs_approval] == ["r1", "r2"]

    # An empty plural falls back to the singular (do not drop it).
    fallback = AgentResponse(
        content="x",
        tool_calls=[{"tool": "e", "needs_approvals": [], "needs_approval": p2}],
    )
    assert [p["request_id"] for p in fallback.needs_approval] == ["r2"]


async def test_repeated_cancel_during_recording_finishes_entire_batch(monkeypatch):
    from machina.permissions.broker import permission_broker

    entered = asyncio.Event()
    release = asyncio.Event()
    outcomes = []

    async def complete(approval_id, outcome):
        entered.set()
        await release.wait()
        outcomes.append((approval_id, outcome))
        return True

    monkeypatch.setattr(permission_broker, "complete", complete)
    agent = _StubAgent()
    planned = [
        {"name": "probe", "args": {}, "result": str(i), "elapsed_ms": 1, "_approval_id": str(i)}
        for i in range(2)
    ]
    pending, made = [], []
    task = asyncio.create_task(
        agent._finalize_completed_tools(planned, uuid.uuid4(), pending, made)
    )
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(pending) == len(made) == 2
    assert outcomes == [("0", "completed"), ("1", "completed")]
    assert all(item["_recorded"] and item["_approval_done"] for item in planned)


async def test_cancelled_worker_write_keeps_completed_effect(sleep_tool, tmp_path, monkeypatch):
    import threading
    import machina.tools.file as files
    from machina.permissions.broker import permission_broker

    entered, release = threading.Event(), threading.Event()
    target = tmp_path / "completed.txt"
    monkeypatch.setattr(files, "_BASE_DIR", tmp_path.resolve())
    real_write = files._atomic_write_file_at

    def gated_write(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return real_write(*args, **kwargs)

    async def probe(**kwargs):
        return await files.write_file("completed.txt", "saved")

    async def guard(request):
        return None

    monkeypatch.setattr(files, "_atomic_write_file_at", gated_write)
    monkeypatch.setattr(permission_broker, "guard", guard)
    monkeypatch.setattr(registry._tools["sleep_probe"], "func", probe)
    response = LLMResponse(content="", model="test", tool_calls=[_tool_call("sleep_probe", {}, 0)])
    pending, made = [], []
    agent = _StubAgent()

    async def drive():
        async for _ in agent._execute_tool_calls_stream(response, [], made, uuid.uuid4(), pending):
            pass

    task = asyncio.create_task(drive())
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert target.read_text() == "saved"
        assert len(made) == len(pending) == 1
        assert pending[0]["payload"]["result"].startswith("Successfully wrote")
    finally:
        release.set()


@pytest.mark.parametrize("pause_at", ["batch", "marker"])
async def test_repeated_cancel_joins_final_flush_and_marker(monkeypatch, pause_at):
    from machina.core.agent import context_manager

    entered, release = asyncio.Event(), asyncio.Event()
    completed = []

    async def batch(chunks):
        if pause_at == "batch":
            entered.set()
            await release.wait()
        completed.append("batch")

    async def marker(**kwargs):
        if pause_at == "marker":
            entered.set()
            await release.wait()
        completed.append("marker")

    monkeypatch.setattr(context_manager, "add_chunks_batch", batch)
    monkeypatch.setattr(context_manager, "add_chunk", marker)
    pending = [{"payload": {"result": "saved"}}]
    task = asyncio.create_task(_StubAgent()._flush_pending_cancellation_safe(pending, uuid.uuid4()))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert completed == ["batch", "marker"]
    assert pending == []


@pytest.mark.parametrize("exit_kind", ["close", "error"])
async def test_premature_tool_stream_exit_records_completed_effect(sleep_tool, exit_kind):
    agent = _StubAgent()
    response = LLMResponse(
        content="",
        model="test",
        tool_calls=[_tool_call("sleep_probe", {"delay": 0, "tag": "finished"}, 0)],
    )
    pending, made = [], []
    stream = agent._execute_tool_calls_stream(response, [], made, uuid.uuid4(), pending)
    async for event in stream:
        if event.type == "tool_result":
            break
    if exit_kind == "close":
        await stream.aclose()
    else:
        with pytest.raises(RuntimeError, match="consumer failed"):
            await stream.athrow(RuntimeError("consumer failed"))
    assert len(made) == len(pending) == 1
    assert pending[0]["payload"]["result"] == "done-finished"


@pytest.mark.parametrize("cancelled", [False, True])
async def test_failed_approval_retirement_can_retry_without_duplicate_records(
    monkeypatch, cancelled
):
    from machina.permissions.broker import permission_broker
    from unittest.mock import AsyncMock

    complete = AsyncMock(side_effect=[False, True])
    monkeypatch.setattr(permission_broker, "complete", complete)
    item = {
        "name": "probe",
        "args": {},
        "result": "saved",
        "elapsed_ms": 1,
        "_approval_id": "approval",
    }
    agent = _StubAgent()
    pending, made = [], []
    sid = uuid.uuid4()
    if cancelled:
        item.pop("elapsed_ms")
        item["_cancelled"] = True
    await agent._finalize_completed_tools([item], sid, pending, made)
    assert not item.get("_approval_done")
    await agent._finalize_completed_tools([item], sid, pending, made)
    assert item["_approval_done"]
    assert complete.await_count == 2
    assert len(pending) == len(made) == (0 if cancelled else 1)
    assert complete.await_args.args == ("approval", "cancelled" if cancelled else "completed")


async def test_closing_run_stream_flushes_completed_tool_before_return(sleep_tool, monkeypatch):
    from types import SimpleNamespace
    from machina.core import agent as agent_module
    from machina.core.models import StreamEvent

    from machina.core.agent import ReActAgent

    agent = ReActAgent.__new__(ReActAgent)
    agent.agent_id = "test-agent"
    agent.allowed_tools = None
    agent.provider = SimpleNamespace()
    response = LLMResponse(
        content="",
        model="test",
        tool_calls=[_tool_call("sleep_probe", {"delay": 0, "tag": "persisted"}, 0)],
    )
    persisted = []

    async def prepare(*args, **kwargs):
        return SimpleNamespace(), []

    async def provider_stream(**kwargs):
        yield StreamEvent(type="done", response=response)

    async def batch(chunks):
        persisted.extend(chunks)

    async def marker(**kwargs):
        persisted.append(kwargs)

    monkeypatch.setattr(agent, "_prepare_context", prepare)
    monkeypatch.setattr(agent, "_stream_llm_with_retry", provider_stream)
    monkeypatch.setattr(agent, "_condense_history", lambda messages: None)
    monkeypatch.setattr(agent, "_tool_defs", lambda: [])
    monkeypatch.setattr(agent_module, "_effective_turn_budget", lambda *args: 1000)
    monkeypatch.setattr(agent_module, "_request_context_tokens", lambda *args: 1)
    monkeypatch.setattr(agent_module.context_manager, "add_chunks_batch", batch)
    monkeypatch.setattr(agent_module.context_manager, "add_chunk", marker)
    stream = agent.run_stream(uuid.uuid4(), "run", verbose=False)
    async for event in stream:
        if event.type == "tool_result":
            break
    await stream.aclose()
    records = [chunk for chunk in persisted if chunk["chunk_type"] == "tool_call"]
    assert len(records) == 1
    assert records[0]["payload"]["result"] == "done-persisted"
    assert any("interrupted" in chunk["payload"].get("content", "") for chunk in persisted)


async def test_run_joins_post_tool_flush_during_repeated_cancellation(sleep_tool, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from machina.core import agent as agent_module
    from machina.core.agent import ReActAgent

    agent = ReActAgent.__new__(ReActAgent)
    agent.agent_id = "test-agent"
    agent.allowed_tools = None
    agent.provider = SimpleNamespace()
    response = LLMResponse(
        content="",
        model="test",
        tool_calls=[_tool_call("sleep_probe", {"delay": 0, "tag": "persisted"}, 0)],
    )
    entered, release = asyncio.Event(), asyncio.Event()
    persisted = []

    async def prepare(*args, **kwargs):
        return SimpleNamespace(), []

    async def batch(chunks):
        entered.set()
        await release.wait()
        persisted.extend(chunks)

    async def marker(**kwargs):
        persisted.append(kwargs)

    monkeypatch.setattr(agent, "_prepare_context", prepare)
    monkeypatch.setattr(agent, "_call_llm_with_retry", AsyncMock(return_value=response))
    monkeypatch.setattr(agent, "_condense_history", lambda messages: None)
    monkeypatch.setattr(agent, "_tool_defs", lambda: [])
    monkeypatch.setattr(agent_module, "_effective_turn_budget", lambda *args: 1000)
    monkeypatch.setattr(agent_module, "_request_context_tokens", lambda *args: 1)
    monkeypatch.setattr(agent_module.context_manager, "add_chunks_batch", batch)
    monkeypatch.setattr(agent_module.context_manager, "add_chunk", marker)
    task = asyncio.create_task(agent.run(uuid.uuid4(), "run", verbose=False))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    records = [chunk for chunk in persisted if chunk["chunk_type"] == "tool_call"]
    assert len(records) == 1
    assert records[0]["payload"]["result"] == "done-persisted"


@pytest.mark.parametrize("repeat_cancel", [False, True])
async def test_closing_text_stream_joins_provider_and_usage_finalization(
    monkeypatch, repeat_cancel
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from machina.core import agent as agent_module
    from machina.core.agent import ReActAgent
    from machina.core.models import StreamEvent

    entered, release = asyncio.Event(), asyncio.Event()
    order = []

    class Provider:
        async def stream_complete(self, **kwargs):
            try:
                yield StreamEvent(type="text", content="partial")
                await asyncio.Event().wait()
            finally:
                entered.set()
                await release.wait()
                order.append("provider stream")

    async def finish(*args, **kwargs):
        await asyncio.sleep(0)
        order.append("usage")

    agent = ReActAgent.__new__(ReActAgent)
    agent.agent_id = "test-agent"
    agent.allowed_tools = None
    agent.provider = Provider()
    monkeypatch.setattr(agent, "_prepare_context", AsyncMock(return_value=(SimpleNamespace(), [])))
    monkeypatch.setattr(agent, "_reserve_usage", AsyncMock(return_value=uuid.uuid4()))
    monkeypatch.setattr(agent, "_finish_usage", finish)
    monkeypatch.setattr(agent, "_condense_history", lambda messages: None)
    monkeypatch.setattr(agent, "_tool_defs", lambda: [])
    monkeypatch.setattr(agent_module, "_effective_turn_budget", lambda *args: 1000)
    monkeypatch.setattr(agent_module, "_request_context_tokens", lambda *args: 1)
    stream = agent.run_stream(uuid.uuid4(), "run", verbose=False)
    event = await anext(stream)
    assert event.type == "text"
    closing = asyncio.create_task(stream.aclose())
    await asyncio.wait_for(entered.wait(), 5)
    if repeat_cancel:
        closing.cancel()
        await asyncio.sleep(0)
        closing.cancel()
    assert order == []
    release.set()
    if repeat_cancel:
        with pytest.raises(asyncio.CancelledError):
            await closing
    else:
        await closing
    assert order == ["provider stream", "usage"]
