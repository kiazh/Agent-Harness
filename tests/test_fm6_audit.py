"""FM6 F8 — audit loss windows must be counted and logged loudly.

RED-first: submissions before ``start()`` or after shutdown silently vanished
(only queue-full drops were counted).
"""

from __future__ import annotations

import logging

import pytest

from machina.core.metrics import metrics
from machina.observability import audit
from machina.observability.audit import AuditPersistence


def test_submit_before_start_counts_and_logs(monkeypatch, caplog):
    writer = AuditPersistence()
    before = metrics.get_counter("audit.queue.dropped")
    before_specific = metrics.get_counter("audit.queue.dropped_before_start")
    with caplog.at_level(logging.ERROR, logger="machina.observability.audit"):
        writer.submit({"event": "lost-before-start"})
    assert metrics.get_counter("audit.queue.dropped") == before + 1
    assert metrics.get_counter("audit.queue.dropped_before_start") == before_specific + 1
    assert any(
        record.levelno == logging.ERROR and "audit" in record.getMessage().lower()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_submit_after_shutdown_counts_and_logs(monkeypatch, caplog):
    async def noop_execute(*_args, **_kwargs):
        return "INSERT 0 1"

    monkeypatch.setattr(audit.db, "execute", noop_execute)
    writer = AuditPersistence(max_pending=4)
    writer.start()
    await writer.stop(timeout=1.0)
    before = metrics.get_counter("audit.queue.dropped")
    before_specific = metrics.get_counter("audit.queue.dropped_shutdown")
    with caplog.at_level(logging.ERROR, logger="machina.observability.audit"):
        writer.submit({"event": "lost-after-shutdown"})
    assert metrics.get_counter("audit.queue.dropped") == before + 1
    assert metrics.get_counter("audit.queue.dropped_shutdown") == before_specific + 1
    assert any(record.levelno == logging.ERROR for record in caplog.records)


@pytest.mark.asyncio
async def test_healthy_submission_still_persists(monkeypatch):
    persisted = []

    async def capture_execute(sql, *args):
        persisted.append(args)
        return "INSERT 0 1"

    monkeypatch.setattr(audit.db, "execute", capture_execute)
    writer = AuditPersistence(max_pending=4)
    writer.start()
    writer.submit({"event": "kept"})
    await writer.stop(timeout=1.0)
    assert persisted and "kept" in persisted[0][1]
