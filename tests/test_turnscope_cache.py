"""Stream A: turn-scoped mode resolution, per-turn grant cache, usage fast path.

Cost seams only — zero permission-verdict change. All DB access is mocked;
async patterns follow tests/test_permissions_store_regressions.py.
"""

from __future__ import annotations

import uuid

import pytest


@pytest.fixture
def clean_turn():
    from machina.core import session_mode
    from machina.permissions import broker, store

    broker.set_approval_handler(None)
    session_mode.reset_for_tests()
    store.clear_grant_cache()
    try:
        yield
    finally:
        broker.set_approval_handler(None)
        session_mode.reset_for_tests()
        store.clear_grant_cache()


class ModeStubDb:
    """Mocked durable mode authority with a fetch counter."""

    connected = True

    def __init__(self, mode: str = "workspace"):
        self.mode = mode
        self.fetchrows = 0

    async def fetchrow(self, query, _sid):
        self.fetchrows += 1
        assert "execution_mode" in query
        return {"execution_mode": self.mode}


def _grant_row(grant_id: str, session_id: str) -> dict:
    return {
        "id": grant_id,
        "session_id": session_id,
        "agent_id": "harness",
        "mode": "full",
        "capability": "session",
        "scope_path": None,
        "scope_type": "file",
        "grant_kind": "session",
        "digest": "full-mode-session",
        "expires_at": None,
        "revoked": False,
    }


class GrantStubDb:
    """Mocked durable grant authority; counts grant SELECTs only."""

    def __init__(self, grants: list[dict]):
        self.grants = grants
        self.grant_selects = 0
        self.approval_inserts = 0

    async def fetch(self, query, *_args):
        if "permission_grants" in query:
            self.grant_selects += 1
            return [dict(g) for g in self.grants]
        if "permission_approvals" in query:
            return []
        raise AssertionError(f"unexpected fetch: {query}")

    async def execute(self, query, *_args):
        if "permission_grants" in query:
            return "UPDATE 1"
        raise AssertionError(f"unexpected execute: {query}")

    def acquire(self):
        outer = self

        class _Conn:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def execute(self, query, *_args):
                if "permission_approvals" in query:
                    outer.approval_inserts += 1
                    return "INSERT 1"
                return await outer.execute(query, *_args)

            def transaction(self):
                return self

            async def fetchrow(self, query, *_args):
                if "permission_approvals" in query:
                    if "FOR UPDATE" in query:
                        return {
                            "status": "pending",
                            "consumed": False,
                            "agent_id": "someone-else",
                        }
                    return {"request_id": "r1", "status": "approved"}
                raise AssertionError(f"unexpected fetchrow: {query}")

        return _Conn()


# ─── A1: turn-scoped mode resolution ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_turn_scope_resolves_mode_once_per_turn(clean_turn, monkeypatch):
    from machina.core import session_mode

    sid = str(uuid.uuid4())
    stub = ModeStubDb("workspace")
    monkeypatch.setattr("machina.db.connection.db", stub)
    with session_mode.turn_scope():
        for _ in range(5):
            assert await session_mode.resolve_effective_mode(sid) == "workspace"
    assert stub.fetchrows == 1


@pytest.mark.asyncio
async def test_turn_scope_invalidated_on_mode_change(clean_turn, monkeypatch):
    from machina.core import session_mode

    sid = str(uuid.uuid4())
    stub = ModeStubDb("workspace")
    monkeypatch.setattr("machina.db.connection.db", stub)
    with session_mode.turn_scope():
        assert await session_mode.resolve_effective_mode(sid) == "workspace"
        assert stub.fetchrows == 1
        session_mode.set_session_mode(sid, "full")
        assert await session_mode.resolve_effective_mode(sid) == "workspace"
        assert stub.fetchrows == 2
        session_mode.clear_session_mode(sid)
        assert await session_mode.resolve_effective_mode(sid) == "workspace"
        assert stub.fetchrows == 3


