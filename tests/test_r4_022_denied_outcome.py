"""R4-022: the streamed ``tool_result`` outcome must be the truthful ``_tool_outcome``.

Verified finding: DENIED actions streamed ``isError=false`` + empty ``outcome``
(the taxonomy at ``agent.py::_tool_outcome`` knows ``denied``; Phase D
persistence used it, the streamed event did not), so the TUI painted the
green success check on "The action was NOT executed."

These tests drive the REAL agent tool loop (real ``permission_broker.guard``,
real denial) and the REAL ``TurnEngine`` relay -- the same seam the gateway
and SSE transports consume -- with no mocks on the fixed path.
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, patch

from machina.core.models import LLMResponse
from machina.core.turn_engine import TurnEngine, TurnSpec


def _tool_response(name: str, args: dict, call_id: str = "call-1") -> LLMResponse:
    return LLMResponse(
        content="",
        model="r4-022-probe",
        tool_calls=[
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
        ],
    )


async def _collect(agent, response, session_id):
    made: list = []
    events = []
    # A pending buffer keeps tool chunks in memory (the caller flushes); the
    # probe exercises the stream/outcome seam, not context persistence.
    async for event in agent._execute_tool_calls_stream(response, [], made, session_id, []):
        events.append(event)
    return events, made


async def test_broker_denied_tool_streams_denied_outcome():
    """A broker DENIAL (not a pause, not an error) streams outcome="denied"."""
    from machina.core.agent import BaseReActAgent
    from machina.core.agent_factory import parent_authority_var
    from machina.permissions.broker import clear_execution_context, set_approval_handler

    agent = BaseReActAgent(
        provider=AsyncMock(), agent_id="r4-022-denied", allowed_tools=["terminal"]
    )
    set_approval_handler(None)
    token = parent_authority_var.set({"tools": ["read_file"], "max_mode": "ask"})
    try:
        events, made = await _collect(
            agent, _tool_response("terminal", {"command": "touch /tmp/r4-022-probe"}), uuid.uuid4()
        )
    finally:
        parent_authority_var.reset(token)
        set_approval_handler(None)
        clear_execution_context()

    results = [event for event in events if event.type == "tool_result"]
    assert results, f"no streamed tool_result: {events}"
    streamed = results[0]
    assert streamed.tool_outcome == "denied", streamed
    assert "Permission denied" in streamed.tool_result, streamed.tool_result
    assert "NOT executed" in streamed.tool_result, streamed.tool_result
    # Phase D persistence must agree with the stream (one taxonomy).
    assert made and made[0]["outcome"] == "denied", made


async def test_denied_outcome_survives_turn_engine_relay():
    """The real relay forwards the denied outcome to ``tool_complete`` unchanged."""
    from machina.core.agent import BaseReActAgent
    from machina.core.agent_factory import parent_authority_var
    from machina.permissions.broker import clear_execution_context, set_approval_handler

    session_id = uuid.uuid4()

    class _LoopStream:
        """TurnEngine-compatible ``run_stream`` over the real agent tool loop."""

        def __init__(self, agent, response):
            self.agent = agent
            self.response = response

        async def run_stream(self, sid, _user_message, verbose=False, **kwargs):
            made: list = []
            async for event in self.agent._execute_tool_calls_stream(
                self.response, [], made, sid, []
            ):
                yield event

    agent = BaseReActAgent(
        provider=AsyncMock(), agent_id="r4-022-relay", allowed_tools=["terminal"]
    )
    engine = TurnEngine(
        TurnSpec(
            session_id=session_id,
            turn_id="r4-022-turn",
            token="r4-022-owner",
            text="denied probe",
        ),
        boundary="test.r4_022",
    )
    engine.attach(
        _LoopStream(agent, _tool_response("terminal", {"command": "touch /tmp/r4-022-probe"}))
    )
    set_approval_handler(None)
    token = parent_authority_var.set({"tools": ["read_file"], "max_mode": "ask"})
    try:
        events = [event async for event in engine.stream()]
    finally:
        parent_authority_var.reset(token)
        set_approval_handler(None)
        clear_execution_context()

    completes = [event for event in events if event.kind == "tool_complete"]
    assert completes, f"no tool_complete relayed: {events}"
    payload = completes[0].payload
    assert payload["outcome"] == "denied", payload
    assert payload["isError"] is False, payload
    assert "NOT executed" in payload["result"], payload


def test_wire_tool_complete_frame_carries_denied_outcome_additively():
    """The canonical HTTP frame carries the new outcome without extra fields."""
    from machina.protocol import canonical_http_event

    frame = canonical_http_event(
        {
            "type": "tool_result",
            "tool": "terminal",
            "result": "Permission denied: approval denied. The action was NOT executed.",
            "isError": False,
            "outcome": "denied",
            "id": "c1",
        },
        "sess-wire",
        "turn-wire",
    )
    assert frame["type"] == "tool.complete"
    assert frame["outcome"] == "denied"
    assert frame["isError"] is False
    assert frame["protocolVersion"] == 2

    legacy = canonical_http_event(
        {
            "type": "tool_result",
            "tool": "terminal",
            "result": "ok",
            "isError": False,
            "id": "c2",
        },
        "sess-wire",
        "turn-wire",
    )
    assert "outcome" not in legacy, legacy


async def test_paused_tool_streams_paused_not_denied_or_success():
    """Headless pause: taxonomy precedence keeps "paused" distinct from denial."""
    from machina.core.agent import BaseReActAgent
    from machina.permissions.broker import clear_execution_context, set_approval_handler

    agent = BaseReActAgent(
        provider=AsyncMock(), agent_id="r4-022-paused", allowed_tools=["terminal"]
    )
    set_approval_handler(None)  # headless: pending actions pause, never run
    try:
        events, made = await _collect(
            agent,
            _tool_response("terminal", {"command": "touch /tmp/r4-022-pause-probe"}),
            uuid.uuid4(),
        )
    finally:
        set_approval_handler(None)
        clear_execution_context()

    results = [event for event in events if event.type == "tool_result"]
    assert results, f"no streamed tool_result: {events}"
    assert results[0].tool_outcome == "paused", results[0]
    assert made and made[0]["outcome"] == "paused", made


async def test_executed_tool_streams_completed_outcome():
    """Executed tools stream their truthful completion label, not empty text."""
    from machina.core.agent import BaseReActAgent
    from machina.tools.base import ToolRegistry

    reg = ToolRegistry()

    @reg.register(description="probe")
    async def r4_022_probe_tool() -> str:
        return "ok"

    session_id = uuid.uuid4()
    with patch("machina.core.agent.registry", reg):
        agent = BaseReActAgent(provider=AsyncMock(), agent_id="r4-022-completed")
        events, made = await _collect(agent, _tool_response("r4_022_probe_tool", {}), session_id)

    results = [event for event in events if event.type == "tool_result"]
    assert results, f"no streamed tool_result: {events}"
    assert results[0].tool_outcome == "completed", results[0]
    assert made and made[0]["outcome"] == "completed", made
