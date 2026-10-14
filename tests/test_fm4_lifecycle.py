"""FM4 lifecycle fixes: corrupt-state visibility, runtime reconnect, dead
config removal, Database.reset stale-handle warning.

RED before fix:
- m6: update_state_fields silently replaced corrupt session state with {}.
- m7: runtime.startup() latched unavailable while the DB was down and
  refresh() never retried the connection.
- n4: compaction_check_enabled was dead config surface.
- RF2-N9: Database.reset() left module-level `from ... import db` bindings
  pointing at the replaced instance with no signal.
"""

from __future__ import annotations

import logging
import sys
import types
import uuid
from unittest.mock import AsyncMock, MagicMock

from machina.core.session import SessionManager


async def test_update_state_fields_logs_corrupt_state(monkeypatch, caplog):
    from machina.core import session as session_mod

    sid = uuid.uuid4()
    connection = MagicMock()
    connection.fetchrow = AsyncMock(return_value={"state_msgpack": b"\xc1"})
    connection.execute = AsyncMock(return_value="UPDATE 1")
    connection.transaction = MagicMock()
    database = MagicMock()
    database.acquire.return_value.__aenter__.return_value = connection
    monkeypatch.setattr(session_mod, "db", database)

    with caplog.at_level(logging.WARNING, logger="machina.core.session"):
        merged = await SessionManager().update_state_fields(sid, {"emotion": "trust"})

    assert merged == {"emotion": "trust"}
    messages = [r.getMessage() for r in caplog.records]
    assert any(str(sid) in m and "state" in m.lower() for m in messages), messages


class _FlakyDb:
    """connect() fails on the first call, succeeds afterwards."""

    def __init__(self) -> None:
        self.connected = False
        self.connect_calls = 0
        self.schema_calls = 0
        self.closed = 0

    async def connect(self) -> None:
        self.connect_calls += 1
        if self.connect_calls == 1:
            raise ConnectionError("database down")
        self.connected = True

    async def initialize_runtime_schema(self) -> None:
        self.schema_calls += 1

    async def close(self) -> None:
        self.closed += 1
        self.connected = False


async def test_runtime_refresh_reconnects_after_unavailable_startup(monkeypatch):
    from machina.core import runtime as runtime_mod

    fake = _FlakyDb()
    monkeypatch.setattr("machina.db.connection.db", fake)
    monkeypatch.setattr(runtime_mod, "_detect_providers", lambda: {})
    monkeypatch.setattr(runtime_mod, "_detect_sandbox", lambda: {})
    monkeypatch.setattr(runtime_mod, "_detect_toggles", lambda: {})

    services = runtime_mod.RuntimeServices()
    try:
        first = await services.startup()
        assert "unavailable" in str(first["db"])
        assert fake.connect_calls == 1

        second = await services.refresh()
        assert fake.connect_calls == 2, "refresh() must retry db.connect()"
        assert second["db"] == "ready"
        assert fake.schema_calls == 1
        assert {"database", "chat_provider", "jobs"} <= {c["name"] for c in second["capabilities"]}
    finally:
        await services.shutdown()


def test_dead_compaction_check_flag_removed():
    from machina.core.config import DEFAULTS, Config

    assert "compaction_check_enabled" not in DEFAULTS
    assert not hasattr(Config(), "compaction_check_enabled")


def _stale_holder(previous_db, name: str) -> types.ModuleType:
    module = types.ModuleType(name)
    module.db = previous_db  # type: ignore[attr-defined]
    return module


def test_database_reset_warns_about_stale_module_refs(caplog):
    from machina.db import connection

    previous = connection.db
    holder_name = f"fm4_stale_holder_{uuid.uuid4().hex}"
    sys.modules[holder_name] = _stale_holder(previous, holder_name)
    try:
        with caplog.at_level(logging.WARNING, logger="machina.db.connection"):
            connection.Database.reset()
        assert connection.db is not previous
        messages = [r.getMessage() for r in caplog.records]
        assert any(holder_name in m for m in messages), messages
    finally:
        sys.modules.pop(holder_name, None)
        connection.db = previous