@pytest.mark.asyncio
async def test_execution_context_resolves_once_per_turn(clean_turn, monkeypatch):
    from machina.core import execution_context as ec
    from machina.core import session_mode

    sid = str(uuid.uuid4())
    stub = ModeStubDb("sandbox")
    monkeypatch.setattr("machina.db.connection.db", stub)
    with session_mode.turn_scope():
        for _ in range(4):
            ctx = await ec.resolve_execution_context(sid, "terminal")
            assert ctx.mode == "sandbox"
            assert ctx.backend == "sandbox"
    assert stub.fetchrows == 1


@pytest.mark.asyncio
async def test_implicit_broker_turn_caches_mode(clean_turn, monkeypatch):
    from machina.core import session_mode
    from machina.permissions import broker

    sid = str(uuid.uuid4())
    stub = ModeStubDb("workspace")
    monkeypatch.setattr("machina.db.connection.db", stub)
    broker.set_approval_handler(None, principal="tui", turn_id="T1")
    try:
        for _ in range(3):
            assert await session_mode.resolve_effective_mode(sid) == "workspace"
        assert stub.fetchrows == 1
        broker.set_approval_handler(None, principal="tui", turn_id="T2")
        assert await session_mode.resolve_effective_mode(sid) == "workspace"
        assert stub.fetchrows == 2
    finally:
        broker.set_approval_handler(None)


@pytest.mark.asyncio
async def test_no_turn_no_cache_every_call_hits_db(clean_turn, monkeypatch):
    from machina.core import session_mode

    sid = str(uuid.uuid4())
    stub = ModeStubDb("ask")
    monkeypatch.setattr("machina.db.connection.db", stub)
    assert await session_mode.resolve_effective_mode(sid) == "ask"
    assert await session_mode.resolve_effective_mode(sid) == "ask"
    assert stub.fetchrows == 2


@pytest.mark.asyncio
async def test_db_unconnected_sync_fallback_works_in_scope(clean_turn, monkeypatch):
    from machina.core import session_mode

    sid = str(uuid.uuid4())

    class Down:
        connected = False

    monkeypatch.setattr("machina.db.connection.db", Down())
    with session_mode.turn_scope():
        assert await session_mode.resolve_effective_mode(sid) == "ask"
        session_mode.set_session_mode(sid, "full")
        assert await session_mode.resolve_effective_mode(sid) == "full"


@pytest.mark.asyncio
async def test_db_failure_never_poisons_turn_cache(clean_turn, monkeypatch):
    from machina.core import session_mode

    sid = str(uuid.uuid4())

    class Flaky:
        connected = True
        calls = 0

        async def fetchrow(self, query, _sid):
            type(self).calls += 1
            if type(self).calls == 1:
                raise RuntimeError("database unavailable")
            return {"execution_mode": "workspace"}

    stub = Flaky()
    monkeypatch.setattr("machina.db.connection.db", stub)
    with session_mode.turn_scope():
        with pytest.raises(RuntimeError, match="database unavailable"):
            await session_mode.resolve_effective_mode(sid)
        assert await session_mode.resolve_effective_mode(sid) == "workspace"
        assert stub.calls == 2


# ─── A2: per-turn grant cache ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_grant_cache_per_turn_session(clean_turn, monkeypatch):
    from machina.permissions import broker, store

    sid = str(uuid.uuid4())
    other = str(uuid.uuid4())
    gid = str(uuid.uuid4())
    stub = GrantStubDb([_grant_row(gid, sid)])

    async def fake_db():
        return stub

    monkeypatch.setattr(store, "_db", fake_db)
    broker.set_approval_handler(None, principal="tui", turn_id="G1")
    try:
        first = await store.list_grants(sid)
        second = await store.list_grants(sid)
        assert first == second and first is not second
        assert stub.grant_selects == 1
        await store.list_grants(other)
        assert stub.grant_selects == 2
        broker.set_approval_handler(None, principal="tui", turn_id="G2")
        await store.list_grants(sid)
        assert stub.grant_selects == 3
    finally:
        broker.set_approval_handler(None)


