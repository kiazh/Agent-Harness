"""FM4 M2-partner: a lost usage.finish (0 rows) must be surfaced.

RED before fix: finish() silently returned False after a clobber, so a
late/duplicate finish vanished with no log and no counter.
"""

from __future__ import annotations

import logging
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

from machina.core.metrics import metrics
from machina.core.usage import UsageStore

LOGGER = "machina.core.usage"


async def test_finish_lost_logs_and_counts(monkeypatch, caplog):
    import machina.core.usage as usage_mod

    execute = AsyncMock(return_value="UPDATE 0")
    monkeypatch.setattr(usage_mod, "db", SimpleNamespace(execute=execute))
    before = metrics.get_counter("usage.finish_lost")
    rid = uuid.uuid4()
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        written = await UsageStore().finish(rid, {"prompt_tokens": 1, "completion_tokens": 1})
    assert written is False
    assert metrics.get_counter("usage.finish_lost") == before + 1
    messages = [r.getMessage() for r in caplog.records]
    assert any("finish" in m and str(rid) in m for m in messages), messages
    # Never raises: caller (complete_call) still gets a boolean.
    execute.assert_awaited_once()


async def test_finish_written_does_not_count_lost(monkeypatch):
    import machina.core.usage as usage_mod

    monkeypatch.setattr(
        usage_mod, "db", SimpleNamespace(execute=AsyncMock(return_value="UPDATE 1"))
    )
    before = metrics.get_counter("usage.finish_lost")
    written = await UsageStore().finish(uuid.uuid4(), {"prompt_tokens": 1, "completion_tokens": 1})
    assert written is True
    assert metrics.get_counter("usage.finish_lost") == before


async def test_finish_none_reservation_is_not_lost(monkeypatch):
    """DB-less calls legitimately have no reservation: no loss signal."""
    import machina.core.usage as usage_mod

    execute = AsyncMock(return_value="UPDATE 0")
    monkeypatch.setattr(usage_mod, "db", SimpleNamespace(execute=execute))
    before = metrics.get_counter("usage.finish_lost")
    assert await UsageStore().finish(None, {"prompt_tokens": 1}) is False
    assert metrics.get_counter("usage.finish_lost") == before
    execute.assert_not_awaited()


async def test_finish_error_still_records_error_row(monkeypatch):
    """failed=True is a normal finish (1 row); it is not 'lost'."""
    import machina.core.usage as usage_mod

    monkeypatch.setattr(
        usage_mod, "db", SimpleNamespace(execute=AsyncMock(return_value="UPDATE 1"))
    )
    before = metrics.get_counter("usage.finish_lost")
    written = await UsageStore().finish(uuid.uuid4(), failed=True)
    assert written is True
    assert metrics.get_counter("usage.finish_lost") == before
