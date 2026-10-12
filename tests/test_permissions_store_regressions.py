"""Permission-store behavior when process-local state differs from durable state."""

import uuid

import pytest

from ah.permissions import store
from ah.permissions.policy import build_request, decide


@pytest.mark.asyncio
async def test_durable_approval_reads_ignore_stale_local_pending(monkeypatch):
    request_id = uuid.uuid4().hex
    session_id = str(uuid.uuid4())
    digest = uuid.uuid4().hex
    local = {
        "request_id": request_id,
        "session_id": session_id,
        "digest": digest,
        "status": "pending",
        "consumed": False,
        "proposal": {"version": 2},
    }
    durable = {**local, "status": "approved"}

    class FakeDb:
        async def fetchrow(self, query, *_args):
            return None if "status = 'pending'" in query else durable

        async def fetch(self, _query, *_args):
            return []

    async def fake_db():
        return FakeDb()

    monkeypatch.setattr(store, "_db", fake_db)
    store._mem.approvals[request_id] = local
    try:
        assert (await store.get_approval(request_id))["status"] == "approved"
        assert await store.find_pending(session_id, digest) is None
        assert (await store.find_approved_unclaimed(session_id, digest))["status"] == "approved"
        assert await store.list_pending(session_id) == []
    finally:
        store._mem.approvals.pop(request_id, None)


@pytest.mark.asyncio
async def test_grant_revoke_by_id_does_not_revoke_another_session(monkeypatch):
    owner_session = str(uuid.uuid4())
    other_session = str(uuid.uuid4())
    grant_id = str(uuid.uuid4())

    class FakeDb:
        def __init__(self):
            self.grant = {
                "id": uuid.UUID(grant_id),
                "session_id": uuid.UUID(other_session),
                "revoked": False,
            }

        async def execute(self, query, *args):
            if self.grant["id"] != args[0]:
                return "UPDATE 0"
            if "session_id = $2" in query and self.grant["session_id"] != args[1]:
                return "UPDATE 0"
            self.grant["revoked"] = True
            return "UPDATE 1"

    fake = FakeDb()

    async def fake_db():
        return fake

    monkeypatch.setattr(store, "_db", fake_db)
    assert await store.revoke_grants(owner_session, grant_id) == 0
    assert fake.grant["revoked"] is False


@pytest.mark.asyncio
async def test_outage_grant_cannot_regain_authority_after_db_recovery(monkeypatch):
    session_id = str(uuid.uuid4())
    grant_id = str(uuid.uuid4())
    local_grant = {
        "id": grant_id,
        "session_id": session_id,
        "agent_id": "harness",
        "mode": "full",
        "capability": "session",
        "digest": "full-mode-session",
        "revoked": False,
        "expires_at": None,
    }

    class FakeDb:
        async def fetch(self, _query, *_args):
            return []  # Another worker revoked all durable authority.

    available = False

    async def fake_db():
        return FakeDb() if available else None

    request = build_request(
        operation="process.exec",
        argv=["echo", "hello"],
        mode="full",
        session_id=session_id,
        agent_id="harness",
    )
    monkeypatch.setattr(store, "_db", fake_db)
    store._mem.grants[grant_id] = local_grant
    try:
        assert decide(request, await store.list_grants(session_id)).verdict == "allowed"
        available = True
        assert decide(request, await store.list_grants(session_id)).verdict == "pending"
        available = False
        assert decide(request, await store.list_grants(session_id)).verdict == "pending"
    finally:
        store._mem.grants.pop(grant_id, None)
