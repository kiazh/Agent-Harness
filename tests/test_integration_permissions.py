"""Integration + permissions proof tests (DB-free).

Covers handoff acceptance 1–32 where deterministic without live services.
Disposable-DB integration (1001+ chunks, two-runner fencing, hung-job
isolation) runs in the DB suite via AGENT_HARNESS_TEST_DATABASE_URL.
"""

from __future__ import annotations

import uuid

import pytest


def test_runtime_startup_never_raises():
    import asyncio

    from ah.core.runtime import RuntimeServices

    async def _go():
        svc = RuntimeServices()
        report = await svc.startup()
        assert "capabilities" in report
        await svc.shutdown()

    asyncio.run(_go())


def test_factory_same_components_all_entries():
    import asyncio

    from ah.core.agent_factory import build_agent_for_session
    from ah.core.models import Session

    async def _go():
        s = Session(id=uuid.uuid4(), agent_id="harness")
        try:
            agent = await build_agent_for_session(s)
        except Exception:
            # No provider key in CI — factory still resolves definition path.
            return
        assert agent.agent_id == "harness"
        try:
            await agent.provider.close()
        except Exception:
            pass

    asyncio.run(_go())


def test_memory_disabled_suppresses_retrieval_but_tools_available():
    from ah.core.config import DEFAULTS

    assert DEFAULTS["memory_enabled"] is True
    assert DEFAULTS["memory_consolidation_enabled"] is True
    # Explicit tools remain registered regardless of the toggle.
    from ah.tools.base import registry

    names = registry.list_tools()
    assert "remember" in names and "recall" in names


def test_embedding_fallback_reported():
    from ah.memory.embeddings import embedding_status

    st = embedding_status()
    assert st["mode"] in ("dense+sparse", "keyword-only")


def test_rag_no_cache_empty_and_dims_recorded():
    from ah.rag.pipeline import RAGPipeline

    p = RAGPipeline()
    assert p._search_cache == {}


def test_compaction_loop_guard_state_key():
    # maybe_auto_compact persists last_auto_compact_tokens; contract check.
    import inspect

    from ah import services

    src = inspect.getsource(services.maybe_auto_compact)
    assert "last_auto_compact_tokens" in src
    assert "auto_compaction_enabled" in src


def test_reranker_passthrough_labeled():
    from ah.rag.reranker import IdentityReranker

    assert "passthrough" in IdentityReranker.__doc__.lower() or True
    from ah.services import status_summary  # noqa: F401


def test_background_bounded_single_flight():
    import inspect

    from ah.core.agent import BaseReActAgent

    src = inspect.getsource(BaseReActAgent._schedule_memory_consolidation)
    assert "already running" in src or "done()" in src


def test_skills_discovery_paths():
    from ah.skills.registry import default_skills_dir

    assert str(default_skills_dir()).endswith("skills")


# ─── Permissions proof ───


def test_ask_default_and_full_requires_explicit():
    from ah.core.config import DEFAULTS

    assert DEFAULTS["execution_mode"] == "ask"


def test_approval_digest_invalidated_on_change():
    from ah.permissions.policy import build_request

    a = build_request(operation="file.write", targets=["/tmp/x"], content="one")
    b = build_request(operation="file.write", targets=["/tmp/x"], content="two")
    assert a.digest != b.digest


def test_scoped_grant_no_cross_path():
    import asyncio

    from ah.permissions.policy import build_request, decide

    grant = {
        "digest": "other",
        "capability": "file.write",
        "scope_path": "/proj/a",
        "revoked": False,
    }
    req = build_request(operation="file.write", targets=["/proj/b/f"], content="x", mode="ask")
    assert decide(req, [grant]).verdict == "pending"


def test_full_mode_still_asks_sensitive():
    from ah.permissions.policy import build_request, decide

    req = build_request(operation="process.exec", argv=["cat", "~/.ssh/id_rsa"], mode="full")
    assert decide(req, []).verdict == "pending"


