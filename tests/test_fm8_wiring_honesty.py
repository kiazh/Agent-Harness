"""FM8 wave-4 fixes: stagnation honesty, eager-diagnostics guard, paused outcome, loop symmetry.

RED-first coverage (verified failing before the wave-4 fix):

- #2/#3  the stagnation stop is written as SESSION STATE (a durable evidence
         summary built from ``tool_calls_made``, no LLM call) and never as a
         context chunk, so the ``[stagnation stop]`` marker cannot re-enter the
         next turn's recent-context window; the response text names the write.
- #4     ``log_section_tokens`` performs no JSON dump / tokenization unless
         DEBUG is enabled for its logger.
- #5     needs-approval tool entries carry the truthful ``outcome="paused"``
         instead of claiming ``"completed"``.
- #9     ``run()`` and ``run_stream()`` call the same wiring helpers, counted
         through one shared spy for the identical turn shape.
"""

from __future__ import annotations

import json
import logging
import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from machina.core.models import LLMResponse, Session, StreamEvent


def _mock_session():
    return Session(id=uuid.uuid4(), title="Test", context_budget=8000)


def _run_mocks(mock_sm, mock_cm, session, flushed=None):
    mock_sm.get = AsyncMock(return_value=session)
    mock_sm.update_activity = AsyncMock()
    mock_sm.update_state_fields = AsyncMock(return_value={})
    mock_cm.add_chunk = AsyncMock()
    if flushed is None:
        mock_cm.add_chunks_batch = AsyncMock()
    else:
        # Snapshot batch contents at call time: the agent clears the live
        # pending list right after flushing, so recorded call args alias [].
        async def _capture(batch):
            flushed.extend(batch or [])

        mock_cm.add_chunks_batch = AsyncMock(side_effect=_capture)
    mock_cm.get_latest_images = AsyncMock(return_value=[])
    mock_cm.get_recent_context = AsyncMock(return_value=[])


def _stream_once(*responses):
    """One ``done`` event per provider call (the agent consumes one stream per LLM call)."""
    calls = {"n": 0}

    async def _stream(*args, **kwargs):
        index = min(calls["n"], len(responses) - 1)
        calls["n"] += 1
        yield StreamEvent(type="done", response=responses[index])

    return _stream


def _recall_response(query: str) -> LLMResponse:
    return LLMResponse(
        content="",
        model="t",
        usage={"total_tokens": 5},
        tool_calls=[
            {
                "id": "c1",
                "function": {"name": "session_recall", "arguments": json.dumps({"query": query})},
            }
        ],
    )


def _tool_response(name: str, args: dict, call_id: str = "call-1") -> LLMResponse:
    return LLMResponse(
        content="",
        model="t",
        usage={"total_tokens": 5},
        tool_calls=[
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
        ],
    )


def _assert_state_write(state, expected_rounds: int) -> None:
    assert state["tool_calls"] == expected_rounds
    assert state["evidence"]["session_recall"] == expected_rounds
    assert "stagnant" in state["reason"].lower()
    assert "recorded as session state" in state["stop_text"]
    assert "max iterations" not in state["stop_text"].lower()


# ─── #2/#3: stagnation marker lives in session state, not in context ─────────


async def test_stagnation_stop_is_session_state_not_context_chunk():
    from machina.core.agent import ReActAgent
    from machina.core.progress import STAGNATION_ROUNDS_K
    from machina.tools.base import ToolRegistry

    reg = ToolRegistry()

    @reg.register(description="stub recall")
    async def session_recall(query: str = "") -> str:
        return "echo: no new evidence"

    provider = AsyncMock()
    provider.complete = AsyncMock(
        side_effect=[_recall_response("same query")] * (STAGNATION_ROUNDS_K + 3)
        + [LLMResponse(content="unreached", model="t", usage={"total_tokens": 5})]
    )
    session = _mock_session()
    flushed: list = []
    with (
        patch("machina.core.agent.session_manager") as mock_sm,
        patch("machina.core.agent.context_manager") as mock_cm,
        patch("machina.core.agent.registry", reg),
    ):
        _run_mocks(mock_sm, mock_cm, session, flushed=flushed)
        response = await ReActAgent(provider=provider).run(session.id, "go", verbose=False)

    assert response.outcome == "failed"
    rounds = response.iterations  # K+1 batches, each one recall round
    assert rounds >= STAGNATION_ROUNDS_K
    # The stop marker must NOT be persisted as a context chunk: chunks are
    # replayed into the next turn's recent-context window.
    assert flushed, "expected tool/history chunks to be flushed"
    assert not any(
        "[stagnation stop]" in str(c.get("payload", {}).get("content", "")) for c in flushed
    ), "stagnation marker leaked into persisted context chunks"
    # The forced write (no LLM call) actually happened, as session state.
    mock_sm.update_state_fields.assert_awaited()
    fields = mock_sm.update_state_fields.await_args.args[1]
    _assert_state_write(fields["stagnation_stop"], rounds)
    assert fields["stagnation_stop"]["stop_text"] == response.content
    provider.complete.assert_awaited()  # no extra LLM call for the write


