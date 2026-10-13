"""FM8 #1: real turns emit tool / delegation / approval spans (wave-2 producers wired).

Wave-2 added ``tool_span`` / ``delegation_span`` / ``approval_span`` in
``machina/observability/traces.py`` with no production callers, so real turns
only carried turn + LLM spans. These tests drive the agent loop under an
active turn and assert the spans appear with truthful outcomes, and that a
raising tracing backend can never break a turn (observe-only contract).
"""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import pytest

from machina.core.models import LLMResponse, Session, StreamEvent
from machina.observability import traces
from machina.observability.traces import end_turn, start_turn, turn_report


@pytest.fixture(autouse=True)
def _isolated_traces():
    traces.reset_for_tests()
    yield
    traces.reset_for_tests()


def _tool_response(*calls: tuple[str, dict]) -> LLMResponse:
    return LLMResponse(
        content="",
        model="t",
        usage={"total_tokens": 5},
        tool_calls=[
            {
                "id": f"c{i}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
            for i, (name, args) in enumerate(calls)
        ],
    )


def _mock_session() -> Session:
    return Session(id=uuid.uuid4(), title="Test", context_budget=8000)


@contextmanager
def _managers(session):
    with (
        patch("machina.core.agent.session_manager") as mock_sm,
        patch("machina.core.agent.context_manager") as mock_cm,
    ):
        mock_sm.get = AsyncMock(return_value=session)
        mock_sm.update_activity = AsyncMock()
        mock_sm.update_state_fields = AsyncMock(return_value={})
        mock_cm.add_chunk = AsyncMock()
        mock_cm.add_chunks_batch = AsyncMock()
        mock_cm.get_latest_images = AsyncMock(return_value=[])
        mock_cm.get_recent_context = AsyncMock(return_value=[])
        yield mock_sm, mock_cm


async def test_turn_produces_tool_delegation_and_approval_spans(tmp_path):
    from machina.core.agent import ReActAgent
    from machina.permissions.broker import clear_execution_context, set_approval_handler
    from machina.tools.base import ToolRegistry

    reg = ToolRegistry()

    @reg.register(description="probe")
    async def probe_tool() -> str:
        return "probe-ok"

    @reg.register(description="stub delegate")
    async def delegate(agent: str = "", task: str = "") -> str:
        return "delegated ok"

    provider = AsyncMock()
    provider.complete = AsyncMock(
        side_effect=[
            _tool_response(
                ("probe_tool", {}),
                ("delegate", {"agent": "worker", "task": "do it"}),
                # Absolute out-of-workspace touch is not routine: it pauses.
                ("terminal", {"command": f"touch {tmp_path}/fm8_trace_probe"}),
            ),
            LLMResponse(content="done", model="t", usage={"total_tokens": 5}),
        ]
    )
    session = _mock_session()
    set_approval_handler(None)  # headless: the terminal action pauses
    start_turn("t-fm8", session_id=str(session.id), agent_id="fm8")
    try:
        with _managers(session), patch("machina.core.agent.registry", reg):
            response = await ReActAgent(provider=provider).run(session.id, "go", verbose=False)
        assert response.needs_approval, "terminal pause expected"
    finally:
        end_turn("t-fm8", status="ok", stop_reason="paused")
        set_approval_handler(None)
        clear_execution_context()

    report = turn_report("t-fm8")
    assert report is not None
    assert report["tool_calls"]["total"] == 3
    assert report["tool_calls"]["by_outcome"] == {"ok": 2, "paused": 1}
    assert report["delegations"]["total"] == 1
    assert report["delegations"]["by_status"] == {"ok": 1}
    assert report["approval_waits"]["count"] == 1


async def test_stream_turn_produces_tool_spans():
    from machina.core.agent import ReActAgent
    from machina.tools.base import ToolRegistry

    reg = ToolRegistry()

    @reg.register(description="probe")
    async def probe_tool() -> str:
        return "probe-ok"

    provider = AsyncMock()
    responses = [
        _tool_response(("probe_tool", {})),
        LLMResponse(content="done", model="t", usage={"total_tokens": 5}),
    ]
    calls = {"n": 0}

    async def _stream(*args, **kwargs):
        # One done event per provider call (each agent LLM round consumes one).
        index = min(calls["n"], len(responses) - 1)
        calls["n"] += 1
        yield StreamEvent(type="done", response=responses[index])

    provider.stream_complete = _stream
    session = _mock_session()
    start_turn("t-fm8-stream", session_id=str(session.id), agent_id="fm8")
    try:
        with _managers(session), patch("machina.core.agent.registry", reg):
            events = [
                e
                async for e in ReActAgent(provider=provider).run_stream(
                    session.id, "go", verbose=False
                )
            ]
        assert events and events[-1].type == "done"
    finally:
        end_turn("t-fm8-stream", status="ok", stop_reason="completed")

    report = turn_report("t-fm8-stream")
    assert report["tool_calls"]["total"] == 1
    assert report["tool_calls"]["by_outcome"] == {"ok": 1}


async def test_raising_trace_backend_never_breaks_a_tool_turn(monkeypatch):
    from machina.core.agent import ReActAgent
    from machina.tools.base import ToolRegistry

    def _boom(*args, **kwargs):
        raise RuntimeError("tracing backend down")

    monkeypatch.setattr(traces, "tool_span", _boom)
    monkeypatch.setattr(traces, "delegation_span", _boom)
    monkeypatch.setattr(traces, "approval_span", _boom)

    reg = ToolRegistry()

    @reg.register(description="probe")
    async def probe_tool() -> str:
        return "probe-ok"

    provider = AsyncMock()
    provider.complete = AsyncMock(
        side_effect=[
            _tool_response(("probe_tool", {})),
            LLMResponse(content="still works", model="t", usage={"total_tokens": 5}),
        ]
    )
    session = _mock_session()
    start_turn("t-fm8-boom", session_id=str(session.id), agent_id="fm8")
    try:
        with _managers(session), patch("machina.core.agent.registry", reg):
            response = await ReActAgent(provider=provider).run(session.id, "go", verbose=False)
    finally:
        end_turn("t-fm8-boom", status="ok", stop_reason="completed")

    assert response.content == "still works"
    assert response.tool_calls[0]["outcome"] == "completed"
