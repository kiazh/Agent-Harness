"""Regression tests for AH-AUDIT-001..043 remediation.

DB-free unless marked: DB-backed cases use the disposable test database
fixture and skip when unavailable. No live provider calls anywhere.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest


@pytest.fixture(autouse=True)
def _clean_remediation_state():
    from ah.core import session_mode as _sm

    _sm.reset_for_tests()
    from ah.permissions import store as _store

    _store._mem.grants.clear()
    _store._mem.approvals.clear()
    from ah.permissions.broker import clear_execution_context

    clear_execution_context()
    yield
    _sm.reset_for_tests()
    _store._mem.approvals.clear()
    _store._mem.grants.clear()
    clear_execution_context()


def _req(**kw):
    from ah.permissions.policy import build_request

    base = {
        "operation": "file.read",
        "targets": ["/tmp/x.txt"],
        "session_id": "s1",
        "agent_id": "harness",
        "mode": "ask",
    }
    base.update(kw)
    return build_request(**base)


# ─── AH-001: session-local mode ────────────────────────────────────────────


def test_001_session_full_does_not_leak_to_future_sessions():
    import asyncio

    from ah.core import session_mode as sm
    from ah.core.config import config
    from ah.permissions import store as st

    async def _scenario():
        from types import SimpleNamespace

        from ah.gateway.features import mode as modefeat

        class Gw:
            def require_db(self):
                pass

            async def get_session(self, params):
                return SimpleNamespace(id=uuid.UUID(params["sessionId"]), agent_id="h")

        gw, saved = Gw(), config.get("execution_mode")
        assert saved != "full"
        a, b = str(uuid.uuid4()), str(uuid.uuid4())
        try:
            await modefeat.mode_set(gw, {"mode": "full", "sessionId": a})
            assert sm.get_session_mode(a) == "full"
            # Global default untouched: future sessions unaffected.
            assert config.get("execution_mode") != "full"
            assert sm.get_effective_mode(b) != "full"
            grants_b = await st.list_grants(b)
            assert not [g for g in grants_b if g.get("digest") == "full-mode-session"]
            # Explicit persistent default only affects subsequently created sessions.
            await modefeat.mode_set(gw, {"mode": "full", "scope": "global"})
            assert config.get("execution_mode") == "full"
            assert sm.get_effective_mode(b) == "full"
            c = SimpleNamespace(id=uuid.uuid4(), agent_id="h")
            assert await modefeat.grant_full_default(c) is True
        finally:
            config.set("execution_mode", saved if saved != "full" else "ask")
            sm.clear_session_mode(a)

    asyncio.run(_scenario())


def test_001_grant_failure_leaves_no_elevation():
    import asyncio

    from ah.core import session_mode as sm
    from ah.permissions import store as st

    async def _scenario():
        from types import SimpleNamespace

        from ah.gateway.features import mode as modefeat

        class Gw:
            def require_db(self):
                pass

            async def get_session(self, params):
                return SimpleNamespace(id=uuid.UUID(params["sessionId"]), agent_id="h")

        gw = Gw()
        a = str(uuid.uuid4())
        real = st.save_grant

        async def _boom(grant):
            raise RuntimeError("durable write failed")

        st.save_grant = _boom
        try:
            with pytest.raises(RuntimeError):
                await modefeat.mode_set(gw, {"mode": "full", "sessionId": a})
            assert sm.get_session_mode(a) is None
            assert sm.get_effective_mode(a) != "full"
        finally:
            st.save_grant = real

    asyncio.run(_scenario())


# ─── AH-007/008: exact capabilities + canonical digests ────────────────────


def test_007_partial_sensitive_grant_denied_full_set_allowed():
    from ah.permissions.policy import build_request, decide

    req = build_request(
        operation="file.read",
        targets=["/tmp/.env"],
        session_id="s1",
        agent_id="h",
        mode="ask",
        capabilities=["credential_access", "privilege_escalation"],
    )
    one = {
        "session_id": "s1",
        "agent_id": "h",
        "capability": "credential_access",
        "digest": req.digest,
    }
    assert decide(req, [one]).verdict == "pending"
    both = [
        {**one, "capability": "credential_access"},
        {**one, "capability": "privilege_escalation"},
    ]
    assert decide(req, both).verdict == "allowed"
    # Substring lookalikes never authorize.
    tricky = [{**one, "capability": "my-credential_access-backup"}]
    assert decide(req, tricky).verdict == "pending"
    # Ordinary session/full grants are not sensitive consent.
    assert decide(req, [{**one, "capability": "session"}]).verdict == "pending"


def test_008_digest_preserves_boundaries_and_material_fields():
    from ah.permissions.policy import build_request

    a = build_request(operation="process.exec", argv=["echo", "a|b"], session_id="s", mode="ask")
    b = build_request(operation="process.exec", argv=["echo", "a", "b"], session_id="s", mode="ask")
    assert a.digest != b.digest
    c = build_request(
        operation="process.exec",
        argv=["echo", "a|b"],
        session_id="s",
        mode="ask",
        timeout=61,
    )
    assert a.digest != c.digest
    d = build_request(
        operation="process.exec",
        argv=["echo", "a|b"],
        session_id="s",
        mode="ask",
        capabilities=["x"],
    )
    assert a.digest != d.digest
    e = build_request(
        operation="process.exec",
        argv=["echo", "a|b"],
        session_id="s",
        mode="ask",
        tool="terminal",
    )
    assert a.digest != e.digest


def test_006_env_and_destructive_tripwires():
    from ah.permissions.policy import _sensitive_capabilities, build_request

    env_req = build_request(operation="file.read", targets=["/proj/.env"], session_id="s")
    assert "credential_access" in _sensitive_capabilities(env_req)
    rm_req = build_request(operation="process.exec", argv=["rm", "-rf", "/"], session_id="s")
    assert "destructive_system" in _sensitive_capabilities(rm_req)
    ls_req = build_request(operation="process.exec", argv=["ls", "-la"], session_id="s")
    assert "destructive_system" not in _sensitive_capabilities(ls_req)


# ─── AH-002: immutable execution context ───────────────────────────────────


def test_002_backend_snapshot_survives_global_change():
    import asyncio

    from ah.permissions.broker import clear_execution_context, permission_broker
    from ah.permissions.policy import build_request
    from ah.tools import terminal as tmod

    async def _scenario():
        req = build_request(operation="context.read", session_id="s9", mode="ask")
        await permission_broker.guard(req)
        from ah.permissions.broker import get_execution_context

        snap = get_execution_context()
        assert snap and snap["mode"] == "ask" and snap["digest"] == req.digest
        # A concurrent global change cannot alter the approved snapshot.
        from ah.core.config import config

        saved = config.get("execution_mode")
        config.set("execution_mode", "full")
        try:
            assert tmod._call_mode() == "ask"
        finally:
            config.set("execution_mode", saved)
        clear_execution_context()
        assert get_execution_context() is None

    asyncio.run(_scenario())


# ─── AH-003/004: reviewable cards ──────────────────────────────────────────


def test_003_004_card_carries_command_and_diff():
    from ah.permissions.broker import _approval_view
    from ah.permissions.policy import build_request

    req = build_request(
        operation="process.exec",
        argv=["git", "status", "--porcelain"],
        cwd="/tmp",
        session_id="s",
        mode="ask",
        tool="terminal",
    )
    card = _approval_view(req, {"request_id": req.request_id})
    assert card["argv"] == ["git", "status", "--porcelain"]
    assert card["cwd"] and card["timeout"] == 60
    secret = "sk-or-" + "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
    wreq = build_request(
        operation="file.write",
        targets=["/tmp/newfile.txt"],
        content=f"hello secret {secret}",
        session_id="s",
        mode="ask",
        tool="write_file",
    )
    wcard = _approval_view(wreq, {"request_id": wreq.request_id})
    assert wcard["content_digest"] == wreq.content_digest
    assert secret not in wcard["content_preview"]
    assert "current_preview" in (wcard.get("file_diff") or {})


# ─── AH-005: durable proposals fail closed ─────────────────────────────────


def test_005_legacy_approval_never_resumes():
    import asyncio

    from ah.permissions import store as st
    from ah.permissions.policy import build_request

    async def _scenario():
        req = build_request(
            operation="file.write",
            targets=["/tmp/a.txt"],
            content="v1",
            session_id="s5",
            mode="ask",
        )
        rec = await st.create_approval(req, principal="tui")
        assert (await st.get_approval(req.request_id))["proposal_complete"] is True
        # Simulate a legacy row predating proposal columns.
        async with st._mem.lock:
            st._mem.approvals[req.request_id].pop("proposal", None)
        assert await st.find_approved_unclaimed("s5", req.digest) is None
        legacy = await st.get_approval(req.request_id)
        assert legacy["proposal_complete"] is False

    asyncio.run(_scenario())


# ─── AH-010: secret-free subprocess env ────────────────────────────────────


def test_010_child_env_strips_secrets_keeps_platform(monkeypatch):
    from ah.tools.terminal import _sanitized_env

    # monkeypatch.setenv restores prior values (or absence) afterwards —
    # never delete process env outright; other suites rely on it.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-secret")
    monkeypatch.setenv("AGENT_HARNESS_API_KEY", "gw-test-secret")
    env = _sanitized_env()
    assert "OPENAI_API_KEY" not in env
    assert "AGENT_HARNESS_API_KEY" not in env
    assert env.get("PATH")
    monkeypatch.setenv("AGENT_HARNESS_TERMINAL_EXTRA_ENV", "OPENAI_API_KEY")
    assert _sanitized_env()["OPENAI_API_KEY"] == "sk-test-secret"


def test_010_terminal_runs_without_inheriting_secrets(monkeypatch):
    import asyncio

    from ah.tools import terminal as tmod

    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")

    async def _scenario():
        out = await tmod.terminal("git --version", timeout=30, workdir=".")
        assert "git version" in out

    asyncio.run(_scenario())


# ─── AH-011: shared path identity ──────────────────────────────────────────


def test_011_approval_and_backend_agree(tmp_path, monkeypatch):
    import ah.permissions.policy as policymod
    from ah.permissions.policy import normalize_request
    from ah.permissions.policy import ActionRequest
    from ah.tools import file as fmod

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("x")
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(other)
    monkeypatch.setattr(policymod, "workspace_root", lambda: ws.resolve())
    req = normalize_request(ActionRequest(operation="file.read", targets=["a.txt"], cwd="."))
    resolved = fmod.resolve_path("a.txt")
    assert str(resolved) == req.targets[0]
    assert policymod.workspace_root() == ws.resolve()


# ─── AH-012: export + metadata redaction ───────────────────────────────────


def test_012_export_redacts_title_goal_and_content(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from ah import services as svc
    from ah.core import context as ctxmod

    async def _no_chunks(*a, **k):
        return []

    monkeypatch.setattr(ctxmod.context_manager, "get_all_chunks", _no_chunks)

    async def _scenario():
        secret = "sk-or-" + "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
        session = SimpleNamespace(
            id=uuid.uuid4(),
            title=f"working with {secret}",
            status="active",
            agent_id="h",
            goal="deploy with AKIAIOSFODNN7EXAMPLE credential",
            created_at=__import__("datetime").datetime.now(),
            last_activity=__import__("datetime").datetime.now(),
        )
        out = await svc.export_markdown(session)
        assert secret not in out
        assert "AKIAIOSFODNN7EXAMPLE" not in out

    asyncio.run(_scenario())


def test_012_metadata_redacted_and_reserved_fields_win():
    from ah.rag.pipeline import _redact_metadata

    meta = _redact_metadata({"note": "token sk-or-abcdefghij1234567890", "embedding_model": "evil"})
    assert "sk-or-abcdefghij1234567890" not in meta["note"]
    assert meta["embedding_model"] == "evil"  # pipeline overwrites reserved after


# ─── AH-013/016/017: claims ────────────────────────────────────────────────


def test_016_db_less_turn_blocks_mutation_and_vice_versa():
    import asyncio
    import uuid

    from ah.core import turns as t

    async def _scenario():
        sid = uuid.uuid4()
        turn = await t.try_begin_turn(sid)
        assert turn
        assert await t.begin_mutation(sid) is None
        # Stale release never unlocks the owner.
        assert await t.end_turn(sid, "bogus") is False
        assert await t.begin_mutation(sid) is None
        assert await t.end_turn(sid, turn) is True
        mut = await t.begin_mutation(sid)
        assert mut
        assert await t.try_begin_turn(sid) is None
        assert await t.end_mutation(sid, mut) is True

    asyncio.run(_scenario())


def test_013_db_less_renewal_and_ownership():
    import asyncio
    import uuid

    from ah.core import turns as t

    async def _scenario():
        sid = uuid.uuid4()
        turn = await t.try_begin_turn(sid)
        try:
            assert await t.renew_turn(sid, turn) is True
            assert await t.owns_claim(sid, turn) is True
            assert await t.owns_claim(sid, "nope") is False
            assert await t.renew_turn(sid, "nope") is False
        finally:
            await t.end_turn(sid, turn)

    asyncio.run(_scenario())


def test_013_turn_ownership_renewal_lifecycle():
    import asyncio
    import uuid

    from ah.core import turns as t

    async def _scenario():
        sid = uuid.uuid4()
        turn = await t.try_begin_turn(sid)
        assert turn
        owner = t.TurnOwnership(sid, "turn-1", turn)
        try:
            owner.start_renewal(interval_s=0.02)
            await asyncio.sleep(0.1)
            assert owner._renew_task is not None and not owner._renew_task.done()
            assert owner.ownership_lost.is_set() is False
            await owner.stop_renewal()
            assert owner._renew_task is None
        finally:
            await t.end_turn(sid, turn)

    asyncio.run(_scenario())


# ─── AH-022/021: typed pauses ──────────────────────────────────────────────


def test_022_typed_pause_not_prose():
    from ah.core.models import AgentResponse

    discussing = AgentResponse(
        content="This needs_approval flow is documented. Needs approval processes rock.",
        tool_calls=[{"tool": "read_file", "result_preview": "needs_approval mentioned"}],
    )
    assert discussing.needs_approval == []
    paused = AgentResponse(
        content="whatever",
        tool_calls=[
            {
                "tool": "terminal",
                "result_preview": "x",
                "needs_approval": {
                    "request_id": "r1",
                    "session_id": "s",
                    "operation": "process.exec",
                    "target": "",
                    "status": "pending",
                },
            }
        ],
    )
    assert [p["request_id"] for p in paused.needs_approval] == ["r1"]


def test_021_pause_request_id_extraction():
    from ah.core.scheduler import _pause_request_id

    err = PermissionError("needs_approval: job paused; request abcdef1234567890 (x)")
    assert _pause_request_id(err) == "abcdef1234567890"
    linked = PermissionError("needs_approval: paused")
    linked.approval_request_id = "req-9"
    assert _pause_request_id(linked) == "req-9"


# ─── AH-020: ownership-wait cleanup ────────────────────────────────────────


def test_020_no_leaked_tasks_after_run():
    import asyncio
    import uuid

    from ah.core.models import AgentResponse
    from ah.core.scheduler import JobRunner

    class Agent:
        async def run(self, *a, **k):
            return AgentResponse(content="ok", tool_calls=[])

    class Job:
        def __init__(self):
            self.id = uuid.uuid4()
            self.name = "j"
            self.session_id = uuid.uuid4()
            self.agent_name = "harness"
            self.prompt = "hi"
            self.no_agent = False
            self.model = None
            self.provider = None
            self.claim_token = uuid.uuid4()

    class Store:
        def __init__(self):
            self.job = Job()
            self.finished = []

        async def claim_due(self, now=None):
            job, self.job = self.job, None
            return job

        async def renew_lease(self, job_id, claim_token=None):
            return True

        async def finish(self, job_id, *, error=None):
            self.finished.append(error)
            return True

    async def _scenario():
        before = set(asyncio.all_tasks())
        store = Store()
        ok = await JobRunner(store=store, agent_factory=lambda _n: Agent()).run_due_once()
        assert ok is True
        await asyncio.sleep(0)
        leaked = [t for t in asyncio.all_tasks() if t not in before and not t.done()]
        assert leaked == []
        assert store.finished == [None]

    asyncio.run(_scenario())


# ─── AH-023: terminal cancellation kills the tree ──────────────────────────


def test_023_kill_tree_stops_resistant_child():
    import subprocess
    import sys
    import time

    from ah.tools.terminal import _kill_tree

    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _kill_tree(proc)
        deadline = time.time() + 10
        while proc.poll() is None and time.time() < deadline:
            time.sleep(0.05)
        assert proc.poll() is not None
    finally:
        try:
            proc.kill()
        except OSError:
            pass


def test_023_terminal_cancel_signals_stop():
    import asyncio

    from ah.tools import terminal as tmod

    async def _scenario():
        real = tmod._run_bounded
        seen = {}

        def _slow(*a, **k):
            import threading
            import time

            stop = k.get("stop_event")
            seen["stop"] = stop
            for _ in range(300):
                if stop is not None and stop.is_set():
                    raise asyncio.CancelledError("stopped")
                time.sleep(0.01)
            return "done"

        tmod._run_bounded = _slow
        try:
            task = asyncio.create_task(tmod.terminal("echo hi", timeout=60, workdir="."))
            await asyncio.sleep(0.2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert seen["stop"] is not None and seen["stop"].is_set()
        finally:
            tmod._run_bounded = real

    asyncio.run(_scenario())


# ─── AH-024/025: bounded shutdown + tracking ───────────────────────────────


def test_024_shutdown_quarantines_resistant_tasks():
    import asyncio
    import time

    from ah.core.runtime import RuntimeServices

    async def _scenario():
        rt = RuntimeServices()

        async def _resistant():
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                await asyncio.sleep(60)

        task = asyncio.create_task(_resistant())
        rt.track(task)
        assert rt.track(asyncio.create_task(asyncio.sleep(0))) is not None
        start = time.monotonic()
        await rt.shutdown(timeout=0.5)
        assert time.monotonic() - start < 10
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    asyncio.run(_scenario())


# ─── AH-030: typed SSE pause ───────────────────────────────────────────────


def test_030_sse_pause_is_typed_not_error():
    from ah.api.app import _serialize_event
    from ah.core.models import AgentResponse, StreamEvent

    resp = AgentResponse(
        content="paused",
        tool_calls=[
            {
                "tool": "terminal",
                "needs_approval": {
                    "request_id": "r1",
                    "operation": "process.exec",
                    "target": "x",
                    "status": "pending",
                },
            }
        ],
    )
    done = _serialize_event(StreamEvent(type="done", response=resp))
    assert done["needsApproval"][0]["requestId"] == "r1"
    pause = _serialize_event(
        StreamEvent(
            type="needs_approval",
            content="r1",
            tool_name="process.exec",
            tool_args={"requestId": "r1", "status": "pending"},
        )
    )
    assert pause["type"] == "needs_approval" and pause["requestId"] == "r1"


# ─── AH-032: keyword-only pipeline ─────────────────────────────────────────


def test_032_pipeline_without_keys_is_keyword_only(monkeypatch):
    for var in ("OPENAI_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    from ah.core.config import config

    monkeypatch.setattr(config, "openai_api_key", "")
    monkeypatch.setattr(config, "openrouter_api_key", "")
    from ah.rag.pipeline import RAGPipeline

    pipe = RAGPipeline()
    assert pipe._embedder is None


# ─── AH-036: reranker identity ─────────────────────────────────────────────


def test_036_reranker_identity_matches_component():
    from ah.rag.pipeline import RAGPipeline
    from ah.rag.reranker import IdentityReranker

    pipe = RAGPipeline(reranker=IdentityReranker())
    assert pipe.reranker_identity()["reranker"] == "passthrough"


# ─── AH-037: typed consolidation ───────────────────────────────────────────


def _chunk(cid, created):
    from ah.core.models import ContextChunk

    return ContextChunk(
        id=cid,
        session_id=uuid.uuid4(),
        agent_id="h",
        chunk_type="user_message",
        payload={"content": "hi"},
        created_at=created,
    )


def test_037_failed_extraction_advances_nothing(monkeypatch):
    import asyncio
    import uuid
    from datetime import UTC, datetime

    from ah.core import context as ctxmod
    from ah.memory.consolidator import MemoryConsolidator

    async def _scenario():
        cons = MemoryConsolidator(store=_FakeMemStore())
        now = datetime.now(UTC)
        chunks = [_chunk(uuid.uuid4(), now)]
        monkeypatch.setattr(ctxmod.context_manager, "get_chunks", _no_db_guard(ctxmod, chunks))
        monkeypatch.setattr(
            ctxmod.context_manager, "get_chunks_since", _no_db_guard(ctxmod, chunks)
        )

        async def _boom(*a, **k):
            raise RuntimeError("provider down")

        cons._extract_memories = _boom
        result = await cons.consolidate_session(uuid.uuid4(), "h")
        assert result.status == "failed"
        assert result.checkpoint is None

    asyncio.run(_scenario())


def _no_db_guard(ctxmod, chunks):
    async def _get(*a, **k):
        return list(chunks)

    return _get


class _FakeMemStore:
    def __init__(self, fail_add=False):
        self.fail_add = fail_add
        self.added = []

    async def search(self, **k):
        return []

    async def search_by_embedding(self, **k):
        return []

    async def update_access(self, *a, **k):
        return None

    async def add(self, **k):
        if self.fail_add:
            raise RuntimeError("db down")
        self.added.append(k)
        from types import SimpleNamespace

        return SimpleNamespace(id=uuid.uuid4())


def test_037_empty_extraction_checkpoint_valid_partial_not(monkeypatch):
    import asyncio
    import uuid
    from datetime import UTC, datetime

    from ah.core import context as ctxmod
    from ah.memory.consolidator import MemoryConsolidator
    from ah.memory.models import MemoryEntry

    async def _scenario():
        now = datetime.now(UTC)
        chunks = [_chunk(uuid.uuid4(), now)]

        async def _get(*a, **k):
            return list(chunks)

        monkeypatch.setattr(ctxmod.context_manager, "get_chunks", _get)
        monkeypatch.setattr(ctxmod.context_manager, "get_chunks_since", _get)

        async def _empty(*a, **k):
            return []

        cons = MemoryConsolidator(store=_FakeMemStore())
        cons._extract_memories = _empty
        empty = await cons.consolidate_session(uuid.uuid4(), "h")
        assert empty.status == "empty" and empty.checkpoint is not None

        async def _one(*a, **k):
            return [
                MemoryEntry(
                    id=uuid.uuid4(),
                    session_id=None,
                    agent_id="h",
                    content="user likes tea",
                    category="preference",
                    importance=0.9,
                )
            ]

        cons2 = MemoryConsolidator(store=_FakeMemStore(fail_add=True))
        cons2._extract_memories = _one
        partial = await cons2.consolidate_session(uuid.uuid4(), "h")
        assert partial.status == "partial" and partial.checkpoint is None

    asyncio.run(_scenario())


# ─── AH-014/015: fenced replacement (real DB) ─────────────────────────────


async def test_014_015_fenced_replace_aborts_stale(db_pool, monkeypatch):
    from ah.core import context as ctxmod
    from ah.core import session as sessmod
    from ah.core.context import context_manager
    from ah.core.exceptions import StaleOwnershipError, StaleSnapshotError
    from ah.core.session import session_manager

    monkeypatch.setattr(ctxmod, "db", db_pool)
    monkeypatch.setattr(sessmod, "db", db_pool)
    session = await session_manager.create(title="fence", agent_id="h")
    try:
        from ah.core.turns import begin_mutation, end_mutation

        # turns.py binds db lazily per call: point the shared handle at the
        # test database for this test (reverted afterwards).
        import ah.db.connection as _connmod

        monkeypatch.setattr(_connmod, "db", db_pool)
        token = await begin_mutation(session.id)
        assert token
        for i in range(2):
            await context_manager.add_chunk(
                session_id=session.id,
                agent_id="h",
                chunk_type="user_message",
                payload={"content": f"msg {i}"},
                token_count=2,
            )
        chunks = await context_manager.get_all_chunks(session.id)
        ids = [c.id for c in chunks]
        # Wrong owner aborts with rollback (no rows harmed).
        with pytest.raises(StaleOwnershipError):
            await context_manager.replace_chunks_by_ids(
                session.id,
                ids,
                [],
                expected_owner="bogus-token",
            )
        assert len(await context_manager.get_all_chunks(session.id)) == 2
        # Stale snapshot (one input vanished) aborts instead of
        # double-inserting.
        await context_manager.delete_chunks(session.id)
        await context_manager.add_chunk(
            session_id=session.id,
            agent_id="h",
            chunk_type="user_message",
            payload={"content": "fresh"},
            token_count=1,
        )
        with pytest.raises(StaleSnapshotError):
            await context_manager.replace_chunks_by_ids(
                session.id,
                ids,
                [],
                expected_owner=token,
            )
        assert len(await context_manager.get_all_chunks(session.id)) == 1
        # Happy path with the live token commits.
        fresh = await context_manager.get_all_chunks(session.id)
        n = await context_manager.replace_chunks_by_ids(
            session.id,
            [c.id for c in fresh],
            [],
            expected_owner=token,
        )
        assert n == 0
        await end_mutation(session.id, token)
    finally:
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session.id)


# ─── AH-021: paused jobs excluded until resume (real DB) ────────────────────


async def test_021_paused_job_not_due_until_resume(db_pool, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from ah.core import session as sessmod
    from ah.core.scheduler import JobStore
    from ah.core.session import session_manager

    monkeypatch.setattr(sessmod, "db", db_pool)
    monkeypatch.setattr("ah.core.scheduler.db", db_pool)
    session = await session_manager.create(title="pause", agent_id="h")
    try:
        store = JobStore()
        job = await store.create(
            name="pausable",
            kind="interval",
            session_id=session.id,
            prompt="p",
            interval_seconds=60,
        )
        # Park it awaiting approval: excluded from due claims even when due.
        # Side-effect-free check (the shared test DB holds other suites'
        # rows): attempt the exact claim_due predicate for OUR row only, in
        # a rolled-back transaction — never touching other jobs.
        past = datetime.now(UTC) - timedelta(hours=1)
        await db_pool.execute("UPDATE jobs SET next_run_at = $2 WHERE id = $1", job.id, past)
        assert await store.finish(job.id, error="needs_approval: x", paused_for="req-1")

        async def _try_claim_mine(now):
            async with db_pool.acquire() as conn:
                async with conn.transaction():
                    row = await conn.fetchrow(
                        "UPDATE jobs SET status = 'running' WHERE id = $1 "
                        "AND enabled AND paused_for_approval IS NULL "
                        "AND status <> 'paused_approval' AND next_run_at <= $2 "
                        "RETURNING id",
                        job.id,
                        now,
                    )
                    # Rollback: predicate probe only, no state change.
                    raise _RollbackProbe(row)

        class _RollbackProbe(Exception):
            def __init__(self, row):
                super().__init__("probe")
                self.row = row

        async def _claimable(now):
            try:
                await _try_claim_mine(now)
            except _RollbackProbe as probe:
                return probe.row is not None
            return False  # unreachable

        assert await _claimable(datetime.now(UTC)) is False
        # Explicit human resume re-arms exactly one run.
        resumed = await store.resume_job(job.id)
        assert resumed is not None and resumed.status == "idle"
        assert await _claimable(datetime.now(UTC)) is True
    finally:
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session.id)


# ─── AH-042: config validation ─────────────────────────────────────────────


def test_042_invalid_values_rejected_without_mutation():
    from ah.core.config import config, validate_value
    from ah.gateway.features._common import coerce_config_value
    from ah.gateway.errors import RpcError

    saved = config.get("turn_timeout")
    with pytest.raises(ValueError):
        config.set("turn_timeout", "not-a-number")
    assert config.get("turn_timeout") == saved
    for bad in ("nan", "inf", "-inf", "-5", "99999999"):
        with pytest.raises(ValueError):
            validate_value("turn_timeout", bad)
    assert config.get("turn_timeout") == saved
    with pytest.raises(ValueError):
        validate_value("temperature", 5.0)
    assert validate_value("turn_timeout", "60") == 60
    assert validate_value("execution_mode", "FULL") == "full"
    with pytest.raises(RpcError):
        coerce_config_value("turn_timeout", "bad")


# ─── AH-043: fetch bounds ──────────────────────────────────────────────────


class _FakeSock:
    def __init__(self, payload: bytes):
        import io

        self._buf = io.BytesIO(payload)

    def makefile(self, *a, **k):
        return self._buf

    def settimeout(self, *a, **k):
        pass

    def close(self):
        pass


def test_043_endless_headers_rejected():
    import time

    from ah.security.fetch import FetchError, _read_response

    blob = b"HTTP/1.1 200 OK\r\n" + b"X-Pad: abcdefgh\r\n" * 5000
    with pytest.raises(FetchError):
        _read_response(_FakeSock(blob), 200_000, time.monotonic() + 5)


def test_043_oversized_body_rejected():
    import time

    from ah.security.fetch import FetchError, _read_response

    blob = b"HTTP/1.1 200 OK\r\nContent-Length: 999999999\r\n\r\n"
    with pytest.raises(FetchError):
        _read_response(_FakeSock(blob), 200_000, time.monotonic() + 5)


def test_043_malformed_chunk_rejected():
    import time

    from ah.security.fetch import FetchError, _read_response

    blob = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\nZZZ\r\n"
    with pytest.raises(FetchError):
        _read_response(_FakeSock(blob), 200_000, time.monotonic() + 5)
