"""Final review 2026-10-15 — F4: approval view boundary for resume helpers.

``store.find_pending`` and ``store.find_approved_unclaimed`` returned the RAW
stored row (unredacted top-level ``target`` column) while ``get_approval`` and
``list_pending`` route through ``public_view``. The broker surfaces these
helpers' rows to transports via ``NeedsApproval``, so the SAME approval could
be shown raw or redacted depending on which helper happened to run.

These pins lock the redacted view boundary AND the selection semantics /
fields the callers depend on (``request_id`` / ``session_id`` / ``digest`` /
``status`` / ``proposal``), so routing through ``public_view`` cannot silently
change resume behavior.

Evidence: docs/verification/proofs/final_review_2026-10-15/fix_permissions_views.md
"""

from __future__ import annotations

import json
import uuid

import pytest

from machina.permissions import store
from machina.permissions.policy import build_request

#: Pattern-recognized credential (OpenRouter shape) so redaction does not
#: depend on global secret registration; it appears ONLY inside the ``target``
#: column of the stored record.
SECRET = "sk-or-v1-8f3c1d9b7a25e64f0c185ac39e7b2d46f0819a3c5e72b40d"


def _assert_no_secret(record: dict) -> None:
    assert SECRET not in json.dumps(record, default=str), "raw secret leaked through view"


def _request(session_id: str):
    return build_request(
        operation="process.exec",
        argv=["bash", "-lc", "curl -sS https://api.example.com/v1/export -o out.json"],
        targets=[f"export-{SECRET}.json"],
        shell_payload="",
        cwd=".",
        mode="ask",
        backend="host",
        agent_id="harness",
        session_id=session_id,
        tool="terminal",
        timeout=60,
    )


@pytest.fixture
def view_state(monkeypatch):
    """Force the in-memory store path and isolate the approval mirror."""

    async def no_db():
        return None

    monkeypatch.setattr(store, "_db", no_db)
    known = set(store._mem.approvals)
    try:
        yield
    finally:
        for request_id in set(store._mem.approvals) - known:
            store._mem.approvals.pop(request_id, None)
        store.clear_grant_cache()


@pytest.mark.asyncio
async def test_find_pending_redacts_target_and_preserves_fields(view_state):
    session_id = str(uuid.uuid4())
    req = _request(session_id)
    rec = await store.create_approval(req, principal="headless")

    found = await store.find_pending(session_id, req.digest)
    assert found is not None
    _assert_no_secret(found)
    # Selection + caller-dependent fields are unchanged.
    assert found["request_id"] == rec["request_id"]
    assert found["session_id"] == session_id
    assert found["digest"] == req.digest
    assert found["status"] == "pending"


@pytest.mark.asyncio
async def test_find_approved_unclaimed_redacts_and_still_resumes(view_state):
    session_id = str(uuid.uuid4())
    req = _request(session_id)
    await store.create_approval(req, principal="headless")
    assert await store.resolve_decision(req.request_id, "approved", principal="human") is not None

    approved = await store.find_approved_unclaimed(session_id, req.digest)
    assert approved is not None
    _assert_no_secret(approved)
    assert approved["status"] == "approved"
    assert approved["request_id"] == req.request_id
    assert approved["proposal"], "resume depends on a reviewable proposal"
    # The exact-action claim still authorizes the raw execution identity.
    assert await store.claim_execution(
        approved["request_id"], digest=req.digest, session_id=session_id
    )


@pytest.mark.asyncio
async def test_resume_helpers_keep_selection_semantics(view_state):
    session_id = str(uuid.uuid4())
    req = _request(session_id)
    await store.create_approval(req, principal="headless")

    # Wrong digest: never selected.
    assert await store.find_pending(session_id, "0" * 64) is None
    # Still pending: not resumable as approved.
    assert await store.find_approved_unclaimed(session_id, req.digest) is None
    # Foreign session: never selected.
    assert await store.find_pending(str(uuid.uuid4()), req.digest) is None


@pytest.mark.asyncio
async def test_broker_headless_pause_surfaces_redacted_record(view_state):
    from machina.permissions.broker import NeedsApproval, permission_broker

    session_id = str(uuid.uuid4())
    req = _request(session_id)
    await store.create_approval(req, principal="headless")

    with pytest.raises(NeedsApproval) as excinfo:
        await permission_broker._guard_headless(req)
    _assert_no_secret(excinfo.value.approval)
    assert excinfo.value.approval["request_id"] == req.request_id


@pytest.mark.asyncio
async def test_broker_headless_resume_still_claims_after_view(view_state):
    from machina.permissions.broker import permission_broker

    session_id = str(uuid.uuid4())
    req = _request(session_id)
    await store.create_approval(req, principal="headless")
    await store.resolve_decision(req.request_id, "approved", principal="human")

    guarded = await permission_broker._guard_headless(req)
    assert guarded is req
    assert guarded.approval_id == req.request_id


@pytest.mark.asyncio
async def test_durable_find_helpers_redact_raw_target(monkeypatch):
    """The durable branch (real DB rows) mirrors get_approval's redaction."""
    row = {
        "request_id": uuid.uuid4().hex,
        "session_id": str(uuid.uuid4()),
        "agent_id": "harness",
        "operation": "process.exec",
        "target": f"export-{SECRET}.json",
        "digest": "a" * 64,
        "status": "pending",
        "consumed": False,
        "proposal": {"version": 2, "targets": [f"export-{SECRET}.json"]},
        "display": "",
    }

    class FakeDb:
        async def fetchrow(self, _query, *_args):
            return row

    async def fake_db():
        return FakeDb()

    monkeypatch.setattr(store, "_db", fake_db)

    pending = await store.find_pending(row["session_id"], row["digest"])
    assert pending is not None
    _assert_no_secret(pending)
    assert pending["request_id"] == row["request_id"]
    assert pending["digest"] == row["digest"]

    approved = await store.find_approved_unclaimed(row["session_id"], row["digest"])
    assert approved is not None
    _assert_no_secret(approved)
    assert approved["request_id"] == row["request_id"]
    assert approved["proposal"], "durable resume still needs the reviewable proposal"