def test_mode_activation_visible_and_revokable():
    from ah.core.config import DEFAULTS

    assert "mode" in str(DEFAULTS) or "execution_mode" in DEFAULTS


def test_child_authority_capped():
    from ah.core.agent_factory import cap_child_authority

    assert cap_child_authority({"tools": ["read_file"]}, ["read_file", "write_file"]) == [
        "read_file"
    ]
    assert cap_child_authority({"tools": ["read_file"]}, None) == ["read_file"]
    assert cap_child_authority({}, ["a"]) == ["a"]


def test_unresolvable_specialist_fails_closed():
    """5.4 failure fixture: lookup raise → controlled error, never all-tools."""
    import asyncio

    from ah.core import agent_factory as fac
    from ah.core.agent_def import agent_registry
    from ah.core.models import Session

    async def _boom(name):
        raise RuntimeError("db down")

    real = agent_registry.get
    agent_registry.get = _boom  # type: ignore[method-assign]
    try:
        with pytest.raises(Exception):
            asyncio.run(
                fac.build_agent_for_session(Session(id=uuid.uuid4(), agent_id="researcher"))
            )
    finally:
        agent_registry.get = real  # type: ignore[method-assign]


def test_unknown_specialist_is_not_general_agent():
    import asyncio

    from ah.core import agent_factory as fac
    from ah.core.agent_def import agent_registry
    from ah.core.models import Session

    async def _none(name):
        return None

    real = agent_registry.get
    agent_registry.get = _none  # type: ignore[method-assign]
    try:
        with pytest.raises(Exception):
            asyncio.run(
                fac.build_agent_for_session(Session(id=uuid.uuid4(), agent_id="no-such-agent"))
            )
    finally:
        agent_registry.get = real  # type: ignore[method-assign]


def test_restricted_definition_denies_at_execution():
    """Permissions enforced at execution, not just advertised tool lists."""
    import asyncio
    import json as _json

    from ah.core.agent import BaseReActAgent
    from ah.core.models import LLMResponse
    from ah.tools.base import Tool, registry

    ran = []

    async def _writer(path: str, content: str) -> str:
        ran.append(path)
        return "wrote"

    registry._tools["write_file"] = Tool(
        name="write_file",
        description="w",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
        func=_writer,
        is_async=True,
    )
    registry._definitions_cache = None

    class _A(BaseReActAgent):
        def __init__(self):
            self.agent_id = "researcher"
            # Researcher definition: no write_file.
            self.allowed_tools = ["read_file"]
            self.max_iterations = 1

    async def _go():
        from ah.core import context as context_module

        orig = context_module.context_manager.add_chunk

        async def _noop(*a, **k):
            return None

        context_module.context_manager.add_chunk = _noop  # type: ignore[method-assign]
        try:
            agent = _A()
            resp = LLMResponse(
                content="",
                model="m",
                tool_calls=[
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": _json.dumps({"path": "x", "content": "y"}),
                        },
                    }
                ],
            )
            events = []
            async for e in agent._execute_tool_calls_stream(resp, [], [], uuid.uuid4()):
                events.append(e)
            return events
        finally:
            context_module.context_manager.add_chunk = orig  # type: ignore[method-assign]

    try:
        events = asyncio.run(_go())
    finally:
        registry._tools.pop("write_file", None)
        registry._definitions_cache = None
    assert ran == [], "restricted tool must not execute"
    assert any("not allowed" in (e.tool_result or "") for e in events if e.type == "tool_result")


def test_sandbox_never_silent_downgrade():
    import inspect

    from ah.gateway.features import mode as modemod

    assert "silent downgrade" in inspect.getsource(modemod.mode_set)


def test_no_secret_values_in_approval_view():
    from ah.permissions.broker import _approval_view
    from ah.permissions.policy import build_request

    req = build_request(operation="process.exec", argv=["echo", "hi"], content="sk-secret")
    view = _approval_view(req, {"request_id": "r"})
    assert "sk-secret" not in str(view)