@pytest.mark.asyncio
async def test_grant_cache_invalidated_on_save_and_revoke(clean_turn, monkeypatch):
    from machina.permissions import broker, store

    sid = str(uuid.uuid4())
    gid = str(uuid.uuid4())
    stub = GrantStubDb([_grant_row(gid, sid)])

    async def fake_db():
        return stub

    monkeypatch.setattr(store, "_db", fake_db)
    broker.set_approval_handler(None, principal="tui", turn_id="G3")
    try:
        assert len(await store.list_grants(sid)) == 1
        assert stub.grant_selects == 1
        assert len(await store.list_grants(sid)) == 1
        assert stub.grant_selects == 1
        await store.save_grant(
            {
                "session_id": sid,
                "agent_id": "harness",
                "mode": "ask",
                "capability": "op",
                "digest": "d2",
            }
        )
        await store.list_grants(sid)
        assert stub.grant_selects == 2
        await store.revoke_grants(sid)
        await store.list_grants(sid)
        assert stub.grant_selects == 3
    finally:
        broker.set_approval_handler(None)


@pytest.mark.asyncio
async def test_approval_rows_never_touch_grant_cache(clean_turn, monkeypatch):
    """Approval creation must neither serve from nor poison the grant cache."""
    from machina.permissions import broker, store
    from machina.permissions.policy import build_request

    sid = str(uuid.uuid4())
    gid = str(uuid.uuid4())
    stub = GrantStubDb([_grant_row(gid, sid)])

    async def fake_db():
        return stub

    monkeypatch.setattr(store, "_db", fake_db)
    broker.set_approval_handler(None, principal="tui", turn_id="G4")
    try:
        assert len(await store.list_grants(sid)) == 1
        assert stub.grant_selects == 1
        req = build_request(
            operation="process.exec",
            argv=["echo", "hi"],
            mode="ask",
            session_id=sid,
            agent_id="harness",
        )
        await store.create_approval(req, principal="tui")
        assert stub.approval_inserts == 1
        # Grant read still served from cache; approvals live elsewhere.
        assert len(await store.list_grants(sid)) == 1
        assert stub.grant_selects == 1
        pending = await store.list_pending(sid)
        assert isinstance(pending, list)
        assert stub.grant_selects == 1
    finally:
        broker.set_approval_handler(None)


@pytest.mark.asyncio
async def test_decision_write_invalidates_grant_cache(clean_turn, monkeypatch):
    from machina.permissions import broker, store

    sid = str(uuid.uuid4())
    gid = str(uuid.uuid4())
    request_id = uuid.uuid4().hex
    stub = GrantStubDb([_grant_row(gid, sid)])

    async def fake_db():
        return stub

    monkeypatch.setattr(store, "_db", fake_db)
    store._mem.approvals[request_id] = {
        "request_id": request_id,
        "session_id": sid,
        "status": "pending",
        "consumed": False,
        "agent_id": "someone-else",
    }
    broker.set_approval_handler(None, principal="tui", turn_id="G5")
    try:
        assert len(await store.list_grants(sid)) == 1
        assert stub.grant_selects == 1
        resolved = await store.resolve_decision(request_id, "approved", principal="human")
        assert resolved is not None
        await store.list_grants(sid)
        assert stub.grant_selects == 2
    finally:
        broker.set_approval_handler(None)
        store._mem.approvals.pop(request_id, None)


# ─── A3: usage reserve estimation ─────────────────────────────────────────────
# Reservations always reflect the full payload estimate (a gateway test pins
# reserved_tokens covering image/context tokens). An earlier fast path that
# skipped estimation with limits off broke that accounting contract and was
# reverted; estimation runs on every connected reserve.


class _UsageTx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _UsageConn:
    def __init__(self):
        self.reserved_seen: list = []

    def transaction(self):
        return _UsageTx()

    async def execute(self, query, *_args):
        return "SELECT 1"

    async def fetchrow(self, query, *args):
        if "RETURNING id" in query:
            self.reserved_seen.append(args[4])
            return {"id": uuid.uuid4()}
        if "ancestor_session_ids" in query:
            return {"session_ids": [], "agent_ids": []}
        raise AssertionError(f"unexpected fetchrow: {query}")

    async def fetch(self, query, *_args):
        return []


class _UsageAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class UsageStubDb:
    connected = True

    def __init__(self):
        self.conn = _UsageConn()

    def acquire(self):
        return _UsageAcquire(self.conn)


