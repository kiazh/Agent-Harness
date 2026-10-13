"""FM3-F6: memory approval state advances only after durable confirmation.

A lost durable race (or a failed durable write) must never leave the memory
record authorizing an execution; during an outage the memory fallback must
not run a denied/claimed approval.
"""

from __future__ import annotations

import uuid

import pytest

from machina.permissions import store as st


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Conn:
    def __init__(self, outer):
        self.outer = outer

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def transaction(self):
        return _Tx()

    async def fetchrow(self, query, *args):
        self.outer.queries.append(query)
        if "FOR UPDATE" in query:
            return self.outer.for_update
        if "SET status = 'claimed'" in query:
            return self.outer.claim_result
        if "SET status = $2" in query:
            if self.outer.fail_update:
                raise RuntimeError("db write failed")
            return self.outer.update_result
        raise AssertionError(f"unexpected fetchrow: {query}")


class _Db:
    connected = True

    def __init__(
        self, *, for_update=None, update_result=None, claim_result=None, fail_update=False
    ):
        self.for_update = for_update
        self.update_result = update_result
        self.claim_result = claim_result
        self.fail_update = fail_update
        self.queries: list[str] = []

    def acquire(self):
        return _Conn(self)


def _pending_record(request_id: str, session_id: str, digest: str) -> dict:
    return {
        "request_id": request_id,
        "session_id": session_id,
        "digest": digest,
        "status": "pending",
        "consumed": False,
        "agent_id": "h",
        "proposal": {"version": 2},
    }


@pytest.mark.asyncio
async def test_lost_race_does_not_relax_memory_to_approved(monkeypatch):
    request_id, session_id, digest = uuid.uuid4().hex, str(uuid.uuid4()), uuid.uuid4().hex
    rec = _pending_record(request_id, session_id, digest)
    st._mem.approvals[request_id] = rec
    # Durable truth: another worker already denied this approval.
    db = _Db(for_update={"status": "denied", "consumed": False, "agent_id": "tui"})

    async def fake_db():
        return db

    monkeypatch.setattr(st, "_db", fake_db)
    try:
        assert await st.resolve_decision(request_id, "approved", principal="tui") is None
        assert rec["status"] == "denied", (
            "memory must mirror durable truth, not the attempted verdict"
        )

        async def no_db():
            return None

        monkeypatch.setattr(st, "_db", no_db)
        assert await st.claim_execution(request_id, digest=digest, session_id=session_id) is False
    finally:
        st._mem.approvals.pop(request_id, None)


@pytest.mark.asyncio
async def test_durable_write_failure_leaves_memory_pending(monkeypatch):
    request_id, session_id, digest = uuid.uuid4().hex, str(uuid.uuid4()), uuid.uuid4().hex
    rec = _pending_record(request_id, session_id, digest)
    st._mem.approvals[request_id] = rec
    db = _Db(
        for_update={"status": "pending", "consumed": False, "agent_id": "some-agent"},
        fail_update=True,
    )

    async def fake_db():
        return db

    monkeypatch.setattr(st, "_db", fake_db)
    try:
        with pytest.raises(RuntimeError):
            await st.resolve_decision(request_id, "approved", principal="tui")
        assert rec["status"] == "pending"

        async def no_db():
            return None

        monkeypatch.setattr(st, "_db", no_db)
        assert await st.claim_execution(request_id, digest=digest, session_id=session_id) is False
    finally:
        st._mem.approvals.pop(request_id, None)


@pytest.mark.asyncio
async def test_claim_mirrors_durable_success_and_blocks_memory_replay(monkeypatch):
    request_id, session_id, digest = uuid.uuid4().hex, str(uuid.uuid4()), uuid.uuid4().hex
    rec = _pending_record(request_id, session_id, digest)
    rec["status"] = "approved"
    st._mem.approvals[request_id] = rec
    db = _Db(claim_result={"request_id": request_id})

    async def fake_db():
        return db

    monkeypatch.setattr(st, "_db", fake_db)
    try:
        assert await st.claim_execution(request_id, digest=digest, session_id=session_id) is True
        assert rec["status"] == "claimed" and rec["consumed"] is True

        async def no_db():
            return None

        monkeypatch.setattr(st, "_db", no_db)
        assert await st.claim_execution(request_id, digest=digest, session_id=session_id) is False
    finally:
        st._mem.approvals.pop(request_id, None)


@pytest.mark.asyncio
async def test_lost_claim_prevents_memory_path_replay(monkeypatch):
    request_id, session_id, digest = uuid.uuid4().hex, str(uuid.uuid4()), uuid.uuid4().hex
    rec = _pending_record(request_id, session_id, digest)
    rec["status"] = "approved"
    st._mem.approvals[request_id] = rec
    db = _Db(claim_result=None)  # another worker won the claim

    async def fake_db():
        return db

    monkeypatch.setattr(st, "_db", fake_db)
    try:
        assert await st.claim_execution(request_id, digest=digest, session_id=session_id) is False

        async def no_db():
            return None

        monkeypatch.setattr(st, "_db", no_db)
        assert await st.claim_execution(request_id, digest=digest, session_id=session_id) is False
    finally:
        st._mem.approvals.pop(request_id, None)
