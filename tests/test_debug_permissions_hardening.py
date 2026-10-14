"""Debug-pass hardenings: permission-store fallback reporting + expiry fail-closed."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


def test_grant_live_null_expiry_is_live():
    from machina.permissions.store import _grant_live

    assert _grant_live({"revoked": False, "expires_at": None}) is True
    assert _grant_live({"revoked": False}) is True


def test_grant_live_expired_is_not_live():
    from machina.permissions.store import _grant_live

    past = datetime.now(UTC) - timedelta(seconds=10)
    assert _grant_live({"revoked": False, "expires_at": past}) is False
    future = datetime.now(UTC) + timedelta(hours=1)
    assert _grant_live({"revoked": False, "expires_at": future}) is True
    assert _grant_live({"revoked": True, "expires_at": future}) is False


def test_grant_live_unparseable_expiry_fails_closed():
    """A present-but-garbage expires_at must not authorize (fail closed)."""
    from machina.permissions.store import _grant_live

    assert _grant_live({"revoked": False, "expires_at": "not-a-timestamp"}) is False
    assert _grant_live({"revoked": False, "expires_at": "99999"}) is False
    assert _grant_live({"revoked": False, "expires_at": ""}) is False


def test_grant_live_iso_string_expiry_parses():
    from machina.permissions.store import _grant_live

    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    assert _grant_live({"revoked": False, "expires_at": future}) is True
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    assert _grant_live({"revoked": False, "expires_at": past}) is False


def test_grant_usable_matches_store_on_garbage_expiry(monkeypatch):
    """Policy-time and execution-time checks agree: garbage expiry denies."""
    from machina.permissions import policy
    from machina.permissions.policy import _grant_usable
    from machina.permissions.store import _grant_live

    recorded = []
    monkeypatch.setattr(policy, "record_failure", lambda op, e: recorded.append((op, e)))

    grant = {"revoked": False, "expires_at": "garbage", "session_id": "", "agent_id": ""}
    assert _grant_live(grant) is False

    class Req:
        session_id = ""
        agent_id = ""

    assert _grant_usable(grant, Req()) is False  # type: ignore[arg-type]
    assert [op for op, _ in recorded] == ["policy.grant_expiry"]


def test_broker_redaction_failure_never_returns_raw_text(monkeypatch):
    """The display redactor fails closed with a marker, never raw input."""
    from machina.permissions import broker

    recorded = []
    monkeypatch.setattr(broker, "record_failure", lambda op, e: recorded.append((op, e)))

    def boom(_text):
        raise RuntimeError("redactor exploded")

    monkeypatch.setattr("machina.memory.redaction.redact_secrets", boom)
    result = broker._redacted("sk-" + "a" * 30)
    assert result == "[redaction unavailable]"
    assert [op for op, _ in recorded] == ["broker.redacted"]


async def test_db_helper_records_failure(monkeypatch):
    """The memory fallback path reports instead of failing silently."""
    import machina.permissions.store as store

    recorded = []
    monkeypatch.setattr(
        "machina.observability.diagnostics.record_failure",
        lambda op, e: recorded.append((op, e)),
    )
    import machina.db.connection as connmod

    class Disconnected:
        connected = False

    real_db = connmod.db
    monkeypatch.setattr(connmod, "db", Disconnected())
    # connected=False is a clean miss (no failure recorded)...
    assert await store._db() is None
    assert recorded == []

    # ...while an exception records exactly once.
    class Boom:
        @property
        def connected(self):
            raise RuntimeError("db probe boom")

    monkeypatch.setattr(connmod, "db", Boom())
    assert await store._db() is None
    assert [op for op, _ in recorded] == ["permissions.store"]
    monkeypatch.setattr(connmod, "db", real_db)