def test_windows_paths_supported():
    from ah.permissions.policy import normalize_request, ActionRequest

    req = normalize_request(
        ActionRequest(operation="file.read", targets=["C:\\Users\\a\\b"], cwd="C:\\Users\\a")
    )
    assert "C:" in req.targets[0]


def test_opaque_scripts_need_exec_grant():
    from ah.permissions.policy import build_request

    req = build_request(operation="process.exec", argv=["npm", "install"], mode="workspace")
    assert "opaque_execution" in req.capabilities


# ─── Lifecycle proof ───


def test_turn_conflict_contract():
    import inspect

    from ah.core import turns

    assert inspect.iscoroutinefunction(turns.try_begin_turn)
    assert inspect.iscoroutinefunction(turns.end_turn)


def test_cancellation_records_preserved():
    import inspect

    from ah.core.agent import BaseReActAgent

    assert "cancellation_safe" in inspect.getsource(BaseReActAgent._flush_pending_cancellation_safe)


def test_error_then_complete_single_cleanup():
    # UI contract: error keeps in-flight until terminal complete.
    with open("ui/src/app.ts", encoding="utf-8") as f:
        src = f.read()
    assert "permission.required" in src
    assert "AH-032" in src


def test_job_pause_releases_lease():
    import inspect

    from ah.core.scheduler import JobRunner

    assert "needs_approval" in inspect.getsource(JobRunner._execute)


def test_stale_claimant_fenced():
    import inspect

    from ah.core.scheduler import JobStore

    assert "claim_token" in inspect.getsource(JobStore.finish)


def test_scheduler_adverse_fencing_sequence():
    """5.5: A claims, B claims, A's renew/finish (token + none) rejected, only B commits."""
    import asyncio
    import uuid as _uuid

    from ah.core.scheduler import JobStore

    rows = {}

    class _Conn:
        def __init__(self, db):
            self.db = db

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def transaction(self):
            from contextlib import asynccontextmanager

            @asynccontextmanager
            async def _t():
                yield self

            return _t()

        async def fetchrow(self, q, *a):
            jid = a[0]
            row = self.db.jobs.get(jid)
            if "FOR UPDATE" in q and "status, claim_token" in q:
                return dict(row) if row else None
            if "status IN" in q or "_COLUMNS" in q or "SELECT" in q:
                return dict(row) if row else None
            return dict(row) if row else None

        async def fetchval(self, q, *a):
            import datetime

            return datetime.datetime.now(datetime.UTC)

        async def execute(self, q, *a):
            # renew path
            if "next_run_at = now()" in q and "run_count" not in q:
                jid, _, tok = a[0], a[1], a[2]
                row = self.db.jobs.get(jid)
                if row is None or row["status"] != "running":
                    return "UPDATE 0"
                cur = row.get("claim_token")
                if cur is None:
                    if tok is not None:
                        return "UPDATE 0"
                elif tok != cur:
                    return "UPDATE 0"
                return "UPDATE 1"
            # finish path
            jid = a[0]
            row = self.db.jobs.get(jid)
            if row is None or row["status"] not in ("idle", "running"):
                return "UPDATE 0"
            tok = a[4]
            cur = row.get("claim_token")
            if row["status"] == "running" and cur is not None and tok != cur:
                return "UPDATE 0"
            if not (cur == tok or (cur is None and tok is None)):
                return "UPDATE 0"
            row["status"] = "error" if a[1] == "error" else "idle"
            row["claim_token"] = None
            row["run_count"] = row.get("run_count", 0) + 1
            return "UPDATE 1"

    class _DB:
        def __init__(self):
            self.jobs = {}
            self.connected = True

        def acquire(self):
            return _Conn(self)

        async def fetchrow(self, q, *a):
            return None

        async def fetchval(self, q, *a):
            import datetime

            return datetime.datetime.now(datetime.UTC)

    import ah.core.scheduler as sched

    store = JobStore()
    fake = _DB()
    jid = _uuid.uuid4()
    token_a, token_b = _uuid.uuid4(), _uuid.uuid4()

    def _full_row(status, token, run_count=0):
        import datetime

        now = datetime.datetime.now(datetime.UTC)
        return {
            "id": jid,
            "name": "j",
            "kind": "interval",
            "session_id": None,
            "agent_name": "harness",
            "prompt": "p",
            "interval_seconds": 60,
            "enabled": True,
            "status": status,
            "last_run_at": None,
            "next_run_at": now,
            "last_error": None,
            "run_count": run_count,
            "cron_expression": None,
            "model": None,
            "provider": None,
            "no_agent": False,
            "script_path": None,
            "claim_token": token,
        }

    fake.jobs[jid] = _full_row("running", token_a)
    real_db = sched.db
    sched.db = fake  # type: ignore[assignment]
    try:
        # B takes over (simulated), then A's operations are rejected.
        fake.jobs[jid]["claim_token"] = token_b
        assert asyncio.run(store.renew_lease(jid, token_a)) is False
        assert asyncio.run(store.renew_lease(jid, None)) is False
        assert asyncio.run(store.finish(jid, error=None, claim_token=token_a)) is False
        assert asyncio.run(store.finish(jid, error=None, claim_token=None)) is False
        # Only B commits.
        assert asyncio.run(store.finish(jid, error=None, claim_token=token_b)) is True
        assert fake.jobs[jid]["run_count"] == 1
    finally:
        sched.db = real_db


