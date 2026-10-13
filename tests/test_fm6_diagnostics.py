"""FM6 F13 — repeated boundary failures must not flood logs.

Every call still moves the ``diagnostic.failures`` metric; only the warning
line is rate-limited to one per key per flood interval.
"""

from __future__ import annotations

import logging
import uuid

from machina.core.metrics import metrics
from machina.observability import diagnostics


def _records_for(caplog, key):
    return [
        record
        for record in caplog.records
        if key in record.getMessage() and record.levelno >= logging.WARNING
    ]


def test_record_failure_flood_control(monkeypatch, caplog):
    clock = {"now": 10_000.0}
    monkeypatch.setattr(diagnostics.time, "monotonic", lambda: clock["now"])
    key = f"fm6-boundary-{uuid.uuid4().hex}"
    before = metrics.get_counter("diagnostic.failures")

    with caplog.at_level(logging.WARNING, logger="machina.observability.diagnostics"):
        diagnostics.record_failure(key, ValueError("first"))
        diagnostics.record_failure(key, ValueError("second"))
        assert len(_records_for(caplog, key)) == 1

        clock["now"] += diagnostics.FLOOD_INTERVAL_SECONDS - 1
        diagnostics.record_failure(key, ValueError("still-exempt"))
        assert len(_records_for(caplog, key)) == 1

        clock["now"] += 1
        diagnostics.record_failure(key, ValueError("after-interval"))
        assert len(_records_for(caplog, key)) == 2

    # The metric is never suppressed: all four failures counted.
    assert metrics.get_counter("diagnostic.failures") == before + 4


def test_flood_control_is_per_boundary_key(monkeypatch, caplog):
    clock = {"now": 50_000.0}
    monkeypatch.setattr(diagnostics.time, "monotonic", lambda: clock["now"])
    first = f"fm6-a-{uuid.uuid4().hex}"
    second = f"fm6-b-{uuid.uuid4().hex}"
    with caplog.at_level(logging.WARNING, logger="machina.observability.diagnostics"):
        diagnostics.record_failure(first, ValueError("x"))
        diagnostics.record_failure(second, ValueError("x"))
    assert len(_records_for(caplog, first)) == 1
    assert len(_records_for(caplog, second)) == 1
