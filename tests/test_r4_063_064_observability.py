"""R4-063/R4-064 — safe_logging structured redaction + trace-ring eviction markers.

R4-063: the log-record factory redacted only top-level string args (dict/list
args passed through verbatim) and called the redactor unguarded (a redactor
failure raised inside the logging call). Structured args are now walked
recursively and the redactor call fails closed to ``[REDACTED]``.

R4-064: the turn registry evicted turns with no marker and ``turn_report``
counted only retained spans, so ring overflow silently undercounted tool
calls. Evictions now carry markers (global counter, parent turn count,
``turn_evicted`` sink events) and the span ring exposes a dropped-tool delta.
"""

from __future__ import annotations

import logging

import pytest

from machina.observability import traces
from machina.observability.traces import (
    MAX_SPANS_PER_TURN,
    MAX_TURNS_RETAINED,
    end_turn,
    render_diagnostics,
    start_turn,
    tool_span,
    turn_report,
)

# OpenAI-style canary: matches the shipped redaction pattern exactly.
_KEY = "sk-R4OBS0123456789abcdef"
_SECRET = "api_key=" + "R4OBS_" + "9x8y7z6w5v4u3t2s1r0q"


@pytest.fixture(autouse=True)
def _isolated_traces():
    traces.reset_for_tests()
    yield
    traces.reset_for_tests()


# ─── R4-063: structured + guarded redaction ─────────────────────────────────


def test_structured_args_redacted_recursively(caplog):
    logger = logging.getLogger("machina.r4_obs.structured")

    with caplog.at_level(logging.WARNING, logger="machina.r4_obs.structured"):
        logger.warning(
            "payload %s",
            {
                "api_key": _KEY,
                "nested": [{"token": _KEY}, ("plain", _KEY), {"deep": [{"x": _KEY}]}],
                "count": 3,
            },
        )

    assert len(caplog.records) == 1
    record = caplog.records[0]
    rendered = record.getMessage()
    assert _KEY not in rendered
    assert "[REDACTED" in rendered
    assert _KEY not in repr(record.args)


def test_structured_kwargs_and_mapping_args_redacted(caplog):
    logger = logging.getLogger("machina.r4_obs.mapping")

    with caplog.at_level(logging.WARNING, logger="machina.r4_obs.mapping"):
        # %-style mapping args (record.args is a dict) take the same path.
        logger.warning("cfg %(user)s", {"user": _KEY})
        logger.warning("list %s", [_KEY, [_KEY]])

    assert len(caplog.records) == 2
    for record in caplog.records:
        assert _KEY not in record.getMessage()


def test_raising_redactor_masks_and_never_escapes(caplog, monkeypatch):
    import machina.memory.redaction as redaction

    def _boom(text):
        raise RuntimeError("redactor down")

    monkeypatch.setattr(redaction, "redact_secrets", _boom)
    logger = logging.getLogger("machina.r4_obs.raising")

    with caplog.at_level(logging.WARNING, logger="machina.r4_obs.raising"):
        # A raising redactor must not raise into the caller...
        logger.warning("secret=%s structured=%s", _SECRET, {"api_key": _KEY})

    assert len(caplog.records) == 1
    rendered = caplog.records[0].getMessage()
    # ...and must mask fail-closed rather than emit raw text.
    assert _SECRET not in rendered
    assert _KEY not in rendered
    assert rendered == "[REDACTED]"


def test_cyclic_structured_arg_masks_at_depth_bound(caplog):
    logger = logging.getLogger("machina.r4_obs.cyclic")
    cyclic: dict = {"api_key": _KEY}
    cyclic["self"] = cyclic

    with caplog.at_level(logging.WARNING, logger="machina.r4_obs.cyclic"):
        logger.warning("cyclic %s", cyclic)

    assert len(caplog.records) == 1
    rendered = caplog.records[0].getMessage()
    assert _KEY not in rendered  # secret redacted before the cycle bound
    assert "[REDACTED]" in rendered  # depth bound masks instead of walking


def test_non_machina_loggers_untouched(caplog):
    logger = logging.getLogger("other.r4_obs")

    with caplog.at_level(logging.WARNING, logger="other.r4_obs"):
        logger.warning("plain %s", {"api_key": _KEY})

    assert len(caplog.records) == 1
    assert _KEY in caplog.records[0].getMessage()  # namespace contract: untouched


