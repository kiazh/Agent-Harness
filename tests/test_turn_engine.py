"""Pins for the shared turn engine (W2 step 1, unwired).

The engine is not yet adopted by the transports; these pins lock the semantics
gateway ``_run_turn`` and the SSE route will delegate to: exactly-once withheld
tail flush, tool start/complete pairing and truncation, token accounting,
needs_approval extraction, and the ordered bounded cleanup (stop renewal ->
shielded bounded release -> join renewal) against fakes.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from machina.core import turn_engine as turn_engine_module
from machina.core import turns
from machina.core.models import AgentResponse, StreamEvent
from machina.core.turn_engine import TurnEngine, TurnSpec

#: Complete OpenAI-shaped key (>= 20 body chars): the streaming redactor
#: withholds it from deltas and flush() replaces it whole.
_SECRET = "sk-abcdefghijklmnopqrstuvwx1234"
_REDACTED_SECRET = "[REDACTED_OPENAI_KEY]"


class _RecordingHooks:
    def __init__(self) -> None:
        self.stages: list[str] = []
        self.losses: list[str] = []

    def on_stage(self, stage: str) -> None:
        self.stages.append(stage)

    def on_ownership_lost(self, reason: str) -> None:
        self.losses.append(reason)


def _spec(**overrides) -> TurnSpec:
    base: dict = {
        "session_id": uuid.uuid4(),
        "turn_id": "turn-1",
        "token": "owner-token",
        "text": "hello",
    }
    base.update(overrides)
    return TurnSpec(**base)


def _scripted_agent(events: list[StreamEvent]):
    class _Scripted:
        async def run_stream(self, session_id, user_message, verbose=False, **kwargs):
            for event in events:
                yield event

    return _Scripted()


async def _drain(engine: TurnEngine) -> list:
    return [event async for event in engine.stream()]


# ─── relay: redaction tail ──────────────────────────────────────────────────


async def test_stream_requires_attached_agent():
    engine = TurnEngine(_spec(), boundary="test.turn_engine")
    with pytest.raises(RuntimeError):
        await _drain(engine)


async def test_flush_tail_redacts_withheld_secret_exactly_once():
    agent = _scripted_agent([StreamEvent(type="text", content=f"here {_SECRET}")])
    engine = TurnEngine(_spec(), boundary="test.turn_engine")
    engine.attach(agent)

    events = await _drain(engine)

    assert [(event.kind, event.payload) for event in events] == [("delta", {"text": "here "})]
    tail = engine.flush_tail()
    assert tail == _REDACTED_SECRET
    assert _SECRET not in tail
    assert engine.flush_tail() == ""


async def test_terminal_without_final_uses_withheld_tail_exactly_once():
    agent = _scripted_agent([StreamEvent(type="text", content=f"here {_SECRET}")])
    engine = TurnEngine(_spec(), boundary="test.turn_engine")
    engine.attach(agent)
    await _drain(engine)

    terminal = engine.build_terminal(cancelled=False)

    assert terminal["text"] == _REDACTED_SECRET
    assert terminal["cancelled"] is False
    assert engine.flush_tail() == ""


async def test_terminal_after_explicit_tail_flush_does_not_duplicate_tail():
    agent = _scripted_agent([StreamEvent(type="text", content=f"here {_SECRET}")])
    engine = TurnEngine(_spec(), boundary="test.turn_engine")
    engine.attach(agent)
    await _drain(engine)

    assert engine.flush_tail() == _REDACTED_SECRET
    terminal = engine.build_terminal(cancelled=True)

    assert terminal["text"] == ""
    assert terminal["cancelled"] is True


async def test_terminal_with_final_uses_redacted_content_and_leaves_tail_for_delta():
    agent = _scripted_agent(
        [
            StreamEvent(type="text", content=f"here {_SECRET}"),
            StreamEvent(
                type="done",
                response=AgentResponse(content=f"answer {_SECRET}", tokens_used=7, iterations=2),
            ),
        ]
    )
    engine = TurnEngine(_spec(), boundary="test.turn_engine")
    engine.attach(agent)
    await _drain(engine)

    terminal = engine.build_terminal(cancelled=False)

    assert terminal["text"] == f"answer {_REDACTED_SECRET}"
    assert _SECRET not in terminal["text"]
    # The withheld tail is still available as the final delta, exactly once.
    assert engine.flush_tail() == _REDACTED_SECRET
    assert engine.flush_tail() == ""


# ─── relay: tool pairing and truncation ─────────────────────────────────────


async def test_tool_results_pair_by_stable_id_out_of_order():
    agent = _scripted_agent(
        [
            StreamEvent(type="tool_call", tool_name="read_file", tool_call_id="c1"),
            StreamEvent(type="tool_call", tool_name="read_file", tool_call_id="c2"),
            StreamEvent(
                type="tool_result", tool_name="read_file", tool_call_id="c2", tool_result="second"
            ),
            StreamEvent(
                type="tool_result", tool_name="read_file", tool_call_id="c1", tool_result="first"
            ),
        ]
    )
    engine = TurnEngine(_spec(), boundary="test.turn_engine")
    engine.attach(agent)

    events = await _drain(engine)

    starts = [event for event in events if event.kind == "tool_start"]
    completes = [event for event in events if event.kind == "tool_complete"]
    assert [start.payload["id"] for start in starts] == ["c1", "c2"]
    assert [(done.payload["id"], done.payload["result"]) for done in completes] == [
        ("c2", "second"),
        ("c1", "first"),
    ]


async def test_tool_results_fall_back_to_fifo_same_name():
    agent = _scripted_agent(
        [
            StreamEvent(type="tool_call", tool_name="grep"),
            StreamEvent(type="tool_call", tool_name="grep"),
            StreamEvent(type="tool_result", tool_name="grep", tool_result="r1"),
            StreamEvent(type="tool_result", tool_name="grep", tool_result="r2"),
        ]
    )
    engine = TurnEngine(_spec(), boundary="test.turn_engine")
    engine.attach(agent)

    events = await _drain(engine)

    completes = [event for event in events if event.kind == "tool_complete"]
    assert [(done.payload["id"], done.payload["result"]) for done in completes] == [
        ("turn-1-1", "r1"),
        ("turn-1-2", "r2"),
    ]


async def test_tool_result_without_matching_call_uses_turn_scoped_fallback_id():
    agent = _scripted_agent([StreamEvent(type="tool_result", tool_name="mystery", tool_result="x")])
    engine = TurnEngine(_spec(), boundary="test.turn_engine")
    engine.attach(agent)

    events = await _drain(engine)

    completes = [event for event in events if event.kind == "tool_complete"]
    assert completes[0].payload["id"] == "turn-1-0"


async def test_tool_result_truncated_at_bound_after_redaction():
    agent = _scripted_agent(
        [
            StreamEvent(type="tool_call", tool_name="fetch", tool_call_id="c1"),
            StreamEvent(
                type="tool_result",
                tool_name="fetch",
                tool_call_id="c1",
                tool_result=f"{_SECRET} {'x' * 4500}",
            ),
            StreamEvent(type="tool_call", tool_name="fetch", tool_call_id="c2"),
            StreamEvent(
                type="tool_result", tool_name="fetch", tool_call_id="c2", tool_result="Error: boom"
            ),
        ]
    )
    engine = TurnEngine(_spec(), boundary="test.turn_engine")
    engine.attach(agent)

    events = await _drain(engine)

    completes = [event for event in events if event.kind == "tool_complete"]
    first = next(done for done in completes if done.payload["id"] == "c1")
    second = next(done for done in completes if done.payload["id"] == "c2")
    assert len(first.payload["result"]) == turn_engine_module.MAX_TOOL_RESULT_CHARS == 4000
    assert first.payload["truncated"] is True
    assert _SECRET not in first.payload["result"]
    assert second.payload["isError"] is True
    assert second.payload["truncated"] is False


async def test_tool_result_bound_is_configurable():
    agent = _scripted_agent(
        [
            StreamEvent(type="tool_call", tool_name="fetch", tool_call_id="c1"),
            StreamEvent(
                type="tool_result", tool_name="fetch", tool_call_id="c1", tool_result="y" * 11
            ),
        ]
    )
    engine = TurnEngine(_spec(), boundary="test.turn_engine", max_tool_result_chars=10)
    engine.attach(agent)

    events = await _drain(engine)

    complete = next(event for event in events if event.kind == "tool_complete")
    assert complete.payload["result"] == "y" * 10
    assert complete.payload["truncated"] is True


# ─── relay: token accounting and pauses ─────────────────────────────────────


async def test_token_accounting_prefers_final_and_falls_back_to_accumulated():
    agent = _scripted_agent(
        [
            StreamEvent(type="token_usage", tokens_used=10),
            StreamEvent(type="token_usage", tokens_used=5),
            StreamEvent(
                type="done",
                response=AgentResponse(
                    content="ok",
                    tokens_used=42,
                    iterations=3,
                    tool_calls=[{"id": "a"}, {"id": "b"}],
                ),
            ),
        ]
    )
    engine = TurnEngine(_spec(), boundary="test.turn_engine")
    engine.attach(agent)

    events = await _drain(engine)

    assert [event.payload["tokens"] for event in events if event.kind == "usage"] == [10, 15]
    # The relay surfaces the stream's terminal marker (transports capture
    # their final snapshot at exactly this point).
    assert events[-1].kind == "done"
    terminal = engine.build_terminal(cancelled=False)
    assert terminal["tokens"] == 42
    assert terminal["iterations"] == 3
    assert terminal["toolCalls"] == 2
    assert engine.tokens == 15

    zero_usage = _scripted_agent(
        [
            StreamEvent(type="token_usage", tokens_used=15),
            StreamEvent(type="done", response=AgentResponse(content="ok", tokens_used=0)),
        ]
    )
    engine_zero = TurnEngine(_spec(), boundary="test.turn_engine")
    engine_zero.attach(zero_usage)
    await _drain(engine_zero)
    assert engine_zero.build_terminal(cancelled=False)["tokens"] == 15

    no_final = _scripted_agent([StreamEvent(type="token_usage", tokens_used=7)])
    engine_none = TurnEngine(_spec(), boundary="test.turn_engine")
    engine_none.attach(no_final)
    await _drain(engine_none)
    terminal_none = engine_none.build_terminal(cancelled=False)
    assert terminal_none["tokens"] == 7
    assert terminal_none["iterations"] == 0
    assert terminal_none["toolCalls"] == 0


async def test_needs_approval_extraction_filters_invalid_entries():
    final = AgentResponse(
        content="paused",
        needs_approval=[
            {
                "request_id": "r1",
                "operation": "file.write",
                "target": "/tmp/x",
                "status": "pending",
                "session_id": "s1",
            },
            {"operation": "missing-id"},
            "junk",
            42,
        ],
    )
    agent = _scripted_agent([StreamEvent(type="done", response=final)])
    engine = TurnEngine(_spec(), boundary="test.turn_engine")
    engine.attach(agent)
    await _drain(engine)

    pauses = engine.pauses

    assert len(pauses) == 1
    assert pauses[0]["requestId"] == "r1"
    assert pauses[0]["operation"] == "file.write"
    assert pauses[0]["sessionId"] == "s1"
    assert engine.build_terminal(cancelled=False)["needsApproval"] == pauses


async def test_terminal_without_pauses_omits_needs_approval():
    agent = _scripted_agent([StreamEvent(type="done", response=AgentResponse(content="done"))])
    engine = TurnEngine(_spec(), boundary="test.turn_engine")
    engine.attach(agent)
    await _drain(engine)

    assert "needsApproval" not in engine.build_terminal(cancelled=False)


# ─── cleanup: ordering and failure boundaries ───────────────────────────────


async def test_close_stops_renewal_before_shielded_release_and_joins(monkeypatch):
    order: list[str] = []
    renew_entered = asyncio.Event()
    end_calls: list[str] = []
    cancels_requested_at_release: list[int] = []

    async def fake_renew(session_id, token):
        order.append("renew")
        renew_entered.set()
        await asyncio.Event().wait()
        return True

    async def fake_end(session_id, token):
        # Stop-before-release: the renewal task must already be cancel-requested
        # when the shielded, bounded release runs.
        cancels_requested_at_release.append(renew_task.cancelling())
        end_calls.append(token)
        return True

    monkeypatch.setattr(turns, "renew_turn", fake_renew)
    monkeypatch.setattr(turns, "end_turn", fake_end)

    engine = TurnEngine(_spec(), boundary="test.close", renew_interval_s=0.001)
    renew_task = engine.start_renewal()
    assert renew_task is not None
    await asyncio.wait_for(renew_entered.wait(), 1)

    report = await asyncio.wait_for(engine.close(cancelled=False), 2)

    assert "renew" in order
    assert cancels_requested_at_release and cancels_requested_at_release[0] >= 1
    assert end_calls == ["owner-token"]
    assert report.release_failed is False
    assert renew_task.done() and renew_task.cancelled()
    renewals_before = order.count("renew")
    await asyncio.sleep(0.02)
    assert order.count("renew") == renewals_before  # renewal stopped after close


async def test_close_bounded_cleanup_order_and_stages(monkeypatch):
    order: list[str] = []
    join_calls: list[tuple] = []

    async def fake_join(agent, timeout=0.0, settle_grace=0.0):
        order.append("join")
        join_calls.append((timeout, settle_grace))
        return 0

    async def fake_close_provider(agent):
        order.append("close_provider")

    async def fake_end(session_id, token):
        order.append("release")
        return True

    monkeypatch.setattr(turn_engine_module, "join_agent_background_tasks", fake_join)
    monkeypatch.setattr("machina.core.agent_factory.close_agent_provider", fake_close_provider)
    monkeypatch.setattr(turns, "end_turn", fake_end)

    hooks = _RecordingHooks()
    engine = TurnEngine(_spec(), hooks, boundary="test.close")
    engine.attach(object())

    report = await engine.close(cancelled=True)

    assert order == ["join", "close_provider", "release"]
    assert join_calls == [(1.7, 0.35)]
    assert report.release_failed is False and report.cancelled is True
    assert hooks.stages == [
        "cleanup_started",
        "joining_optional_work",
        "closing_owned_clients",
        "releasing_ownership",
    ]


async def test_close_reports_ordinary_failures_without_raising(monkeypatch):
    recorded: list[str] = []

    async def failing_join(agent, timeout=0.0, settle_grace=0.0):
        raise RuntimeError("join boom")

    async def failing_close_provider(agent):
        raise RuntimeError("provider boom")

    async def failing_end(session_id, token):
        raise ConnectionError("db offline")

    monkeypatch.setattr(turn_engine_module, "join_agent_background_tasks", failing_join)
    monkeypatch.setattr(turn_engine_module, "record_failure", lambda op, exc: recorded.append(op))
    monkeypatch.setattr("machina.core.agent_factory.close_agent_provider", failing_close_provider)
    monkeypatch.setattr(turns, "end_turn", failing_end)

    engine = TurnEngine(_spec(), boundary="test.boundary")
    engine.attach(object())

    report = await engine.close()

    assert report.release_failed is True
    assert "test.boundary.join_optional" in recorded
    assert "test.boundary.release" in recorded


async def test_close_consumes_mid_release_cancellation(monkeypatch):
    release_started, release_gate = asyncio.Event(), asyncio.Event()
    end_calls: list[str] = []

    async def slow_end(session_id, token):
        end_calls.append(token)
        release_started.set()
        await release_gate.wait()
        return True

    monkeypatch.setattr(turns, "end_turn", slow_end)

    engine = TurnEngine(_spec(), boundary="test.cancel")
    task = asyncio.create_task(engine.close())
    try:
        await asyncio.wait_for(release_started.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)  # deliver the cancellation mid-release
        release_gate.set()
        report = await asyncio.wait_for(task, 2)

        assert end_calls == ["owner-token"]  # release settled exactly once
        assert report.release_failed is False
        assert not task.cancelled()  # RF4-C1: consumed, not propagated
    finally:
        release_gate.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_cancellation_during_optional_join_still_releases_and_propagates(monkeypatch):
    join_started = asyncio.Event()
    end_calls: list[str] = []

    async def blocking_join(agent, timeout=0.0, settle_grace=0.0):
        join_started.set()
        await asyncio.Event().wait()

    async def fake_end(session_id, token):
        end_calls.append(token)
        return True

    monkeypatch.setattr(turn_engine_module, "join_agent_background_tasks", blocking_join)
    monkeypatch.setattr(turns, "end_turn", fake_end)

    engine = TurnEngine(_spec(), boundary="test.cancel")
    engine.attach(object())
    task = asyncio.create_task(engine.close())
    try:
        await asyncio.wait_for(join_started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert end_calls == ["owner-token"]  # release still ran before propagating
        assert task.cancelled()
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


# ─── compaction and resume ──────────────────────────────────────────────────


async def test_schedule_auto_compact_is_fire_and_forget(monkeypatch):
    compacted: list = []

    async def fake_compact(session_id):
        compacted.append(session_id)

    monkeypatch.setattr("machina.services.maybe_auto_compact", fake_compact)
    spec = _spec()
    engine = TurnEngine(spec, boundary="test.compact")

    engine.schedule_auto_compact()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert compacted == [spec.session_id]


async def test_needs_approval_stream_events_surface_as_semantic_events():
    agent = _scripted_agent(
        [
            StreamEvent(
                type="needs_approval",
                content="r1",
                tool_name="process.exec",
                tool_args={"requestId": "r1", "status": "pending", "target": "x"},
            )
        ]
    )
    engine = TurnEngine(_spec(), boundary="test.turn_engine")
    engine.attach(agent)

    events = await _drain(engine)

    assert [(event.kind, event.payload) for event in events] == [
        (
            "needs_approval",
            {
                "request_id": "r1",
                "operation": "process.exec",
                "proposal": {"requestId": "r1", "status": "pending", "target": "x"},
            },
        )
    ]


async def test_start_renewal_adopts_transport_loss_event(monkeypatch):
    custom = asyncio.Event()
    entered = asyncio.Event()

    async def fake_renew(session_id, token):
        entered.set()
        return False

    monkeypatch.setattr(turns, "renew_turn", fake_renew)
    hooks = _RecordingHooks()
    engine = TurnEngine(_spec(), hooks, boundary="test.renew", renew_interval_s=0.001)

    task = engine.start_renewal(ownership_lost=custom)

    assert task is not None
    await asyncio.wait_for(entered.wait(), 1)
    for _ in range(100):
        if custom.is_set():
            break
        await asyncio.sleep(0.01)
    assert custom.is_set()
    assert engine.ownership_lost is custom
    assert hooks.losses == ["claim_renewal_failed"]
    await asyncio.wait_for(task, 1)


async def test_resume_stream_emits_approval_resumed_and_reads_caller_context(monkeypatch):
    from machina.permissions.broker import resume_approval_var

    seen: dict = {}

    async def fake_resume(agent, session_id, record, arguments):
        seen["request_id"] = resume_approval_var.get()
        seen["arguments"] = arguments
        yield StreamEvent(type="done", response=AgentResponse(content="resumed"))

    monkeypatch.setattr("machina.core.approval_resume.resume_action_stream", fake_resume)
    record = {"request_id": "req-9", "turn_id": "origin-turn"}
    before = resume_approval_var.get()
    # The transport owns the contextvar: it must be set in the context that
    # drives the stream (each SSE step runs in its own task copy).
    token = resume_approval_var.set("req-9")
    try:
        engine = TurnEngine(_spec(resume=(record, {"path": "x"})), boundary="test.resume")
        engine.attach(object())
        events = await _drain(engine)
    finally:
        resume_approval_var.reset(token)

    assert events[0].kind == "approval_resumed"
    assert events[0].payload == {"request_id": "req-9", "origin_turn_id": "origin-turn"}
    assert seen == {"request_id": "req-9", "arguments": {"path": "x"}}
    assert resume_approval_var.get() == before
