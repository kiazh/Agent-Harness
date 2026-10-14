"""R4-065..R4-068 (round-4): approval deadline truthfulness and store ordering.

Primary proofs (E2E / adversarial — see FIX_PLAN_ROUND4_TASKS.md matrix):

- R4-065 (E2E, compressed approval timeout): the REAL gateway turn + REAL
  broker + REAL store must emit an ``approval.resolved``/``permission.resolved``
  frame with status ``expired`` on the same channel as human decisions, so the
  UI can retire the card without deciding.
- R4-066 (adversarial timing, N>=5): a decision that lands before the deadline
  must be honoured from the durable record and executed exactly once — never
  reported as "expired", never replayable afterwards.
- R4-067 (E2E audit pipeline): routine allowed writes/execs carry bounded,
  redacted target/argv context in the real audit entry (sanitizer included).
- R4-068 (durable failure injection): a failed INSERT leaves no in-memory
  pending orphan; a failed/absent durable completion leaves no memory-only
  terminal state.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from unittest.mock import AsyncMock

import pytest

TEST_DSN = os.environ.get("MACHINA_TEST_DATABASE_URL", "")
needs_test_db = pytest.mark.skipif(not TEST_DSN, reason="MACHINA_TEST_DATABASE_URL not set")

# Same isolated-database seam as tests/test_approvals_ownership.py: patch every
# module-level ``db`` handle so the real paths run against machina_test.
_MODULES_WITH_DB = [
    "machina.db.connection",
    "machina.core.session",
    "machina.core.context",
    "machina.core.scheduler",
    "machina.core.orchestrator",
    "machina.core.agent_def",
    "machina.core.usage",
    "machina.gateway.server",
    "machina.rag.pipeline",
    "machina.tools.agents",
    "machina.memory.store",
]


@pytest.fixture
async def isolated_db(monkeypatch):
    from machina.db.connection import Database

    tdb = Database(dsn=TEST_DSN)
    await tdb.connect()
    await tdb.initialize_schema()
    for mod in _MODULES_WITH_DB:
        monkeypatch.setattr(f"{mod}.db", tdb, raising=False)
    yield tdb
    try:
        await tdb.execute("DELETE FROM sessions WHERE title LIKE 'r4-approval%'")
    except Exception:
        pass
    await tdb.close()


async def _session(title="r4-approval"):
    from machina.core.session import session_manager

    return await session_manager.create(title=title)


def _req(**kw):
    from machina.permissions.policy import build_request

    base = dict(
        operation="file.write",
        targets=["/proj/note.txt"],
        content="hello",
        mode="ask",
        agent_id="harness",
    )
    base.update(kw)
    return build_request(**base)


def _events(frames: list[dict]) -> list[dict]:
    return [frame["params"] for frame in frames if frame.get("method") == "event"]


# ─── R4-065: expiry resolution frame (E2E) ─────────────────────────────────


class _PausedAgent:
    """Fake agent that drives the REAL broker guard inside a real gateway turn."""

    def __init__(self, session_id: str, *, argv: list[str] | None = None) -> None:
        self.session_id = session_id
        self.argv = argv or ["git", "reset", "--hard"]
        self.pauses: list[dict] = []
        self.allowed = False

    async def run_stream(self, *_args, **_kwargs):
        from machina.core.models import AgentResponse, StreamEvent
        from machina.permissions.broker import NeedsApproval, permission_broker
        from machina.permissions.policy import build_request

        req = build_request(
            operation="process.exec",
            argv=list(self.argv),
            cwd=".",
            mode="ask",
            agent_id="r4-approval",
            session_id=self.session_id,
        )
        try:
            await permission_broker.guard(req)
            self.allowed = True
        except NeedsApproval as pause:
            self.pauses.append(pause.approval)
        yield StreamEvent(
            type="done",
            response=AgentResponse(content="paused", needs_approval=self.pauses),
        )


async def _drive_expiry(gateway, session, turn_id: str) -> None:
    """Run one gateway turn whose only tool pauses for an approval that expires."""
    turn = asyncio.create_task(gateway._run_turn_with_timeout(session.id, turn_id, "work"))
    gateway._turns[str(session.id)] = turn
    try:
        await asyncio.wait_for(asyncio.shield(turn), timeout=20)
    finally:
        if not turn.done():
            turn.cancel()
            await asyncio.gather(turn, return_exceptions=True)


def _assert_expiry_contract(frames: list[dict]) -> None:
    events = _events(frames)
    cards = [
        event for event in events if event["type"] in ("needs_approval", "permission.required")
    ]
    assert cards, f"the approval card must reach the transport: {events}"
    request_id = cards[-1]["requestId"]
    resolved = [
        event for event in events if event["type"] in ("approval.resolved", "permission.resolved")
    ]
    assert resolved, (
        "R4-065: the broker-recorded expiry must be emitted on the decision "
        f"channel; frames were: {[(e['type'], e.get('status')) for e in events]}"
    )
    assert resolved[-1]["status"] == "expired", resolved[-1]
    assert resolved[-1]["requestId"] == request_id, resolved[-1]
    # Ordering matters to the UI: resolution after the card, never before.
    assert events.index(cards[-1]) < events.index(resolved[-1])
    assert not any(event["type"] == "error" for event in events), events
    assert any(event["type"] == "message.complete" for event in events), events


async def test_expiry_emits_resolution_frame_memory_store(monkeypatch):
    """E2E without a DB: real gateway turn, real broker, memory-store fallback."""
    from machina import services
    from machina.core.config import config
    from machina.core.models import Session
    from machina.core.session import session_manager
    from machina.gateway.server import Gateway
    from machina.permissions import store as st

    async def no_db():
        return None

    monkeypatch.setattr(st, "_db", no_db)
    st._mem.approvals.clear()
    monkeypatch.setattr(config, "approval_timeout", 0.2)
    monkeypatch.setattr(services, "maybe_auto_compact", AsyncMock())
    session = Session(id=uuid.uuid4(), agent_id="r4-approval")
    monkeypatch.setattr(session_manager, "get_fresh", AsyncMock(return_value=session))

    frames: list[dict] = []
    agent_holder: dict[str, _PausedAgent] = {}

    def factory(_model: str, _provider: str) -> _PausedAgent:
        agent_holder["agent"] = _PausedAgent(str(session.id))
        return agent_holder["agent"]

    gateway = Gateway(lambda frame: frames.append(frame), agent_factory=factory, owns_db=False)
    await _drive_expiry(gateway, session, "r4-065-memory")
    agent = agent_holder["agent"]
    assert agent.allowed is False
    assert agent.pauses and agent.pauses[0]["status"] == "expired"
    _assert_expiry_contract(frames)
    request_id = agent.pauses[0]["request_id"]
    record = await st.get_approval(request_id)
    assert record is not None and record["status"] == "expired"
    st._mem.approvals.clear()


@needs_test_db
async def test_expiry_emits_resolution_frame_durable_store(isolated_db, monkeypatch):
    """E2E on the real store: the durable row is expired before the frame lands."""
    from machina import services
    from machina.core.config import config
    from machina.gateway.server import Gateway
    from machina.permissions import store as st

    monkeypatch.setattr(config, "approval_timeout", 0.2)
    monkeypatch.setattr(services, "maybe_auto_compact", AsyncMock())
    session = await _session(title="r4-approval-expiry-e2e")

    frames: list[dict] = []
    agent_holder: dict[str, _PausedAgent] = {}

    def factory(_model: str, _provider: str) -> _PausedAgent:
        agent_holder["agent"] = _PausedAgent(str(session.id))
        return agent_holder["agent"]

    gateway = Gateway(lambda frame: frames.append(frame), agent_factory=factory, owns_db=False)
    await _drive_expiry(gateway, session, "r4-065-durable")
    agent = agent_holder["agent"]
    assert agent.allowed is False
    _assert_expiry_contract(frames)
    request_id = agent.pauses[0]["request_id"]
    record = await st.get_approval(request_id)
    assert record is not None and record["status"] == "expired", record
    assert await st.find_pending(str(session.id), agent.pauses[0].get("digest", "")) is None


# ─── R4-066: decide-at-deadline race (adversarial, N>=5) ───────────────────


@needs_test_db
@pytest.mark.parametrize("round_index", range(5))
async def test_resolve_before_deadline_is_honoured_exactly_once(
    isolated_db, monkeypatch, round_index
):
    """Adversarial: the decision lands durably, the transport stalls past the
    deadline, wait_for cancels it. Durable truth is "approved" — the broker
    must claim and execute it exactly once, never report "expired", and a
    retry of the same request must not replay the approval.
    """
    from machina.core.config import config
    from machina.permissions import broker as brokermod
    from machina.permissions import store as st
    from machina.permissions.broker import ApprovalDenied

    session = await _session(title=f"r4-approval-race-{round_index}")
    req = _req(session_id=str(session.id), content=f"race-{round_index}")
    monkeypatch.setattr(config, "approval_timeout", 0.05 + 0.02 * round_index)
    calls: list[str] = []

    async def decision_then_stall(card):
        calls.append(card["request_id"])
        # The human decision commits to the durable row first ...
        assert await st.resolve_decision(card["request_id"], "approved", principal="tui")
        # ... and the transport has not returned when the deadline fires.
        await asyncio.sleep(30)
        return "approved"

    brokermod.set_approval_handler(
        decision_then_stall, principal="tui", turn_id=f"race-{round_index}"
    )
    try:
        out = await brokermod.permission_broker.guard(req)
        assert out.approval_id == req.request_id, "durable approval must win over a stale expiry"
        record = await st.get_approval(req.request_id)
        assert record is not None and record["status"] == "claimed", record
        assert record["consumed"] is True
        # No stale-approved replay: complete once, then the same request is
        # terminally denied and the handler is never invoked again.
        assert await brokermod.permission_broker.complete(req.request_id, "completed") is True
        assert await st.find_approved_unclaimed(str(session.id), req.digest) is None
        with pytest.raises(ApprovalDenied):
            await brokermod.permission_broker.guard(req)
        assert len(calls) == 1, calls
    finally:
        brokermod.set_approval_handler(None)


@needs_test_db
@pytest.mark.parametrize("jitter", [0.0, 0.5, 0.9, 0.99, 1.0])
async def test_decide_at_deadline_invariant_never_mismatches_durable(
    isolated_db, monkeypatch, jitter
):
    """Adversarial timing (N=5): the reported outcome always matches the
    durable row — an approval that lands at/just before the deadline is
    honoured; a genuine expiry is exactly that. Never "expired" over an
    approved/claimed row.
    """
    from machina.core.config import config
    from machina.permissions import broker as brokermod
    from machina.permissions import store as st
    from machina.permissions.broker import ApprovalDenied, NeedsApproval

    timeout = 0.06
    session = await _session(title=f"r4-approval-jitter-{jitter}")
    req = _req(session_id=str(session.id), content=f"jitter-{jitter}")
    monkeypatch.setattr(config, "approval_timeout", timeout)

    async def decide_at_deadline(card):
        await asyncio.sleep(timeout * jitter)
        await st.resolve_decision(card["request_id"], "approved", principal="tui")
        return "approved"

    brokermod.set_approval_handler(decide_at_deadline, principal="tui", turn_id=f"jitter-{jitter}")
    allowed = False
    try:
        try:
            await brokermod.permission_broker.guard(req)
            allowed = True
        except NeedsApproval as pause:
            assert pause.approval.get("status") == "expired", pause.approval
        except ApprovalDenied as denied:  # pragma: no cover - construction cannot deny
            pytest.fail(f"unexpected denial: {denied}")
    finally:
        brokermod.set_approval_handler(None)
    record = await st.get_approval(req.request_id)
    assert record is not None
    if allowed:
        assert record["status"] == "claimed", record
        assert record["consumed"] is True
        await brokermod.permission_broker.complete(req.request_id, "completed")
    else:
        assert record["status"] == "expired", (
            f"R4-066: expiry reported while durable truth is {record['status']!r}"
        )
    print(f"[R4-066] jitter={jitter} timeout={timeout} allowed={allowed}")


# ─── R4-067: allowed-action audit context ──────────────────────────────────


@pytest.fixture
def audit_entries(monkeypatch):
    """Capture entries at the persistence sink; the real audit pipeline runs."""
    entries: list[dict] = []
    monkeypatch.setattr("machina.observability.audit.audit_persistence.submit", entries.append)
    return entries


async def test_routine_allowed_rows_carry_bounded_redacted_target_context(
    audit_entries, monkeypatch, tmp_path
):
    from machina.core.config import config
    from machina.permissions import store as st
    from machina.permissions.broker import permission_broker

    async def no_db():
        return None

    monkeypatch.setattr(st, "_db", no_db)
    st._mem.grants.clear()
    st.clear_grant_cache()
    monkeypatch.setattr(config, "workspace_root", str(tmp_path))
    monkeypatch.setattr(config, "execution_mode", "ask")
    session_id = str(uuid.uuid4())

    # Routine workspace write with a benign path: the target must be present.
    benign = tmp_path / "note.txt"
    benign_req = _req_with(session_id, operation="file.write", targets=[str(benign)], content="hi")
    await permission_broker.guard(benign_req)
    benign_row = _row(audit_entries, benign_req.request_id)
    assert benign_row["targets"] == [str(benign)]
    assert benign_row["argv"] == []

    # Routine exec (echo) whose argv carries a bearer token: bounded preview,
    # no raw secret.
    secret = "sk-ant-api03-" + "q" * 30
    exec_req = _req_with(
        session_id,
        operation="process.exec",
        argv=["echo", "Authorization: Bearer " + secret],
        cwd=str(tmp_path),
    )
    await permission_broker.guard(exec_req)
    exec_row = _row(audit_entries, exec_req.request_id)
    assert exec_row["argv"][0] == "echo"
    assert len(exec_row["argv"]) == 2
    assert "Authorization" in json.dumps(exec_row)
    blob = json.dumps(exec_row)
    assert secret not in blob, blob
    assert any("REDACTED" in value for value in exec_row["argv"]), exec_row["argv"]

    # Routine write whose *file name* is secret-shaped: redacted, never raw.
    hostile = tmp_path / f"{secret}.txt"
    hostile_req = _req_with(session_id, operation="file.write", targets=[str(hostile)], content="x")
    await permission_broker.guard(hostile_req)
    hostile_row = _row(audit_entries, hostile_req.request_id)
    assert hostile_row["targets"], hostile_row
    assert hostile_row["targets"][0].endswith(".txt")
    assert "REDACTED" in hostile_row["targets"][0]
    assert secret not in json.dumps(hostile_row), hostile_row

    # Bounded: at most three previews per list, each length-capped with an
    # explicit hidden-count marker.
    long_name = "l" * 240 + ".txt"
    many = [tmp_path / f"bounded-{index}.txt" for index in range(4)] + [tmp_path / long_name]
    bounded_req = _req_with(
        session_id, operation="file.write", targets=[str(t) for t in many], content="x"
    )
    await permission_broker.guard(bounded_req)
    bounded_row = _row(audit_entries, bounded_req.request_id)
    assert len(bounded_row["targets"]) == 3, bounded_row["targets"]
    assert len(bounded_row["argv"]) <= 3
    exec_many = _req_with(
        session_id,
        operation="process.exec",
        argv=["echo", "a", "b", "c", "d", "e"],
        cwd=str(tmp_path),
    )
    await permission_broker.guard(exec_many)
    exec_many_row = _row(audit_entries, exec_many.request_id)
    assert len(exec_many_row["argv"]) == 3, exec_many_row["argv"]

    long_req = _req_with(
        session_id, operation="file.write", targets=[str(tmp_path / long_name)], content="x"
    )
    await permission_broker.guard(long_req)
    long_row = _row(audit_entries, long_req.request_id)
    assert "…(+" in long_row["targets"][0], long_row["targets"]


def _req_with(session_id: str, **kw):
    kw.setdefault("agent_id", "harness")
    kw.setdefault("mode", "ask")
    kw["session_id"] = session_id
    return _req(**kw)


def _row(entries: list[dict], request_id: str) -> dict:
    rows = [
        entry
        for entry in entries
        if entry["event"] == "permission_allowed" and entry.get("request_id") == request_id
    ]
    assert rows, [(entry["event"], entry.get("request_id")) for entry in entries]
    return rows[-1]


# ─── R4-068: store ordering / failure injection ────────────────────────────


class _BrokenInsertDB:
    """DB boundary whose reads succeed and whose approval INSERT fails."""

    connected = True

    async def fetchrow(self, *_args, **_kwargs):
        return None

    async def fetch(self, *_args, **_kwargs):
        return []

    def acquire(self):
        class _Conn:
            async def execute(self, *_args, **_kwargs):
                raise RuntimeError("injected insert failure")

        class _CM:
            async def __aenter__(self):
                return _Conn()

            async def __aexit__(self, *_exc):
                return False

        return _CM()


async def test_create_approval_insert_failure_leaves_no_orphan(monkeypatch):
    """Failure injection: a failed durable INSERT must not leave a pending
    in-memory record nothing can resolve."""
    from machina.permissions import broker as brokermod
    from machina.permissions import store as st

    async def broken_db():
        return _BrokenInsertDB()

    monkeypatch.setattr(st, "_db", broken_db)
    st._mem.approvals.clear()

    async def handler(card):  # never reached: creation fails first
        return "approved"

    req = _req(session_id=str(uuid.uuid4()), content="insert-failure")
    brokermod.set_approval_handler(handler, principal="tui", turn_id="r4-068-insert")
    try:
        with pytest.raises(RuntimeError, match="injected insert failure"):
            await brokermod.permission_broker.guard(req)
    finally:
        brokermod.set_approval_handler(None)
    assert req.request_id not in st._mem.approvals, "no orphaned pending record"
    assert await st.get_approval(req.request_id) is None


@needs_test_db
async def test_complete_execution_durable_first_under_failure(isolated_db, monkeypatch):
    """Failure injection: when the durable completion fails, the memory mirror
    must NOT report a terminal state the durable row never saw."""
    from machina.permissions import store as st

    session = await _session(title="r4-approval-complete")
    req = _req(session_id=str(session.id), content="complete-failure")
    await st.create_approval(req, principal="tui")
    assert await st.resolve_decision(req.request_id, "approved", principal="tui")
    assert await st.claim_execution(req.request_id, digest=req.digest, session_id=str(session.id))
    assert st._mem.approvals[req.request_id]["status"] == "claimed"

    real_db = await st._db()

    class _FailingAcquire:
        async def fetchrow(self, *args, **kwargs):
            return await real_db.fetchrow(*args, **kwargs)

        async def fetch(self, *args, **kwargs):
            return await real_db.fetch(*args, **kwargs)

        def acquire(self):
            raise RuntimeError("injected durable completion failure")

    async def failing():
        return _FailingAcquire()

    monkeypatch.setattr(st, "_db", failing)
    with pytest.raises(RuntimeError, match="injected durable completion failure"):
        await st.complete_execution(req.request_id, "completed")
    assert st._mem.approvals[req.request_id]["status"] == "claimed", (
        "memory advanced before durable confirmation (R4-068)"
    )
    assert (await st.get_approval(req.request_id))["status"] == "claimed"

    # With the failure removed, durable confirmation mirrors into memory once.
    async def restored():
        return real_db

    monkeypatch.setattr(st, "_db", restored)
    assert await st.complete_execution(req.request_id, "completed") is True
    assert st._mem.approvals[req.request_id]["status"] == "completed"
    assert await st.complete_execution(req.request_id, "completed") is False


@needs_test_db
async def test_complete_execution_refuses_durable_record_without_db(isolated_db, monkeypatch):
    """A durable-origin approval must not be completed from the memory mirror
    while the durable authority is unreachable."""
    from machina.permissions import store as st

    session = await _session(title="r4-approval-offline")
    req = _req(session_id=str(session.id), content="offline-complete")
    await st.create_approval(req, principal="tui")
    assert await st.resolve_decision(req.request_id, "approved", principal="tui")
    assert await st.claim_execution(req.request_id, digest=req.digest, session_id=str(session.id))

    async def no_db():
        return None

    monkeypatch.setattr(st, "_db", no_db)
    assert await st.complete_execution(req.request_id, "completed") is False
    assert st._mem.approvals[req.request_id]["status"] == "claimed"


async def test_memory_only_completion_still_works_without_db(monkeypatch):
    """Control: a memory-authority record (DB down at creation) still completes."""
    from machina.permissions import store as st

    async def no_db():
        return None

    monkeypatch.setattr(st, "_db", no_db)
    st._mem.approvals.clear()
    req = _req(session_id=str(uuid.uuid4()), content="memory-complete")
    await st.create_approval(req, principal="tui")
    assert st._mem.approvals[req.request_id]["durable"] is False
    await st.resolve_decision(req.request_id, "approved", principal="tui")
    await st.claim_execution(req.request_id, digest=req.digest, session_id=req.session_id)
    assert await st.complete_execution(req.request_id, "completed") is True
    st._mem.approvals.clear()
