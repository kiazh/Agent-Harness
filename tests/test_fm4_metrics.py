"""FM4 M1: caller-recorded totals and errors must share a key family.

RED before fix: record_llm_call/record_db_call/timed increment
``<op>.calls`` but record errors under ``<op>``, so
``get_error_rate("<op>")`` is structurally always 0.0.
"""

from __future__ import annotations

import pytest

from machina.core.metrics import MetricsCollector, metrics, timed


def test_llm_call_error_rate_reflects_error():
    m = MetricsCollector()
    m.record_llm_call("s1", "m1", 10.0, prompt_tokens=1, completion_tokens=1, is_error=True)
    assert m.get_error_count("llm.m1") == 1
    assert m.get_error_rate("llm.m1") > 0.0
    assert m.get_error_rate("llm.m1") == pytest.approx(1.0)


def test_llm_call_error_rate_matches_calls_and_errors():
    m = MetricsCollector()
    m.record_llm_call("s1", "m1", 1.0)
    m.record_llm_call("s1", "m1", 1.0)
    m.record_llm_call("s1", "m1", 1.0, is_error=True)
    # Two successes + one error = 3 calls; rate 1/3.
    assert m.get_error_rate("llm.m1") == pytest.approx(1 / 3)


def test_db_call_error_rate_reflects_error():
    m = MetricsCollector()
    m.record_db_call("fetch", 5.0)
    m.record_db_call("fetch", 5.0, is_error=True)
    assert m.get_error_rate("db.fetch") == pytest.approx(0.5)


def test_timed_error_rate_reflects_error():
    op = "timed.fm4probe"
    before = metrics.get_counter(f"{op}.calls")
    try:
        with timed(op):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert metrics.get_counter(f"{op}.calls") == before + 1
    assert metrics.get_error_rate(op) > 0.0


def test_error_rate_zero_without_errors():
    m = MetricsCollector()
    m.record_llm_call("s1", "m1", 1.0)
    assert m.get_error_rate("llm.m1") == 0.0
