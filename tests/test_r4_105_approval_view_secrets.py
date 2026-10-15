"""R4-105(d): no secrets in the rendered approval view (real broker + store).

The existing coverage (``test_fm3_approval_store``) drives ``create_approval``
directly against the IN-MEMORY store with an argv-only secret. This suite runs
the REAL broker (interactive approval pause) over a real PostgreSQL approval
store (private schema on ``MACHINA_TEST_DATABASE_URL``), with a
runtime-registered secret (the path provider keys and OAuth tokens take via
``remember_secret_value``) placed in the executable argv/shell AND in a target
string, and asserts every rendered surface:

* the live card handed to the approval handler (``broker._approval_view``),
* the durable record read back (``store.get_approval``),
* the listing surfaces (``store.list_pending`` -> ``public_view``) shared by
  the gateway, REST and CLI.

Display redaction must never weaken execution identity: after approval the
same request still claims exactly (raw argv, exact digest).

Known leak (pinned strict-xfail, owner: ``permissions/store.py`` lane): the
top-level ``target`` column is persisted raw and ``public_view`` does not
redact it, so a secret embedded in *targets* still reaches listings. argv
redaction is complete today.
"""

from __future__ import annotations

import json
import os

import pytest
import pytest_asyncio

pytestmark = pytest.mark.db

#: Runtime-registered secret (46 chars after the sk-or-v1- prefix), matching
#: the shape of an OpenRouter key saved into the credential registry.
SECRET = "sk-or-v1-8f3c1d9b7a25e64f0c185ac39e7b2d46f0819a3c5e72b40d"


