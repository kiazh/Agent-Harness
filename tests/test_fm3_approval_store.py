"""FM3-F6: memory approval state advances only after durable confirmation.

A lost durable race (or a failed durable write) must never leave the memory
record authorizing an execution; during an outage the memory fallback must
not run a denied/claimed approval.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

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

    async def execute(self, query, *args):
        self.outer.queries.append(query)
        return "INSERT 0 1"

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
        self,
        *,
        for_update=None,
        update_result=None,
        claim_result=None,
        fail_update=False,
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


@pytest.mark.asyncio
async def test_durable_origin_approval_never_authorized_from_memory_fallback(monkeypatch):
    """Audit: stale durable mirror must not outlive the DB.

    A record created while the DB was reachable is DB-authoritative. Even when
    its local mirror still says ``approved`` (another worker may already have
    claimed the durable row), a disconnected pool must refuse to claim it.
    """
    from machina.permissions.policy import build_request

    req = build_request(
        operation="file.write",
        targets=["fallback.txt"],
        content="audit payload",
        cwd=".",
        mode="ask",
        backend="host",
        tool="write_file",
        session_id=str(uuid.uuid4()),
    )
    db = _Db()

    async def durable():
        return db

    async def no_db():
        return None

    monkeypatch.setattr(st, "_db", durable)
    try:
        await st.create_approval(req, "tui")
        rec = st._mem.approvals[req.request_id]
        assert rec.get("durable") is True
        # Human decision recorded durably; mirror is now stale if another
        # worker claims the durable row.
        rec["status"] = "approved"

        monkeypatch.setattr(st, "_db", no_db)
        assert (
            await st.claim_execution(req.request_id, digest=req.digest, session_id=req.session_id)
            is False
        )
        # Decisions are refused too: a memory-only approval verdict must never
        # stand in for the durable authority.
        rec["status"] = "pending"
        assert await st.resolve_decision(req.request_id, "approved", principal="tui") is None
        assert rec["status"] == "pending"
        assert rec.get("consumed") is False
    finally:
        st._mem.approvals.pop(req.request_id, None)


@pytest.mark.asyncio
async def test_memory_only_approval_still_uses_memory_authority(monkeypatch):
    """Control: without a durable origin, the memory backend stays authoritative."""
    from machina.permissions.policy import build_request

    async def no_db():
        return None

    monkeypatch.setattr(st, "_db", no_db)
    req = build_request(
        operation="file.write",
        targets=["memory-only.txt"],
        content="x",
        cwd=".",
        mode="ask",
        backend="host",
        tool="write_file",
        session_id=str(uuid.uuid4()),
    )
    try:
        await st.create_approval(req, "tui")
        rec = st._mem.approvals[req.request_id]
        assert rec.get("durable") is False
        assert await st.resolve_decision(req.request_id, "approved", principal="tui") is not None
        assert (
            await st.claim_execution(req.request_id, digest=req.digest, session_id=req.session_id)
            is True
        )
    finally:
        st._mem.approvals.pop(req.request_id, None)


@pytest.mark.asyncio
async def test_approval_records_are_redacted_in_proposal_display_and_listing(monkeypatch):
    """Audit: stored proposals/display/listings must not persist raw credentials."""
    import json

    from machina.permissions.policy import build_request

    secret = "review-fixture-bearer-8931"
    req = build_request(
        operation="process.exec",
        argv=["curl", "-X", "POST", "-H", f"Authorization: Bearer {secret}", "https://example.com"],
        cwd=".",
        mode="ask",
        backend="host",
        tool="terminal",
        session_id=str(uuid.uuid4()),
    )
    proposal = st.proposal_of(req)
    display = st.display_of(req)
    assert secret not in json.dumps(proposal)
    assert secret not in display
    # Redacted, not dropped: the card/display still shows the argument shape.
    assert any("REDACTED" in value for value in proposal["argv"])
    assert len(proposal["argv"]) == len(req.argv)

    await st.create_approval(req, "tui")
    rec = await st.get_approval(req.request_id)
    pending = await st.list_pending(req.session_id)
    blob = json.dumps({"rec": rec, "pending": pending})
    assert secret not in blob
    assert rec["proposal_complete"] is True

    # The authenticated REST listing (and the gateway, which shares
    # list_pending) returns the same redacted records.
    import machina.api.routes.approvals as approvals_route
    from machina.gateway.features.mode import approvals_list

    class _Sessions:
        async def get(self, _session_id):
            return object()

    monkeypatch.setattr(approvals_route, "session_manager", _Sessions())
    try:
        response = await approvals_route.list_approvals(uuid.UUID(req.session_id))
        assert secret not in json.dumps(response)
        gateway_listing = await approvals_list(_GrantGateway(session_id=req.session_id), {})
        assert secret not in json.dumps(gateway_listing)
    finally:
        st._mem.approvals.pop(req.request_id, None)


class _GrantGateway:
    """Minimal gateway stub for the approvals list/resolve mirrors."""

    def __init__(self, session_id: str = ""):
        self._pending_approvals: dict = {}
        self.writes: list = []
        self._session_id = session_id

    def require_db(self) -> None:
        return None

    async def get_session(self, _params):
        return SimpleNamespace(id=self._session_id)

    def _write(self, payload: dict) -> None:
        self.writes.append(payload)

    def event_payload(self, *args, **kwargs):
        return {"args": args, "kwargs": kwargs}


@pytest.mark.asyncio
async def test_resolve_route_and_gateway_store_reviewed_capabilities(tmp_path):
    """Audit: session grants persist the reviewed capability set, not the operation."""
    from machina.api.routes import approvals as approvals_route
    from machina.api.routes.models import ResolveApprovalRequest
    from machina.gateway.features.mode import approvals_resolve
    from machina.permissions.policy import build_request, decide

    env_file = tmp_path / ".env"
    env_file.write_text("PRIVATE_TOKEN=review-fixture-secret\n", encoding="utf-8")

    def request(session_id: str):
        return build_request(
            operation="file.read",
            targets=[str(env_file)],
            cwd=".",
            mode="ask",
            backend="host",
            tool="read_file",
            session_id=session_id,
        )

    # REST route: session grant must cover the reviewed sensitive capability.
    session_id = str(uuid.uuid4())
    req = request(session_id)
    assert decide(req, []).verdict == "pending"
    approval = await st.create_approval(req, "tui")
    try:
        await approvals_route.resolve_approval(
            approval["request_id"],
            ResolveApprovalRequest(
                verdict="approved", sessionId=uuid.UUID(session_id), grant="session"
            ),
        )
        grants = await st.list_grants(session_id)
        capabilities = {cap for g in grants for cap in str(g["capability"]).split(",")}
        assert "credential_access" in capabilities
        assert decide(request(session_id), grants).verdict == "allowed"
    finally:
        st._mem.approvals.pop(approval["request_id"], None)
        st._mem.grants = {
            k: v for k, v in st._mem.grants.items() if v.get("session_id") != session_id
        }

    # Gateway mirror: identical grant payload from approvals_resolve.
    session2 = str(uuid.uuid4())
    req2 = request(session2)
    approval2 = await st.create_approval(req2, "tui")
    gw = _GrantGateway()
    try:
        await approvals_resolve(
            gw,
            {
                "requestId": approval2["request_id"],
                "verdict": "approved",
                "grant": "session",
                "sessionId": session2,
            },
        )
        grants2 = await st.list_grants(session2)
        capabilities2 = {cap for g in grants2 for cap in str(g["capability"]).split(",")}
        assert "credential_access" in capabilities2
        assert decide(request(session2), grants2).verdict == "allowed"
    finally:
        st._mem.approvals.pop(approval2["request_id"], None)
        st._mem.grants = {
            k: v for k, v in st._mem.grants.items() if v.get("session_id") != session2
        }