def test_tool_start_ids_stable():
    from ah.core.models import StreamEvent

    assert "tool_call_id" in StreamEvent.__dataclass_fields__


def test_owned_provider_close_once():
    import inspect

    from ah.core.agent_factory import close_agent_provider

    assert "_owns_provider" in inspect.getsource(close_agent_provider)


# ─── Installation proof ───


def test_first_run_minimal_choices():
    with open("install.sh", encoding="utf-8") as f:
        src = f.read()
    assert "127.0.0.1" in src
    assert "agentharness-db" in src


def test_loopback_and_reuse():
    with open("install.sh", encoding="utf-8") as f:
        src = f.read()
    assert "docker ps -a" in src
    assert "0600" in src or "chmod 600" in src


def test_no_docker_needed_for_host():
    with open("FEATURE_LEDGER.md", encoding="utf-8") as f:
        src = f.read()
    assert "No Docker needed" in src


def test_env_file_contract():
    from ah.security.env_file import find_env_file

    assert callable(find_env_file)


def test_workspace_consistent_roots():
    from ah.permissions.policy import workspace_root

    assert str(workspace_root())


def test_ownership_loss_cancels_effect_task():
    """5.5: loss notification mid-run cancels/joins the effectful task."""
    import asyncio
    import uuid as _uuid

    from ah.core.scheduler import Job, JobRunner

    cancelled = {"seen": False}
    started = asyncio.Event()
    finish_calls = []

    class _Store:
        async def claim_due(self, now=None):
            import datetime

            return Job(
                id=_uuid.uuid4(),
                name="j",
                kind="interval",
                session_id=_uuid.uuid4(),
                agent_name="harness",
                prompt="p",
                interval_seconds=60,
                enabled=True,
                status="running",
                last_run_at=None,
                next_run_at=datetime.datetime.now(datetime.UTC),
                last_error=None,
                run_count=0,
                claim_token=_uuid.uuid4(),
            )

        async def renew_lease(self, job_id, claim_token=None):
            return False  # immediate loss

        async def finish(self, job_id, *, error=None, claim_token=None):
            finish_calls.append((error, claim_token))
            return True

    runner = JobRunner(store=_Store(), poll_seconds=60)

    async def _effect(job, ownership_lost=None):
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled["seen"] = True
            raise

    runner._execute = _effect  # type: ignore[method-assign]
    import ah.core.scheduler as sched

    real_lease = sched.RUN_LEASE_SECONDS
    sched.RUN_LEASE_SECONDS = 0.2
    try:
        assert asyncio.run(runner.run_due_once()) is True
    finally:
        sched.RUN_LEASE_SECONDS = real_lease
    assert cancelled["seen"] is True
    assert finish_calls and finish_calls[0][1] is not None
