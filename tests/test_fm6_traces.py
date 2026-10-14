"""FM6 F7/F10/late-finish — diagnostics bound, ledger cap, reservation-map cap."""

from __future__ import annotations

import uuid

import pytest

from machina.observability import traces
from machina.observability.traces import (
    end_turn,
    get_turn,
    note_finish,
    note_reserve,
    render_diagnostics,
    start_turn,
    tool_span,
    turn_report,
)


@pytest.fixture(autouse=True)
def _isolated_traces():
    traces.reset_for_tests()
    yield
    traces.reset_for_tests()


# ─── F7: diagnostics must honor tiny bounds exactly ─────────────────────────


def test_render_diagnostics_honors_bounds_below_marker_length():
    start_turn("t-small", session_id="s", agent_id="a")
    with tool_span("read_file", {"path": "x.py"}):
        pass
    end_turn("t-small", status="ok", stop_reason="completed")
    for max_chars in range(0, 12):
        rendered = render_diagnostics("t-small", max_chars=max_chars)
        assert len(rendered) <= max_chars, (max_chars, rendered)
    assert len(render_diagnostics("t-small", max_chars=200)) <= 200


# ─── F10: the per-turn usage ledger is bounded like the span ring ───────────


def test_usage_ledger_is_capped_with_dropped_marker():
    start_turn("t-ledger")
    for _ in range(traces.MAX_LEDGER_ENTRIES_PER_TURN + 20):
        reservation = uuid.uuid4()
        note_reserve(reservation, model="m", reserved_tokens=5)
        note_finish(reservation, prompt_tokens=3, completion_tokens=2, accounted_tokens=5)
    turn = get_turn("t-ledger")
    assert len(turn.ledger) == traces.MAX_LEDGER_ENTRIES_PER_TURN
    assert turn.ledger_dropped == 20
    report = turn_report("t-ledger")
    assert report["llm_calls"] == traces.MAX_LEDGER_ENTRIES_PER_TURN
    assert report["ledger_dropped"] == 20
    end_turn("t-ledger")


def test_usage_ledger_below_cap_keeps_dropped_zero():
    start_turn("t-ledger-small")
    reservation = uuid.uuid4()
    note_reserve(reservation, model="m")
    note_finish(reservation, prompt_tokens=1, completion_tokens=1, accounted_tokens=2)
    report = turn_report("t-ledger-small")
    assert report["llm_calls"] == 1
    assert report.get("ledger_dropped", 0) == 0
    end_turn("t-ledger-small")


# ─── late-finish companion: the reservation→turn map is bounded ─────────────


def test_reservation_map_is_bounded_and_drops_counted():
    start_turn("t-reservations")
    for index in range(traces.MAX_RESERVATIONS + 64):
        note_reserve(f"reservation-{index}", model="m", reserved_tokens=1)
    assert len(traces._RESERVATIONS) == traces.MAX_RESERVATIONS
    assert traces.dropped_reservations() == 64
    end_turn("t-reservations")


def test_reservation_drop_counter_resets_for_tests():
    assert traces.dropped_reservations() == 0


# ─── body exceptions propagate through the no-turn fast path ────────────────


def test_child_span_body_error_without_turn_propagates_unmasked():
    """Audit fix: a body exception on the no-turn path must surface as itself,
    not as ``RuntimeError: generator didn't stop after throw()``."""
    with pytest.raises(ValueError, match="original body error") as error:
        with traces._child_span("audit-kind", "audit-name", {}):
            raise ValueError("original body error")
    assert type(error.value) is ValueError


def test_tool_span_body_error_without_turn_propagates_unmasked():
    with pytest.raises(ValueError, match="tool body boom"):
        with tool_span("read_file", {}):
            raise ValueError("tool body boom")
