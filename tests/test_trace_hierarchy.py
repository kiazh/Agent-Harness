"""Hierarchical run tracing (S2): parenting, redaction, ring cap, reconciliation."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from machina.core.models import AgentResponse, StreamEvent
from machina.observability import traces
from machina.observability.traces import (
    MAX_SPANS_PER_TURN,
    approval_span,
    cancel_turn,
    current_turn_id,
    delegation_span,
    end_turn,
    get_turn,
    llm_span,
    note_finish,
    note_reserve,
    render_diagnostics,
    set_turn_attributes,
    start_turn,
    tool_span,
    turn_report,
)


@pytest.fixture(autouse=True)
def _isolated_traces():
    traces.reset_for_tests()
    yield
    traces.reset_for_tests()


def _span_attrs(turn_id):
    turn = get_turn(turn_id)
    return [s.attributes for s in turn.spans]


# ─── parenting / nesting ────────────────────────────────────────────────────


def test_span_parenting_and_nesting():
    start_turn("t-parent", session_id="s1", agent_id="a1", task_class="implement")
    with llm_span("m1") as llm:
        with tool_span("read_file", {"path": "a.py"}) as tool:
            pass
    with delegation_span("child-9", child_agent="worker") as deleg:
        deleg.set(status="ok")
    with approval_span("req-1"):
        pass
    end_turn("t-parent", status="ok", stop_reason="completed")

    turn = get_turn("t-parent")
    assert turn is not None and turn.root is not None
    assert turn.root.kind == "turn"
    assert turn.status == "ok" and turn.stop_reason == "completed"
    by_kind = {}
    for span in turn.spans[1:]:
        by_kind.setdefault(span.kind, []).append(span)
    assert set(by_kind) == {"llm", "tool", "delegation", "approval"}
    assert by_kind["tool"][0].parent_id == llm.id
    assert by_kind["llm"][0].parent_id == turn.root.id
    assert by_kind["delegation"][0].parent_id == turn.root.id
    assert by_kind["approval"][0].parent_id == turn.root.id
    assert tool.attributes["tool"] == "read_file"
    assert "a.py" in tool.attributes["args_preview"]
    assert by_kind["approval"][0].attributes["waited_ms"] >= 0


def test_tool_outcome_and_error_body_status():
    start_turn("t-out")
    with tool_span("ok_tool", {}) as s:
        s.set(outcome="ok")
    with tool_span("bad_tool", {}) as s:
        s.set(outcome="error")
        s.status = "error"
    with pytest.raises(RuntimeError):
        with tool_span("boom_tool", {}):
            raise RuntimeError("x")
    end_turn("t-out")
    report = turn_report("t-out")
    assert report["tool_calls"] == {"total": 3, "by_outcome": {"ok": 1, "error": 2}}


# ─── redaction ──────────────────────────────────────────────────────────────


_CANARY = "C4n4ry"
_OPENAI_KEY = "sk-C4n4ryK3yX9qW2mZ7tYvB8sL6Q"
_BEARER = "Bearer C4n4ryBearer.Token_99382-xyz"
_PASSWORD = "password=C4n4ryPwd99382Zulu"
_DSN = "postgres://bob:C4n4ryDbPwd99@db.internal/app"


def test_secret_canaries_never_appear_in_spans_or_diagnostics():
    start_turn("t-red", session_id="s1")
    args = {
        "api_key": _OPENAI_KEY,
        "auth": _BEARER,
        "note": _PASSWORD,
        "dsn": _DSN,
        "nested": {"k": _OPENAI_KEY},
    }
    with tool_span("leaky_tool", args):
        pass
    with llm_span("m"):
        pass
    end_turn("t-red", status="ok", stop_reason="completed")

    for attrs in _span_attrs("t-red"):
        assert _CANARY not in repr(attrs)
    assert _CANARY not in render_diagnostics("t-red")
    assert _CANARY not in repr(turn_report("t-red"))


# ─── ring cap ───────────────────────────────────────────────────────────────


def test_ring_cap_drops_oldest_with_marker():
    start_turn("t-ring")
    for i in range(MAX_SPANS_PER_TURN - 1 + 20):
        with tool_span(f"tool-{i}", {}):
            pass
    end_turn("t-ring")
    turn = get_turn("t-ring")
    assert len(turn.spans) == MAX_SPANS_PER_TURN
    assert turn.dropped_spans == 20
    assert turn.root.kind == "turn"  # root never evicted
    assert turn.root.attributes["dropped_spans"] == 20
    report = turn_report("t-ring")
    assert report["dropped_spans"] == 20
    assert report["tool_calls"]["total"] == MAX_SPANS_PER_TURN - 1


# ─── reconciliation against the real usage path ─────────────────────────────


class _FakeTx:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, outer):
        self._outer = outer

    def transaction(self):
        return _FakeTx()

    async def fetchrow(self, sql, *args):
        if sql.strip().upper().startswith("INSERT"):
            rid = uuid.uuid4()
            # INSERT args: session, agent, provider, model, reserved, ancestors...
            self._outer.reserved[rid] = args[4]
            return {"id": rid}
        return None  # lineage lookup

    async def execute(self, *args):
        return "SELECT 1"  # advisory locks

    async def fetch(self, *args):
        return []  # scope aggregates (no limits configured)


class _FakeAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakeUsageDb:
    connected = True

    def __init__(self):
        self.reserved = {}
        self.finishes = []

    def acquire(self):
        return _FakeAcquire(_FakeConn(self))

    async def execute(self, sql, *args):
        # UPDATE args: reservation_id, status, prompt, completion, accounted, model
        self.finishes.append(
            {
                "reservation_id": args[0],
                "status": args[1],
                "prompt": args[2],
                "completion": args[3],
                "accounted": args[4],
            }
        )
        return "UPDATE 1"


async def _fixture_run(store, session_id, agent_id):
    r1 = await store.reserve(
        session_id, agent_id, "fake", "m", [{"role": "user", "content": "hi"}], [], 8
    )
    await store.finish(r1, {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14})
    r2 = await store.reserve(
        session_id, agent_id, "fake", "m", [{"role": "user", "content": "hi"}], [], 8
    )
    await store.finish(r2, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 9})
    r3 = await store.reserve(
        session_id, agent_id, "fake", "m", [{"role": "user", "content": "hi"}], [], 8
    )
    await store.finish(
        r3, {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10, "estimated": True}
    )
    r4 = await store.reserve(
        session_id, agent_id, "fake", "m", [{"role": "user", "content": "hi"}], [], 8
    )
    await store.finish(r4, failed=True)
    r5 = await store.reserve(
        session_id, agent_id, "fake", "m", [{"role": "user", "content": "hi"}], [], 8
    )
    await store.finish(r5, {})


async def test_reconciliation_equals_usage_ledger_figures(monkeypatch):
    from machina.core.usage import UsageStore

    fake_db = _FakeUsageDb()
    monkeypatch.setattr("machina.core.usage.db", fake_db)
    store = UsageStore()
    session_id = uuid.uuid4()

    start_turn("t-rec", session_id=str(session_id), agent_id="worker")
    await _fixture_run(store, session_id, "worker")
    end_turn("t-rec", status="ok", stop_reason="completed")

    # Expected figures from the exact SQL params the ledger persists
    # (COALESCE fallback to the reservation mirrors the SQL for unknowns).
    exp_in = exp_out = exp_acc = exp_unknown = 0
    for fin in fake_db.finishes:
        known = fin["prompt"] is not None and fin["completion"] is not None
        exp_in += fin["prompt"] or 0
        exp_out += fin["completion"] or 0
        exp_acc += (
            fin["accounted"]
            if fin["accounted"] is not None
            else fake_db.reserved[fin["reservation_id"]]
        )
        exp_unknown += 0 if known else 1

    report = turn_report("t-rec")
    assert report["llm_calls"] == 5 == len(fake_db.finishes)
    assert report["input_tokens"] == exp_in == 10
    assert report["output_tokens"] == exp_out == 4
    assert (
        report["accounted_tokens"]
        == exp_acc
        == 14 + 9 + sum(fake_db.reserved[f["reservation_id"]] for f in fake_db.finishes[2:])
    )
    assert report["unknown_calls"] == exp_unknown == 3


async def test_usage_hooks_noop_without_reservation_or_db(monkeypatch):
    from machina.core.usage import UsageStore

    fake_db = _FakeUsageDb()
    fake_db.connected = False
    monkeypatch.setattr("machina.core.usage.db", fake_db)
    store = UsageStore()
    session_id = uuid.uuid4()

    start_turn("t-noop")
    assert await store.reserve(session_id, "a", "fake", "m", [], [], 8) is None
    assert await store.finish(None, {"prompt_tokens": 1, "completion_tokens": 1}) is False
    assert note_finish(uuid.uuid4(), prompt_tokens=1, completion_tokens=1) is False
    assert note_reserve(uuid.uuid4(), model="m") is not None  # active turn, no reservation needed
    end_turn("t-noop")
    report = turn_report("t-noop")
    assert report["llm_calls"] == 0  # dangling span swept, nothing finished


# ─── nesting / cancellation / isolation ─────────────────────────────────────


def test_nested_turn_delegation_does_not_corrupt_parent():
    start_turn("t-p", session_id="parent-sess", agent_id="boss")
    with delegation_span("child-sess", child_agent="worker") as deleg:
        start_turn("t-c", session_id="child-sess", agent_id="worker")
        rid = uuid.uuid4()
        assert note_reserve(rid, model="m", reserved_tokens=50) is not None
        assert note_finish(rid, prompt_tokens=5, completion_tokens=6, accounted_tokens=11) is True
        assert current_turn_id() == "t-c"
        end_turn("t-c", status="ok", stop_reason="completed")
        deleg.set(status="ok")
    assert current_turn_id() == "t-p"
    with tool_span("parent_tool", {}):
        pass
    end_turn("t-p", status="ok", stop_reason="completed")

    parent = turn_report("t-p")
    child = turn_report("t-c")
    assert parent["llm_calls"] == 0
    assert child["llm_calls"] == 1
    assert child["input_tokens"] == 5 and child["output_tokens"] == 6
    assert parent["delegations"] == {"total": 1, "by_status": {"ok": 1}}
    assert parent["tool_calls"]["total"] == 1
    # Parent stack intact: tool span parented to the parent root.
    parent_turn = get_turn("t-p")
    tool = next(s for s in parent_turn.spans if s.kind == "tool")
    assert tool.parent_id == parent_turn.root.id


def test_cancel_midspan_marks_cancelled():
    start_turn("t-cancel", session_id="s")
    with llm_span("m") as llm:
        with tool_span("t", {}):
            cancel_turn("t-cancel")
    end_turn("t-cancel", status="ok", stop_reason="x")  # no-op after cancel
    turn = get_turn("t-cancel")
    assert turn.status == "cancelled" and turn.stop_reason == "cancelled"
    assert llm.status == "cancelled"
    assert turn_report("t-cancel")["status"] == "cancelled"


async def test_concurrent_sessions_have_no_crosstalk():
    async def worker(tid, sess, names):
        start_turn(tid, session_id=sess, agent_id="a")
        for name in names:
            with tool_span(name, {}):
                await asyncio.sleep(0)
        end_turn(tid, status="ok", stop_reason="done")

    await asyncio.gather(
        worker("t-a", "sess-a", ["a1", "a2"]),
        worker("t-b", "sess-b", ["b1", "b2", "b3"]),
    )
    ra, rb = turn_report("t-a"), turn_report("t-b")
    assert ra["session_id"] == "sess-a" and ra["tool_calls"]["total"] == 2
    assert rb["session_id"] == "sess-b" and rb["tool_calls"]["total"] == 3
    names_a = {s.name for s in get_turn("t-a").spans if s.kind == "tool"}
    names_b = {s.name for s in get_turn("t-b").spans if s.kind == "tool"}
    assert names_a == {"a1", "a2"} and names_b == {"b1", "b2", "b3"}


def test_turn_attributes_and_unknown_turn():
    start_turn("t-attr")
    set_turn_attributes("t-attr", agent_id="g", task_class="debug")
    assert get_turn("t-attr").agent_id == "g"
    assert turn_report("t-attr")["task_class"] == "debug"
    assert turn_report("missing") is None
    assert render_diagnostics("missing") == "trace missing: no data"


def test_diagnostics_bounded_and_secret_free():
    start_turn("t-diag", session_id="s", agent_id="a")
    with tool_span("t", {"api_key": _OPENAI_KEY}):
        pass
    end_turn("t-diag", status="ok", stop_reason="completed")
    text = render_diagnostics("t-diag", max_chars=200)
    assert len(text) <= 200
    assert _CANARY not in text
    assert "llm:" in text and "completed" in text


# ─── failure isolation ──────────────────────────────────────────────────────


def test_raising_sink_never_breaks_tracing():
    def boom(event, payload):
        raise RuntimeError("sink down")

    traces.add_sink(boom)
    try:
        start_turn("t-sink", session_id="s")
        with tool_span("t", {"x": 1}) as s:
            s.set(outcome="ok")
        with pytest.raises(ValueError):
            with llm_span("m"):
                raise ValueError("body fails")
        end_turn("t-sink", status="ok", stop_reason="done")
    finally:
        traces.remove_sink(boom)
    report = turn_report("t-sink")
    assert report["tool_calls"]["total"] == 1
    assert report["status"] == "ok"


def test_tracing_disabled_is_noop():
    traces.set_enabled(False)
    try:
        assert start_turn("t-off") is None
        with tool_span("t", {}) as s:
            assert s is None
        assert note_reserve(uuid.uuid4(), model="m") is None
        assert note_finish(uuid.uuid4(), prompt_tokens=1, completion_tokens=1) is False
        end_turn("t-off")
        assert get_turn("t-off") is None
    finally:
        traces.set_enabled(True)


def _scripted_events():
    return [
        StreamEvent(type="text", content="Looking "),
        StreamEvent(type="tool_call", tool_name="read_file", tool_args={"path": "a.py"}),
        StreamEvent(type="tool_result", tool_name="read_file", tool_result="print('hi')"),
        StreamEvent(type="token_usage", tokens_used=42),
        StreamEvent(type="text", content="done."),
        StreamEvent(
            type="done",
            response=AgentResponse(
                content="Looking done.",
                tool_calls=[{"tool": "read_file"}],
                tokens_used=42,
                iterations=2,
            ),
        ),
    ]


class _FakeAgent:
    def __init__(self, events):
        self.events = events

    async def run_stream(self, session_id, user_message, verbose=False, **kwargs):
        for event in self.events:
            yield event


def _completes(frames):
    return [
        f["params"]
        for f in frames
        if f.get("method") == "event" and f["params"].get("type") == "message.complete"
    ]


async def _run_gateway_turn(monkeypatch, frames, turn_id, agent, text="read a.py"):
    from machina.gateway.server import Gateway

    session_id = uuid.uuid4()
    monkeypatch.setattr(
        "machina.gateway.server.session_manager.get_fresh",
        AsyncMock(return_value=SimpleNamespace(agent_id="harness", model="m", provider="p")),
    )
    monkeypatch.setattr(
        "machina.core.agent_factory.close_agent_provider", AsyncMock(return_value=None)
    )
    monkeypatch.setattr("machina.services.maybe_auto_compact", AsyncMock(return_value=None))
    gateway = Gateway(frames.append, agent_factory=lambda model, provider: agent)
    await gateway._run_turn(session_id, turn_id, text)
    return session_id


async def test_gateway_turn_traced_and_completes_with_raising_sink(monkeypatch):
    from machina.core.intent import classify_task

    frames = []
    session_id = await _run_gateway_turn(
        monkeypatch, frames, "gw-t1", _FakeAgent(_scripted_events())
    )
    assert len(_completes(frames)) == 1
    assert _completes(frames)[0]["text"] == "Looking done."

    report = turn_report("gw-t1")
    assert report is not None
    assert report["status"] == "ok" and report["stop_reason"] == "completed"
    assert report["session_id"] == str(session_id)
    assert report["agent_id"] == "harness"
    assert report["task_class"] == classify_task("read a.py")

    # A failing span backend must not break the turn.
    def boom(event, payload):
        raise RuntimeError("sink down")

    traces.add_sink(boom)
    try:
        frames2 = []
        await _run_gateway_turn(monkeypatch, frames2, "gw-t2", _FakeAgent(_scripted_events()))
        assert len(_completes(frames2)) == 1
        assert turn_report("gw-t2")["status"] == "ok"
    finally:
        traces.remove_sink(boom)


async def test_gateway_turn_completes_with_tracing_disabled(monkeypatch):
    traces.set_enabled(False)
    try:
        frames = []
        await _run_gateway_turn(monkeypatch, frames, "gw-off", _FakeAgent(_scripted_events()))
        assert len(_completes(frames)) == 1
        assert get_turn("gw-off") is None
    finally:
        traces.set_enabled(True)


async def test_gateway_cancel_marks_trace_cancelled(monkeypatch):
    gate = asyncio.Event()

    class _BlockedAgent(_FakeAgent):
        async def run_stream(self, session_id, user_message, verbose=False, **kwargs):
            await gate.wait()
            for event in self.events:
                yield event

    frames = []
    from machina.gateway.server import Gateway

    session_id = uuid.uuid4()
    monkeypatch.setattr(
        "machina.gateway.server.session_manager.get_fresh",
        AsyncMock(return_value=SimpleNamespace(agent_id="harness", model="m", provider="p")),
    )
    monkeypatch.setattr(
        "machina.core.agent_factory.close_agent_provider", AsyncMock(return_value=None)
    )
    monkeypatch.setattr("machina.services.maybe_auto_compact", AsyncMock(return_value=None))
    gateway = Gateway(frames.append, agent_factory=lambda model, provider: _BlockedAgent([]))
    task = asyncio.create_task(gateway._run_turn(session_id, "gw-cancel", "wait"))
    await asyncio.sleep(0.1)
    task.cancel()
    await task
    assert len(_completes(frames)) == 1
    assert _completes(frames)[0]["cancelled"] is True
    assert turn_report("gw-cancel")["status"] == "cancelled"
