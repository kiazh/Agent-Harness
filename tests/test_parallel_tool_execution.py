"""Regression tests for concurrent tool execution in the ReAct agent.

Independent tool calls returned in a single assistant message run concurrently
(bounded). AH-020: tool_call (start) events are emitted BEFORE execution so
long-running tools appear as running; tool_result events follow as each
finishes (completion order, paired by call ID). Provider message order still
follows the original request order.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid

import pytest

from ah.core.agent import _MAX_PARALLEL_TOOLS, BaseReActAgent
from ah.core.models import LLMResponse
from ah.tools.base import registry


def _tool_call(name: str, args: dict, idx: int) -> dict:
    return {
        "id": f"call-{idx}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


class _StubAgent(BaseReActAgent):
    """Minimal concrete agent so the base class's tool loop can be exercised."""

    def __init__(self) -> None:  # noqa: D107 - test double
        self.agent_id = "test-agent"
        self.allowed_tools = None
        self.max_iterations = 1


@pytest.fixture(autouse=True)
def _stub_context_writes(monkeypatch):
    """Keep these tests DB-free: add_chunk normally writes to PostgreSQL."""
    from ah.core import context as context_module

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(context_module.context_manager, "add_chunk", _noop)
    return None


@pytest.fixture
def slow_tool(monkeypatch):
    """Register a tool that sleeps, and record concurrency high-water mark."""
    state = {"active": 0, "peak": 0, "started": []}

    async def _sleeper(delay: float = 0.2, tag: str = "x") -> str:
        state["started"].append(tag)
        state["active"] += 1
        state["peak"] = max(state["peak"], state["active"])
        try:
            await asyncio.sleep(delay)
        finally:
            state["active"] -= 1
        return f"done-{tag}"

    from ah.tools.base import Tool

    monkeypatch.setitem(
        registry._tools,
        "sleep_probe",
        Tool(
            name="sleep_probe",
            description="sleep",
            parameters={
                "type": "object",
                "properties": {
                    "delay": {"type": "number"},
                    "tag": {"type": "string"},
                },
                "required": [],
            },
            func=_sleeper,
            is_async=True,
        ),
    )
    # The registry caches definitions; drop it so the new tool is visible.
    registry._definitions_cache = None
    return state


async def _collect(agent, response, session_id):
    messages: list[dict] = [{"role": "user", "content": "go"}]
    made: list[dict] = []
    events = []
    async for event in agent._execute_tool_calls_stream(response, messages, made, session_id):
        events.append(event)
    return events, messages, made


async def test_independent_tools_run_concurrently(slow_tool):
    """Three 0.2s tools must overlap, not serialize into 0.6s."""
    agent = _StubAgent()
    response = LLMResponse(
        content="",
        model="test-model",
        tool_calls=[
            _tool_call("sleep_probe", {"delay": 0.2, "tag": "a"}, 0),
            _tool_call("sleep_probe", {"delay": 0.2, "tag": "b"}, 1),
            _tool_call("sleep_probe", {"delay": 0.2, "tag": "c"}, 2),
        ],
    )

    start = time.monotonic()
    await _collect(agent, response, uuid.uuid4())
    elapsed = time.monotonic() - start

    assert slow_tool["peak"] >= 2, "tools did not overlap — still sequential"
    assert elapsed < 0.5, f"expected overlap (~0.2s), took {elapsed:.2f}s"


async def test_result_order_is_preserved(slow_tool):
    """Completions arrive in finish order; provider messages stay in request order (AH-020)."""
    agent = _StubAgent()
    # Make the FIRST tool the slowest so completion order != request order.
    response = LLMResponse(
        content="",
        model="test-model",
        tool_calls=[
            _tool_call("sleep_probe", {"delay": 0.30, "tag": "first"}, 0),
            _tool_call("sleep_probe", {"delay": 0.01, "tag": "second"}, 1),
        ],
    )

    events, messages, made = await _collect(agent, response, uuid.uuid4())

    results = [e for e in events if e.type == "tool_result"]
    # Completion order: the fast second tool finishes first.
    assert [e.tool_result for e in results] == ["done-second", "done-first"]
    # Call IDs still pair each start with its own result.
    starts = [e for e in events if e.type == "tool_call"]
    assert {e.tool_call_id for e in starts} == {e.tool_call_id for e in results}
    assert [m["tool"] for m in made] == ["sleep_probe", "sleep_probe"]
    # The assistant/tool message pairs must follow request order for the provider.
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert "done-first" in tool_msgs[0]["content"]


