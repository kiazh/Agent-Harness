"""Final-review batch 3 (2026-10-15) gateway / observability / memory fix evidence.

RED-first regression tests for three confirmed findings:

* P10 — HTTP SSE v2 ``tool.complete.isError`` is always ``false`` even when the
  engine flags a failed tool. The fix forwards the real flag through the SSE
  route to the canonical wire event; the v1 compatibility shape is untouched.
* P6 — ``render_diagnostics`` omits the ``ledger_dropped`` overflow marker that
  ``turn_report`` returns, so a capped usage ledger silently understates
  ``llm_calls`` / token totals.
* N4 — ``StreamingSecretRedactor.flush()`` drops the withheld prefix that
  preceded an oversized unterminated PEM block. Fail-closed (no leak) but silent
  content loss; flush must emit the prefix (redacted) and never the body.

Each test is written to fail before the fix and pass after it.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from machina.core.models import StreamEvent
from machina.memory.redaction import MAX_BUFFERED_PEM, StreamingSecretRedactor
from machina.observability import traces
from machina.observability.traces import (
    end_turn,
    note_finish,
    note_reserve,
    render_diagnostics,
    start_turn,
)

# ─── P10: HTTP SSE v2 tool.complete isError pass-through ────────────────────


def _tool_result_event(
    *, result: str, name: str = "terminal", call_id: str = "c1"
) -> StreamEvent:
    return StreamEvent(
        type="tool_result",
        tool_name=name,
        tool_result=result,
        tool_call_id=call_id,
        tool_outcome="failed",
    )


def test_http_v2_tool_complete_forwards_iserror_flag():
    """The v2 canonical frame must carry the real failure flag, not a default."""
    from machina.api.app import _serialize_event

    failed = _serialize_event(
        _tool_result_event(result="Error: boom"),
        protocol_version=2,
        session_id="s",
        turn_id="t",
        tool_is_error=True,
    )
    ok = _serialize_event(
        _tool_result_event(result="fine", call_id="c2"),
        protocol_version=2,
        session_id="s",
        turn_id="t",
        tool_is_error=False,
    )
    assert failed["type"] == "tool.complete"
    assert failed["isError"] is True
    assert ok["isError"] is False
    from machina.protocol import validate_event

    validate_event(failed)
    validate_event(ok)


def test_http_v1_tool_complete_keeps_legacy_shape():
    """The compatibility path retains the earlier shape: no isError field."""
    from machina.api.app import _serialize_event

    data = _serialize_event(_tool_result_event(result="Error: boom"))
    assert data["type"] == "tool_result"
    assert "isError" not in data


class _FakeEngine:
    """Minimal TurnEngine stand-in for the SSE route branch under test."""

    def __init__(self, events):
        self._events = events
        self.final = None
        self.pauses: list = []
        self.tokens = 0
        self.tool_count = 0
        self.closed = False

    def start_renewal(self, *, ownership_lost) -> None:  # noqa: ANN001
        pass

    def attach(self, agent) -> None:  # noqa: ANN001
        pass

    async def stream(self):  # type: ignore[override]
        for event in self._events:
            yield event

    def flush_tail(self) -> str:
        return ""

    async def close(self, *, cancelled: bool):  # noqa: ANN001
        self.closed = True
        return SimpleNamespace(release_failed=False)

    def schedule_auto_compact(self) -> None:
        pass


def _tool_complete_engine() -> _FakeEngine:
    from machina.core.turn_engine import TurnEvent

    return _FakeEngine(
        [
            TurnEvent(
                "tool_complete",
                {
                    "id": "c1",
                    "name": "terminal",
                    "result": "Error: boom",
                    "isError": True,
                    "outcome": "failed",
                },
            )
        ]
    )


async def _drive_route_with_tool_complete(monkeypatch, *, protocol_version: int) -> list[dict]:
    """Drive the real SSE route with a fake engine yielding one tool_complete."""
    import json

    from machina.api.routes import streaming

    engine = _tool_complete_engine()
    monkeypatch.setattr(streaming, "TurnEngine", lambda *a, **k: engine)

    async def _fake_get_fresh(_sid):  # noqa: ANN001
        return SimpleNamespace(id=_sid, agent_id="a", allowed_tools=["terminal"])

    async def _fake_build(_session):  # noqa: ANN001
        return SimpleNamespace(agent_id="a")

    async def _fake_begin(*_a, **_k):  # noqa: ANN001
        return "owner-token"

    monkeypatch.setattr(streaming.session_manager, "get_fresh", _fake_get_fresh)
    monkeypatch.setattr("machina.core.agent_factory.build_agent_for_session", _fake_build)
    monkeypatch.setattr("machina.core.turns.try_begin_turn", _fake_begin)

    from machina.api.routes.models import PromptRequest

    req = PromptRequest(text="go", protocolVersion=protocol_version)
    response = await streaming._stream_session(str(uuid.uuid4()), req)
    frames = []
    async for chunk in response.body_iterator:
        if chunk.startswith("data: ") and chunk.strip() != "data: [DONE]":
            frames.append(json.loads(chunk[6:]))
    return frames


async def test_sse_route_v2_tool_complete_carries_real_iserror(monkeypatch):
    frames = await _drive_route_with_tool_complete(monkeypatch, protocol_version=2)
    complete = [f for f in frames if f.get("type") == "tool.complete"]
    assert complete, frames
    assert complete[0]["isError"] is True, complete[0]


async def test_sse_route_v1_tool_complete_omits_iserror(monkeypatch):
    frames = await _drive_route_with_tool_complete(monkeypatch, protocol_version=1)
    complete = [f for f in frames if f.get("type") == "tool_result"]
    assert complete, frames
    assert "isError" not in complete[0], complete[0]


# ─── P6: render_diagnostics surfaces the ledger overflow marker ─────────────


@pytest.fixture(autouse=True)
def _isolated_traces():
    traces.reset_for_tests()
    yield
    traces.reset_for_tests()


def test_render_diagnostics_marks_dropped_ledger_entries():
    start_turn("t-ledger-diag")
    for _ in range(traces.MAX_LEDGER_ENTRIES_PER_TURN + 20):
        reservation = uuid.uuid4()
        note_reserve(reservation, model="m", reserved_tokens=5)
        note_finish(reservation, prompt_tokens=3, completion_tokens=2, accounted_tokens=5)

    text = render_diagnostics("t-ledger-diag")
    llm_line = next(line for line in text.splitlines() if line.startswith("llm:"))
    assert "(+20 dropped)" in llm_line, text
    end_turn("t-ledger-diag", status="ok", stop_reason="completed")


def test_render_diagnostics_omits_marker_below_cap():
    start_turn("t-ledger-diag-small")
    reservation = uuid.uuid4()
    note_reserve(reservation, model="m")
    note_finish(reservation, prompt_tokens=1, completion_tokens=1, accounted_tokens=2)

    text = render_diagnostics("t-ledger-diag-small")
    llm_line = next(line for line in text.splitlines() if line.startswith("llm:"))
    assert "dropped" not in llm_line, text
    end_turn("t-ledger-diag-small", status="ok", stop_reason="completed")


# ─── N4: flush emits the withheld prefix of an oversized open PEM ───────────


def test_flush_emits_withheld_prefix_for_oversized_open_pem():
    """Flush must hand back the withheld prefix (redacted) and drop the body."""
    redactor = StreamingSecretRedactor()
    redactor._pem_open = True
    redactor._pem_prefix = "text before the block\n"
    redactor._buf = "RAWPRIVATEKEYBODYMUSTNOTLEAK"

    out = redactor.flush()
    assert "text before the block" in out
    assert "RAWPRIVATEKEYBODYMUSTNOTLEAK" not in out


def test_flush_prefix_is_redacted_when_it_contains_a_secret():
    redactor = StreamingSecretRedactor()
    redactor._pem_open = True
    redactor._pem_prefix = "token=sk-AbCd0123456789EfGhIjKlMnOpQrStUv\n"
    redactor._buf = "RAWPRIVATEKEYBODYMUSTNOTLEAK"

    out = redactor.flush()
    assert "sk-AbCd0123456789EfGhIjKlMnOpQrStUv" not in out
    assert "RAWPRIVATEKEYBODYMUSTNOTLEAK" not in out


def test_flush_emits_prefix_after_real_overflow(monkeypatch):
    """Natural overflow path: a configured credential lowers the frontier, so
    the oversized block carries a genuine withheld prefix that flush must not
    discard."""
    body = "\n".join(
        f"BODYLINE{i:04d}" + "A" * (64 - len(f"BODYLINE{i:04d}")) for i in range(600)
    )
    block = "-----BEGIN PRIVATE KEY-----\n" + body + "\n"
    assert len(block) > MAX_BUFFERED_PEM
    prefix = "ZZZZ"
    monkeypatch.setenv("MACHINA_FIXBATCH3_PASSWORD", prefix + block)

    redactor = StreamingSecretRedactor()
    emitted = redactor.feed(prefix + block)
    assert redactor._pem_open is True
    emitted += redactor.flush()

    assert "BODYLINE" not in emitted
    assert prefix in emitted, emitted[:200]
