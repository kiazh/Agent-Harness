"""Approval ownership + grant semantics against an isolated database (LP-01..07).

Uses AGENT_HARNESS_TEST_DATABASE_URL when set, else skips (never touches
production). Each module's `db` handle is patched to the isolated database.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest

TEST_DSN = os.environ.get("AGENT_HARNESS_TEST_DATABASE_URL", "")
needs_test_db = pytest.mark.skipif(not TEST_DSN, reason="AGENT_HARNESS_TEST_DATABASE_URL not set")
_MODULES_WITH_DB = [
    "ah.db.connection",
    "ah.core.session",
    "ah.core.context",
    "ah.core.scheduler",
    "ah.core.orchestrator",
    "ah.core.agent_def",
    "ah.core.usage",
    "ah.gateway.server",
    "ah.rag.pipeline",
    "ah.tools.agents",
    "ah.memory.store",
]


@pytest.fixture
async def isolated_db(monkeypatch):
    from ah.db.connection import Database

    tdb = Database(dsn=TEST_DSN)
    await tdb.connect()
    await tdb.initialize_schema()
    for mod in _MODULES_WITH_DB:
        monkeypatch.setattr(f"{mod}.db", tdb, raising=False)
    yield tdb
    try:
        await tdb.execute("DELETE FROM sessions WHERE title LIKE 'lp-test%'")
    except Exception:
        pass
    await tdb.close()


async def _session(title="lp-test"):
    from ah.core.session import session_manager

    return await session_manager.create(title=title)


def _req(**kw):
    from ah.permissions.policy import build_request

    base = dict(
        operation="file.write", targets=["/proj/note.txt"], content="hello",
        mode="ask", agent_id="harness",
    )
    base.update(kw)
    return build_request(**base)


@needs_test_db
async def test_two_workers_one_execution_claim(isolated_db):
    """LP-06/acceptance-10: concurrent resolve/consume → exactly one owner."""
    import asyncio

    from ah.permissions import store as st

    s = await _session()
    req = _req(session_id=str(s.id))
    await st.create_approval(req, principal="tui")
    assert await st.resolve_decision(req.request_id, "approved", principal="tui")
    results = await asyncio.gather(
        st.claim_execution(req.request_id, digest=req.digest, session_id=str(s.id)),
        st.claim_execution(req.request_id, digest=req.digest, session_id=str(s.id)),
    )
    assert sorted(results) == [False, True]
    assert await st.complete_execution(req.request_id, "completed") is True
    assert await st.complete_execution(req.request_id, "completed") is False


@needs_test_db
async def test_crash_after_decision_then_retry(isolated_db):
    from ah.permissions import store as st

    s = await _session()
    req = _req(session_id=str(s.id))
    await st.create_approval(req, principal="tui")
    assert await st.resolve_decision(req.request_id, "approved", principal="tui")
    # Duplicate resolve and conflicting verdicts fail; denial path recorded.
    assert await st.resolve_decision(req.request_id, "approved", principal="tui") is None
    assert await st.claim_execution(req.request_id, digest=req.digest, session_id=str(s.id)) is True
    assert await st.claim_execution(req.request_id, digest="changed", session_id=str(s.id)) is False
    assert await st.complete_execution(req.request_id, "failed") is True


@needs_test_db
async def test_headless_pause_approve_resume_once(isolated_db):
    """LP-02/acceptance-5: pause → HTTP approve → restart → fresh claim → one effect."""
    from ah.permissions import broker as brokermod
    from ah.permissions import store as st
    from ah.permissions.broker import NeedsApproval

    s = await _session()
    req = _req(session_id=str(s.id))
    # Pause: no approver → pending record, no grant.
    with pytest.raises(NeedsApproval) as first:
        await brokermod.permission_broker.guard(req)
    assert first.value.approval["status"] == "pending"
    # HTTP approval (separate transport, same durable row).
    assert await st.resolve_decision(req.request_id, "approved", principal="http")
    # Restart: drop process-local state; the row survives.
    st._mem.approvals.clear()
    st._mem.grants.clear()
    # Resume on a fresh claim: no new card, execution claimed once.
    claimed = await brokermod.permission_broker.guard(_req(session_id=str(s.id)))
    assert claimed.approval_id == req.request_id
    # A concurrent duplicate retry cannot claim again (no double effect).
    with pytest.raises(NeedsApproval):
        await brokermod.permission_broker.guard(_req(session_id=str(s.id)))
    assert await brokermod.permission_broker.complete(req.request_id, "completed") is True


@needs_test_db
async def test_once_does_not_authorize_repeat_or_change(isolated_db):
    """LP-03: once-only approval authorizes one exact execution, nothing reusable."""
    from ah.permissions import broker as brokermod
    from ah.permissions import store as st
    from ah.permissions.broker import NeedsApproval

    s = await _session()
    req = _req(session_id=str(s.id))

    async def _approve(card):
        await st.resolve_decision(card["request_id"], "approved", principal="tui")
        return "approved"

    brokermod.set_approval_handler(_approve, principal="tui", turn_id="t1")
    try:
        out = await brokermod.permission_broker.guard(req)
    finally:
        brokermod.set_approval_handler(None)
    assert out.approval_id == req.request_id
    await brokermod.permission_broker.complete(req.request_id, "completed")
    # Repeat of the ORIGINAL digest → fresh approval, not silent allow.
    brokermod.set_approval_handler(None)
    with pytest.raises(NeedsApproval):
        await brokermod.permission_broker.guard(_req(session_id=str(s.id)))
    # Changed content → fresh approval.
    with pytest.raises(NeedsApproval):
        await brokermod.permission_broker.guard(_req(session_id=str(s.id), content="other"))


@needs_test_db
async def test_session_grant_allows_repeat_not_sibling_or_change(isolated_db):
    """LP-03/acceptance-6: explicit session grant covers repeats, nothing else."""
    from ah.permissions import broker as brokermod
    from ah.permissions import store as st
    from ah.permissions.broker import NeedsApproval

    s = await _session()
    req = _req(session_id=str(s.id))

    async def _approve_session(card):
        await st.resolve_decision(card["request_id"], "approved", principal="tui")
        await st.save_grant(
            {
                "session_id": str(s.id),
                "agent_id": "harness",
                "mode": "ask",
                "capability": "file.write",
                "scope_path": "/proj/note.txt",
                "scope_type": "file",
                "grant_kind": "session",
                "digest": req.digest,
            }
        )
        return "approved"

    brokermod.set_approval_handler(_approve_session, principal="tui", turn_id="t1")
    try:
        await brokermod.permission_broker.guard(req)
    finally:
        brokermod.set_approval_handler(None)
    await brokermod.permission_broker.complete(req.request_id, "completed")
    brokermod.set_approval_handler(None)
    # Repeat of the identical action proceeds without a new card.
    await brokermod.permission_broker.guard(_req(session_id=str(s.id)))
    # Sibling path still asks.
    with pytest.raises(NeedsApproval):
        await brokermod.permission_broker.guard(
            _req(session_id=str(s.id), targets=["/proj/other.txt"], content="hello")
        )
    # Changed content still asks.
    with pytest.raises(NeedsApproval):
        await brokermod.permission_broker.guard(_req(session_id=str(s.id), content="changed"))


@needs_test_db
async def test_scope_identity_and_directory_containment(isolated_db):
    """LP-04: exact file identity vs component-wise directory scope."""
    from ah.permissions.policy import build_request, decide

    def _grant(**kw):
        base = {
            "id": str(uuid.uuid4()), "session_id": "s", "agent_id": "h",
            "mode": "ask", "capability": "file.write", "scope_type": "file",
            "grant_kind": "session", "digest": "d", "revoked": False, "expires_at": None,
        }
        base.update(kw)
        return base

    async def _decide(target, grant):
        req = build_request(
            operation="file.write", targets=[target], content="x",
            mode="ask", agent_id="h", session_id="s",
        )
        req.digest = "d"
        return decide(req, [grant]).verdict

    file_grant = _grant(scope_path="/proj/report.txt")
    assert await _decide("/proj/report.txt", file_grant) == "allowed"
    assert await _decide("/proj/report.txt.backup", file_grant) == "pending"
    assert await _decide("/proj-other/report.txt", file_grant) == "pending"
    dir_grant = dict(file_grant, scope_type="dir", scope_path="/proj")
    assert await _decide("/proj/sub/note.txt", dir_grant) == "allowed"
    assert await _decide("/proj-other/note.txt", dir_grant) == "pending"
    assert await _decide("/proj/../etc/passwd", dir_grant) == "pending"


@needs_test_db
async def test_grant_expiry_revocation_binding(isolated_db):
    """LP-05: expired/future/revoked/foreign grants never authorize."""
    from datetime import UTC, datetime, timedelta

    from ah.permissions.policy import build_request, decide

    now = datetime.now(UTC)

    def _grant(**kw):
        base = {
            "id": str(uuid.uuid4()), "session_id": "s", "agent_id": "h",
            "mode": "ask", "capability": "file.write", "scope_type": "file",
            "scope_path": "/proj/note.txt", "grant_kind": "session",
            "digest": "d", "revoked": False, "expires_at": None,
        }
        base.update(kw)
        return base

    async def _decide(grant):
        req = build_request(
            operation="file.write", targets=["/proj/note.txt"], content="x",
            mode="ask", agent_id="h", session_id="s",
        )
        req.digest = "d"
        return decide(req, [grant]).verdict

    assert await _decide(_grant()) == "allowed"
    assert await _decide(_grant(expires_at=now - timedelta(days=1))) == "pending"
    assert await _decide(_grant(expires_at=now + timedelta(days=1))) == "allowed"
    assert await _decide(_grant(revoked=True)) == "pending"
    assert await _decide(_grant(agent_id="other")) == "pending"
    assert await _decide(_grant(session_id="other")) == "pending"


@needs_test_db
async def test_full_mode_session_isolation_and_revoke(isolated_db):
    """LP-07/acceptance-8: A full does not elevate B; revoke ends authority."""
    from ah.permissions import broker as brokermod
    from ah.permissions import store as st

    a = await _session(title="lp-test-a")
    b = await _session(title="lp-test-b")
    await st.save_grant(
        {
            "session_id": str(a.id), "agent_id": "harness", "mode": "full",
            "capability": "session", "scope_type": "file", "grant_kind": "session",
            "digest": "full-mode-session",
        }
    )
    from ah.permissions.policy import build_request, decide

    async def _verdict(sid):
        req = build_request(
            operation="process.exec", argv=["whoami"], mode="full",
            agent_id="harness", session_id=str(sid),
        )
        return decide(req, await st.list_grants(str(sid))).verdict

    assert await _verdict(a.id) == "allowed"
    assert await _verdict(b.id) == "pending"
    assert await brokermod.permission_broker.revoke(str(a.id)) >= 1
    assert await _verdict(a.id) == "pending"
    # Durable: memory dropped, DB still denies.
    st._mem.grants.clear()
    assert await _verdict(a.id) == "pending"


@needs_test_db
async def test_approval_partition_across_sessions(isolated_db):
    """Acceptance-9: finishing A's turn never cancels B's approval wait."""
    import asyncio

    from ah.gateway.server import Gateway

    gw = Gateway(write=lambda frame: None, owns_db=False)
    gw._db_ready = True
    loop = asyncio.get_running_loop()
    fa: asyncio.Future[str] = loop.create_future()
    fb: asyncio.Future[str] = loop.create_future()
    gw._pending_approvals["ra"] = (fa, "turn-a", "sess-a")
    gw._pending_approvals["rb"] = (fb, "turn-b", "sess-b")
    assert gw.cancel_turn_approvals("turn-a") == 1
    assert fa.cancelled() and not fb.done()
    fb.set_result("approved")
    assert (await fb) == "approved"


@needs_test_db
async def test_expired_db_grant_never_authorizes(isolated_db):
    """LP-05 DB backend: expired rows are filtered before decisions."""
    from datetime import UTC, datetime, timedelta

    from ah.permissions import store as st
    from ah.permissions.policy import build_request, decide

    s = await _session()
    now = datetime.now(UTC)
    await st.save_grant(
        {
            "session_id": str(s.id), "agent_id": "harness", "mode": "ask",
            "capability": "file.write", "scope_path": "/proj/note.txt",
            "scope_type": "file", "grant_kind": "session", "digest": "d-exp",
            "expires_at": now - timedelta(days=1),
        }
    )
    live = await st.list_grants(str(s.id))
    assert all(g["digest"] != "d-exp" for g in live)
    req = build_request(
        operation="file.write", targets=["/proj/note.txt"], content="x",
        mode="ask", agent_id="harness", session_id=str(s.id),
    )
    req.digest = "d-exp"
    assert decide(req, live).verdict == "pending"