@pytest.mark.asyncio
async def test_reserve_skips_estimation_when_limits_off_unconnected(clean_turn, monkeypatch):
    import machina.core.usage as usage_mod

    called = {"n": 0}

    def boom(messages, tools, max_tokens):
        called["n"] += 1
        raise AssertionError("estimation must be skipped when limits are off")

    class Down:
        connected = False

    monkeypatch.setattr("machina.core.usage.db", Down())
    monkeypatch.setattr(usage_mod, "_estimate_tokens", boom)
    store = usage_mod.UsageStore()
    assert await store.reserve(uuid.uuid4(), "a", "p", "m", [], [], 8) is None
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_reserve_estimates_even_when_limits_off_connected(clean_turn, monkeypatch):
    import machina.core.usage as usage_mod

    real = usage_mod._estimate_tokens
    called = {"n": 0}

    def spy(messages, tools, max_tokens):
        called["n"] += 1
        return real(messages, tools, max_tokens)

    stub = UsageStubDb()
    monkeypatch.setattr("machina.core.usage.db", stub)
    monkeypatch.setattr(usage_mod, "_estimate_tokens", spy)
    store = usage_mod.UsageStore()
    messages = [{"role": "user", "content": "hello"}]
    rid = await store.reserve(uuid.uuid4(), "a", "p", "m", messages, [], 32)
    assert rid is not None
    assert called["n"] == 1
    assert stub.conn.reserved_seen == [real(messages, [], 32)]


@pytest.mark.asyncio
async def test_reserve_exact_when_limits_on(clean_turn, monkeypatch):
    import machina.core.usage as usage_mod
    from machina.core.config import config

    real = usage_mod._estimate_tokens
    called = {"n": 0}

    def spy(messages, tools, max_tokens):
        called["n"] += 1
        return real(messages, tools, max_tokens)

    stub = UsageStubDb()
    monkeypatch.setattr("machina.core.usage.db", stub)
    monkeypatch.setattr(usage_mod, "_estimate_tokens", spy)
    config.set("usage_session_token_limit", 10_000_000)
    try:
        store = usage_mod.UsageStore()
        messages = [{"role": "user", "content": "hello"}]
        rid = await store.reserve(uuid.uuid4(), "a", "p", "m", messages, [], 8)
        assert rid is not None
        assert called["n"] == 1
        assert stub.conn.reserved_seen == [real(messages, [], 8)]
    finally:
        config.set("usage_session_token_limit", 0)


@pytest.mark.asyncio
async def test_reserve_db_unconnected_contract(clean_turn, monkeypatch):
    import machina.core.usage as usage_mod
    from machina.core.config import config
    from machina.core.exceptions import DatabaseError

    class Down:
        connected = False

    monkeypatch.setattr("machina.core.usage.db", Down())
    store = usage_mod.UsageStore()
    assert await store.reserve(uuid.uuid4(), "a", "p", "m", [], [], 8) is None
    config.set("usage_agent_request_limit", 5)
    try:
        with pytest.raises(DatabaseError):
            await store.reserve(uuid.uuid4(), "a", "p", "m", [], [], 8)
    finally:
        config.set("usage_agent_request_limit", 0)


# ─── A4: sectioned prompt accounting (diagnostics only) ───────────────────────


def test_section_counts_match_tokenizer_and_inputs_unmutated(clean_turn):
    from machina.core.assembler import get_token_count
    from machina.core.execution_context import count_prompt_sections

    sections = {
        "system": "You are a helpful agent.",
        "skills": "skill: foo — use for bar",
        "tools": '{"name": "read", "args": {}}',
        "recent": "user asked about tests",
        "retrieved": "memory: prefers pytest",
        "query": "run the suite",
    }
    snapshot = dict(sections)
    counts = count_prompt_sections(sections)
    assert sections == snapshot
    for name, text in sections.items():
        assert counts[name] == get_token_count(text)


def test_section_summary_is_single_line(clean_turn):
    from machina.core.execution_context import (
        count_prompt_sections,
        summarize_section_tokens,
    )

    counts = count_prompt_sections({"system": "hi", "query": "yo"})
    summary = summarize_section_tokens(counts)
    assert "\n" not in summary
    assert "system" in summary and "query" in summary
    assert "prompt" in summary