@pytest_asyncio.fixture
async def approval_db():
    """Wire the shared ``db`` singleton to a PRIVATE test schema.

    Deliberately independent of the ``db_pool`` fixture (see the private
    schema rationale in ``test_r4_105_same_session_load_race``): public
    per-test DDL deadlocks against other processes' data writes here.
    """
    dsn = os.environ.get("MACHINA_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("MACHINA_TEST_DATABASE_URL not set")
    from machina.db.connection import Database, db
    from tests.support.schema import isolated_schema

    administrator = Database(dsn=dsn)
    await administrator.connect()
    try:
        async with isolated_schema(administrator) as (_schema, database):
            original_pool, original_dsn = db._pool, db.dsn
            db._pool = database._pool
            db.dsn = database.dsn
            try:
                yield database
            finally:
                db._pool = original_pool
                db.dsn = original_dsn
    finally:
        await administrator.close()


@pytest.fixture(autouse=True)
def approval_state():
    """Isolate broker handler, approval mirror and grant cache per test."""
    from machina.permissions import store
    from machina.permissions.broker import clear_execution_context, set_approval_handler

    known = set(store._mem.approvals)
    set_approval_handler(None)
    try:
        yield
    finally:
        set_approval_handler(None)
        clear_execution_context()
        for request_id in set(store._mem.approvals) - known:
            store._mem.approvals.pop(request_id, None)
        store.clear_grant_cache()


def _blob(value) -> str:
    return json.dumps(value, default=str)


def _assert_redacted(rendered, *surfaces: str) -> None:
    text = _blob(rendered)
    assert SECRET not in text, f"raw secret leaked through: {', '.join(surfaces)}"


def _session_request(session_id: str, *, argv: list[str], targets: list[str]):
    from machina.permissions.policy import build_request

    return build_request(
        operation="process.exec",
        argv=argv,
        targets=targets,
        shell_payload="",
        cwd=".",
        mode="ask",
        backend="host",
        agent_id="harness",
        session_id=session_id,
        tool="terminal",
        timeout=60,
    )


async def _pause_at_handler(req, captured):
    """Drive the real broker until the handler returns denied (record stays pending)."""
    from machina.permissions.broker import (
        ApprovalDenied,
        permission_broker,
        set_approval_handler,
    )

    async def handler(card):
        captured.append(card)
        return "denied"  # transport declined; do not resolve the record

    set_approval_handler(handler, principal="tui", turn_id="r4-105-turn")
    with pytest.raises(ApprovalDenied):
        await permission_broker.guard(req)


@pytest.mark.asyncio
async def test_argv_secret_is_redacted_across_card_record_and_listing(approval_db):
    from machina.core.session import session_manager
    from machina.permissions import store
    from machina.permissions.broker import permission_broker
    from machina.permissions.policy import build_request
    from machina.security.secrets import remember_secret_value

    remember_secret_value(SECRET)
    session = await session_manager.create(title="r4-105-argv", agent_id="harness")
    req = _session_request(
        str(session.id),
        argv=[
            "bash",
            "-lc",
            f"curl -H 'Authorization: Bearer {SECRET}' https://api.example.com",
        ],
        targets=[],
    )
    captured: list[dict] = []
    await _pause_at_handler(req, captured)

    assert len(captured) == 1
    card = captured[0]
    _assert_redacted(card, "live approval card")
    assert any("[REDACTED" in str(a) for a in card["argv"]), card["argv"]
    assert card["request_id"] == req.request_id
    assert card["durable"] is True, "the test database must take the durable store path"

    record = await store.get_approval(req.request_id)
    assert record is not None
    _assert_redacted(record, "durable get_approval record")
    assert SECRET not in (record.get("display") or ""), "display text leaked"
    assert SECRET not in _blob(record.get("proposal")), "persisted proposal leaked"

    pending = await store.list_pending(str(session.id))
    listed = [row for row in pending if row["request_id"] == req.request_id]
    assert listed, "pending approval missing from the listing"
    _assert_redacted(listed[0], "list_pending/gateway/REST listing")
    _assert_redacted(store.public_view(listed[0]), "public_view of the listing")
    assert any("[REDACTED" in str(a) for a in listed[0]["proposal"]["argv"]), listed[0]["proposal"][
        "argv"
    ]

    # Redaction is display-only: approval still binds the exact raw action.
    resolved = await store.resolve_decision(req.request_id, "approved", principal="human")
    assert resolved is not None
    guarded = await permission_broker.guard(req)
    assert guarded.approval_id == req.request_id
    assert SECRET in req.argv[2], "execution identity must keep the raw argv"
    assert (
        guarded.digest
        == build_request(
            operation="process.exec",
            argv=list(req.argv),
            targets=[],
            shell_payload="",
            cwd=".",
            mode="ask",
            backend="host",
            agent_id="harness",
            session_id=str(session.id),
            tool="terminal",
            timeout=60,
        ).digest
    )


@pytest.mark.asyncio
async def test_target_secret_is_redacted_in_live_card(approval_db):
    """The live card redacts a secret embedded in targets (green today)."""
    from machina.core.session import session_manager
    from machina.security.secrets import remember_secret_value

    remember_secret_value(SECRET)
    session = await session_manager.create(title="r4-105-target-card", agent_id="harness")
    req = _session_request(
        str(session.id),
        # A command that still requires review; the secret lives ONLY in the
        # target so the target-redaction path is isolated.
        argv=["bash", "-lc", "curl -sS https://api.example.com/v1/export -o /tmp/export.json"],
        targets=[f"https://api.example.com/v1/export?api_key={SECRET}"],
    )
    captured: list[dict] = []
    await _pause_at_handler(req, captured)

    card = captured[0]
    _assert_redacted(card, "live approval card targets")
    assert any("[REDACTED" in str(t) for t in card["targets"]), card["targets"]


@pytest.mark.xfail(
    strict=True,
    reason=(
        "R4-105: permissions/store.py persists the top-level target column raw "
        "and public_view() does not redact it, so a secret embedded in targets "
        "still reaches list_pending/get_approval (owner: permissions/store.py "
        "lane). Flip this xfail with the store-side fix."
    ),
)
@pytest.mark.asyncio
async def test_target_secret_is_redacted_in_durable_record_and_listing(approval_db):
    """Durable record + listing must not expose a target-embedded secret."""
    from machina.core.session import session_manager
    from machina.permissions import store
    from machina.security.secrets import remember_secret_value

    remember_secret_value(SECRET)
    session = await session_manager.create(title="r4-105-target-listing", agent_id="harness")
    req = _session_request(
        str(session.id),
        # Review-required command; the secret lives ONLY in the target.
        argv=["bash", "-lc", "curl -sS https://api.example.com/v1/export -o /tmp/export.json"],
        targets=[f"https://api.example.com/v1/export?api_key={SECRET}"],
    )
    captured: list[dict] = []
    await _pause_at_handler(req, captured)

    record = await store.get_approval(req.request_id)
    assert record is not None
    _assert_redacted(record, "durable get_approval record (targets)")

    pending = await store.list_pending(str(session.id))
    listed = [row for row in pending if row["request_id"] == req.request_id]
    assert listed, "pending approval missing from the listing"
    _assert_redacted(listed[0], "list_pending/gateway/REST listing (targets)")