# ─── R4-064: span-ring tool-call delta ──────────────────────────────────────


def test_span_ring_exposes_dropped_tool_call_delta():
    start_turn("t-tools")
    for index in range(MAX_SPANS_PER_TURN - 1 + 20):
        with tool_span(f"tool-{index}", {}):
            pass
    end_turn("t-tools")

    report = turn_report("t-tools")
    assert report["dropped_spans"] == 20
    assert report["dropped_tool_spans"] == 20
    # Retained total plus the explicit delta marks the undercount.
    assert report["tool_calls"]["total"] == MAX_SPANS_PER_TURN - 1
    assert report["tool_calls"]["total"] + report["dropped_tool_spans"] == 519

    text = render_diagnostics("t-tools")
    assert "(+20 dropped)" in text
    assert "tools: 499 (ok=499) (+20 dropped)" in text


def test_span_ring_tool_delta_counts_only_tool_spans():
    from machina.observability.traces import llm_span

    start_turn("t-mixed")
    for index in range(MAX_SPANS_PER_TURN - 1 + 10):
        if index % 2:
            with tool_span(f"tool-{index}", {}):
                pass
        else:
            with llm_span("m"):
                pass
    end_turn("t-mixed")
    report = turn_report("t-mixed")
    # The 10 evicted spans alternate llm/tool: only the tool half is the
    # tool-call undercount delta.
    assert report["dropped_spans"] == 10
    assert report["dropped_tool_spans"] == 5


# ─── R4-064: turn-ring eviction markers ─────────────────────────────────────


def test_turn_ring_keeps_inflight_parent_and_marks_evicted_children():
    start_turn("t-parent", session_id="parent-session")
    evictions: list[dict] = []
    traces.add_sink(
        lambda event, payload: evictions.append(payload) if event == "turn_evicted" else None
    )
    children = MAX_TURNS_RETAINED + 2
    try:
        for index in range(children):
            child = f"t-child-{index}"
            start_turn(child)
            with tool_span(f"tool-{index}", {}):
                pass
            end_turn(child)
    finally:
        traces.clear_sinks()

    expected_dropped = (1 + children) - MAX_TURNS_RETAINED
    report = turn_report("t-parent")
    assert report is not None, "in-flight parent was evicted while closed turns were available"
    assert report["dropped_turns"] == expected_dropped
    assert report["dropped_tool_spans"] == 0  # the parent's own ring is untouched
    assert report["tool_calls"]["total"] == 0  # parent totals unaffected
    assert traces.dropped_turns() == expected_dropped
    # Evicted children are explicitly gone; survivors still report correctly.
    assert turn_report("t-child-0") is None
    survivor = turn_report(f"t-child-{children - 1}")
    assert survivor is not None and survivor["tool_calls"]["total"] == 1
    # Every eviction is announced, marked with whether it was in flight.
    assert len(evictions) == expected_dropped
    assert all(payload["ended"] is True for payload in evictions)
    assert all(payload["parent_turn_id"] == "t-parent" for payload in evictions)
    assert "child turns evicted: " + str(expected_dropped) in render_diagnostics("t-parent")


def test_forced_eviction_of_inflight_turns_is_marked():
    start_turn("t-inflight-0")
    evictions: list[dict] = []
    traces.add_sink(
        lambda event, payload: evictions.append(payload) if event == "turn_evicted" else None
    )
    try:
        for index in range(1, MAX_TURNS_RETAINED + 2):
            start_turn(f"t-inflight-{index}")
    finally:
        traces.clear_sinks()

    expected_dropped = (1 + MAX_TURNS_RETAINED + 1) - MAX_TURNS_RETAINED
    assert traces.dropped_turns() == expected_dropped == 2
    # No closed victim existed: the oldest in-flight turns were force-evicted
    # and every one is marked as such.
    assert len(evictions) == 2
    assert all(payload["ended"] is False for payload in evictions)
    assert [payload["turn_id"] for payload in evictions] == ["t-inflight-0", "t-inflight-1"]