async def test_stream_stagnation_stop_is_session_state_not_context_chunk():
    from machina.core.agent import ReActAgent
    from machina.core.progress import STAGNATION_ROUNDS_K
    from machina.tools.base import ToolRegistry

    reg = ToolRegistry()

    @reg.register(description="stub recall")
    async def session_recall(query: str = "") -> str:
        return "echo: no new evidence"

    provider = AsyncMock()
    provider.stream_complete = _stream_once(
        *[_recall_response("same query")] * (STAGNATION_ROUNDS_K + 3)
    )
    session = _mock_session()
    flushed: list = []
    with (
        patch("machina.core.agent.session_manager") as mock_sm,
        patch("machina.core.agent.context_manager") as mock_cm,
        patch("machina.core.agent.registry", reg),
    ):
        _run_mocks(mock_sm, mock_cm, session, flushed=flushed)
        events = [
            e
            async for e in ReActAgent(provider=provider).run_stream(session.id, "go", verbose=False)
        ]
    done = [e for e in events if e.type == "done"]
    assert done and done[-1].response is not None
    response = done[-1].response
    assert response.outcome == "failed"

    assert not any(
        "[stagnation stop]" in str(c.get("payload", {}).get("content", "")) for c in flushed
    ), "stagnation marker leaked into persisted context chunks"
    mock_sm.update_state_fields.assert_awaited()
    fields = mock_sm.update_state_fields.await_args.args[1]
    _assert_state_write(fields["stagnation_stop"], response.iterations)


# ─── #4: eager section-token dump guarded by DEBUG ───────────────────────────


def test_log_section_tokens_does_no_work_unless_debug(monkeypatch):
    from machina.controller import wiring
    from machina.core import prompt as prompt_mod

    calls: list = []

    def _record(sections):
        calls.append(sections)
        return {key: 1 for key in sections}

    monkeypatch.setattr(prompt_mod, "prompt_section_tokens", _record)
    logger = logging.getLogger("machina.controller.wiring")
    previous = logger.level
    try:
        logger.setLevel(logging.WARNING)
        assert wiring.log_section_tokens([{"role": "user", "content": "x"}], []) is None
        assert calls == [], "eager JSON dump/tokenization ran with DEBUG disabled"
        logger.setLevel(logging.DEBUG)
        assert wiring.log_section_tokens([{"role": "user", "content": "x"}], []) is None
        assert len(calls) == 1, "DEBUG-enabled diagnostics must still run"
    finally:
        logger.setLevel(previous)


# ─── #5: needs-approval entries are labeled paused, truthfully ───────────────


async def test_needs_approval_tool_entry_is_paused_not_completed(tmp_path):
    from machina.core.agent import BaseReActAgent
    from machina.permissions.broker import clear_execution_context, set_approval_handler

    set_approval_handler(None)  # headless: pending actions pause, never run
    try:
        agent = BaseReActAgent(provider=AsyncMock(), agent_id="fm8-agent", allowed_tools=["terminal"])
        made: list = []
        pending: list = []
        [
            event
            async for event in agent._execute_tool_calls_stream(
                # Absolute out-of-workspace touch is not a routine command: it pauses.
                _tool_response("terminal", {"command": f"touch {tmp_path}/fm8_pause_probe"}),
                [],
                made,
                uuid.uuid4(),
                pending,
            )
        ]
        assert made and made[0].get("needs_approval"), made
        assert made[0]["outcome"] == "paused", made[0]
    finally:
        set_approval_handler(None)
        clear_execution_context()