async def test_events_pair_each_call_with_its_result(slow_tool):
    """Starts emit before execution; completions pair by call ID (AH-020)."""
    agent = _StubAgent()
    response = LLMResponse(
        content="",
        model="test-model",
        tool_calls=[
            _tool_call("sleep_probe", {"delay": 0.05, "tag": "a"}, 0),
            _tool_call("sleep_probe", {"delay": 0.05, "tag": "b"}, 1),
        ],
    )

    events, _, _ = await _collect(agent, response, uuid.uuid4())
    kinds = [e.type for e in events]

    # All starts first (visible as running), then completions as they finish.
    assert kinds == ["tool_call", "tool_call", "tool_result", "tool_result"]
    # Results pair by call ID, not position.
    starts = [e for e in events if e.type == "tool_call"]
    results = [e for e in events if e.type == "tool_result"]
    assert {e.tool_call_id for e in starts} == {e.tool_call_id for e in results}
    assert sorted(e.tool_result for e in results) == ["done-a", "done-b"]


async def test_concurrency_peak_proves_parallelism(slow_tool):
    """The concurrency high-water mark is the direct proof of overlap.

    Event ordering is deliberately serial-looking (for client coherence), so
    wall-clock alone is not sufficient evidence; the peak active count is.
    """
    agent = _StubAgent()
    response = LLMResponse(
        content="",
        model="test-model",
        tool_calls=[
            _tool_call("sleep_probe", {"delay": 0.2, "tag": "a"}, 0),
            _tool_call("sleep_probe", {"delay": 0.2, "tag": "b"}, 1),
            _tool_call("sleep_probe", {"delay": 0.2, "tag": "c"}, 2),
        ],
    )

    await _collect(agent, response, uuid.uuid4())

    assert slow_tool["peak"] == 3, (
        f"expected all 3 tools in flight together, peak was {slow_tool['peak']}"
    )


async def test_parallelism_is_bounded(monkeypatch, slow_tool):
    """No more than _MAX_PARALLEL_TOOLS tools run at once."""
    agent = _StubAgent()
    n = _MAX_PARALLEL_TOOLS + 4
    response = LLMResponse(
        content="",
        model="test-model",
        tool_calls=[_tool_call("sleep_probe", {"delay": 0.05, "tag": str(i)}, i) for i in range(n)],
    )

    await _collect(agent, response, uuid.uuid4())

    assert slow_tool["peak"] <= _MAX_PARALLEL_TOOLS, (
        f"peak concurrency {slow_tool['peak']} exceeded cap {_MAX_PARALLEL_TOOLS}"
    )
    assert len(slow_tool["started"]) == n


async def test_invalid_args_do_not_break_the_batch(slow_tool):
    """A malformed call is reported without blocking its siblings."""
    agent = _StubAgent()
    bad = _tool_call("sleep_probe", {}, 0)
    bad["function"]["arguments"] = "{not valid json"
    response = LLMResponse(
        content="",
        model="test-model",
        tool_calls=[bad, _tool_call("sleep_probe", {"delay": 0.01, "tag": "ok"}, 1)],
    )

    events, messages, made = await _collect(agent, response, uuid.uuid4())

    results = [e.tool_result for e in events if e.type == "tool_result"]
    assert results == ["done-ok"]
    # The malformed call still gets a tool reply so the model can recover.
    assert any(
        "invalid tool arguments" in m.get("content", "")
        for m in messages
        if m.get("role") == "tool"
    )
    assert len(made) == 1