async def test_completed_tool_entry_stays_completed():
    from machina.core.agent import BaseReActAgent
    from machina.tools.base import ToolRegistry

    reg = ToolRegistry()

    @reg.register(description="probe")
    async def probe_tool() -> str:
        return "ok"

    with patch("machina.core.agent.registry", reg):
        agent = BaseReActAgent(provider=AsyncMock(), agent_id="fm8-agent")
        made: list = []
        pending: list = []
        [
            event
            async for event in agent._execute_tool_calls_stream(
                _tool_response("probe_tool", {}), [], made, uuid.uuid4(), pending
            )
        ]
    assert made and made[0]["outcome"] == "completed", made


# ─── #9: run() and run_stream() call the same wiring helpers ─────────────────


class _WiringSpy:
    """One shared fake for every wiring helper the agent loops call."""

    def __init__(self):
        self.calls: dict[str, int] = {}

    def _hit(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1

    def record_triage(self, agent, user_message):
        self._hit("record_triage")

    def log_complexity(self, agent, user_message):
        self._hit("log_complexity")

    def log_section_tokens(self, messages, tools):
        self._hit("log_section_tokens")

    def check_stagnation(self, history, batch):
        self._hit("check_stagnation")
        return (history, None)

    def stagnation_content(self, stop, n, tool_calls=None):
        self._hit("stagnation_content")
        return "stub"

    async def record_stagnation_state(self, *args, **kwargs):
        self._hit("record_stagnation_state")

    def record_approval_pause(self, approval):
        self._hit("record_approval_pause")

    @contextmanager
    def trace_tool_call(self, item):
        self._hit("trace_tool_call")
        yield SimpleNamespace(finish=lambda outcome: self._hit("trace_finish"))


def _patch_wiring(monkeypatch, spy: _WiringSpy) -> None:
    from machina.controller import wiring

    for name in (
        "record_triage",
        "log_complexity",
        "log_section_tokens",
        "check_stagnation",
        "stagnation_content",
        "record_stagnation_state",
        "record_approval_pause",
        "trace_tool_call",
    ):
        monkeypatch.setattr(wiring, name, getattr(spy, name), raising=False)


async def test_run_and_run_stream_call_the_same_wiring_helpers(monkeypatch):
    from machina.core.agent import ReActAgent
    from machina.tools.base import ToolRegistry

    reg = ToolRegistry()

    @reg.register(description="probe")
    async def probe_tool() -> str:
        return "probe-ok"

    session = _mock_session()
    tool_round = _tool_response("probe_tool", {})
    final = LLMResponse(content="done", model="t", usage={"total_tokens": 5})

    run_spy = _WiringSpy()
    with (
        patch("machina.core.agent.session_manager") as mock_sm,
        patch("machina.core.agent.context_manager") as mock_cm,
        patch("machina.core.agent.registry", reg),
    ):
        _run_mocks(mock_sm, mock_cm, session)
        _patch_wiring(monkeypatch, run_spy)
        provider = AsyncMock()
        provider.complete = AsyncMock(side_effect=[tool_round, final])
        response = await ReActAgent(provider=provider).run(session.id, "go", verbose=False)
    assert response.content == "done"

    stream_spy = _WiringSpy()
    provider2 = AsyncMock()
    provider2.stream_complete = _stream_once(tool_round, final)
    with (
        patch("machina.core.agent.session_manager") as mock_sm2,
        patch("machina.core.agent.context_manager") as mock_cm2,
        patch("machina.core.agent.registry", reg),
    ):
        _run_mocks(mock_sm2, mock_cm2, session)
        _patch_wiring(monkeypatch, stream_spy)
        events = [
            e
            async for e in ReActAgent(provider=provider2).run_stream(
                session.id, "go", verbose=False
            )
        ]
    assert events and events[-1].type == "done"

    assert run_spy.calls == stream_spy.calls, (run_spy.calls, stream_spy.calls)
    # The pinned hard requirements of the wave-4 wiring, for both paths:
    assert run_spy.calls["record_triage"] == 1
    assert run_spy.calls["log_complexity"] == 1
    assert run_spy.calls["log_section_tokens"] == 2  # two LLM calls in both loops
    assert run_spy.calls["check_stagnation"] == 1  # one tool batch
    assert run_spy.calls["trace_tool_call"] == 1  # one executed tool
    assert run_spy.calls["trace_finish"] == 1
