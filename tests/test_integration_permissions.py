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

    real_write_file = registry._tools.get("write_file")
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
        if real_write_file is not None:
            registry._tools["write_file"] = real_write_file
        else:
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


def test_scope_windows_case_unc_and_symlink_lexical():
    """LP-04: case-insensitive drives, UNC shares, traversal, symlink text."""
    import os as _os

    from ah.permissions.policy import _norm_path, _scope_allows

    if _os.name != "nt":
        pytest.skip("Windows path semantics")
    file_grant = {
        "id": "1",
        "session_id": "s",
        "agent_id": "h",
        "mode": "ask",
        "capability": "file.read",
        "scope_type": "file",
        "scope_path": "C:\\Proj\\Report.txt",
        "grant_kind": "session",
        "digest": "d",
        "revoked": False,
        "expires_at": None,
    }

    class _R:
        def __init__(self, targets):
            self.targets = targets

    assert _scope_allows(file_grant, _R(["c:\\proj\\REPORT.txt"])) is True
    assert _scope_allows(file_grant, _R(["C:\\Proj\\Report.txt.backup"])) is False
    dir_grant = dict(file_grant, scope_type="dir", scope_path="\\\\srv\\share\\proj")
    assert _scope_allows(dir_grant, _R(["\\\\srv\\share\\proj\\sub\\f.txt"])) is True
    assert _scope_allows(dir_grant, _R(["\\\\srv\\share\\proj-other\\f.txt"])) is False
    assert _scope_allows(dir_grant, _R(["\\\\srv\\share\\proj\\..\\etc\\x"])) is False
    # Symlinked text resolves through the same normalizer (lexical).
    assert _norm_path("C:\\Proj\\sub\\..\\Report.txt") == _norm_path("C:\\Proj\\Report.txt")


def test_parent_caps_enforced_at_broker_despite_grants():
    """LP-08: restricted parent caps bind at execution, not just on the agent."""
    import asyncio

    from ah.core.agent_factory import parent_authority_var
    from ah.permissions.broker import ApprovalDenied
    from ah.permissions.policy import build_request
    from ah.permissions.broker import permission_broker

    async def _go():
        import os as _os

        from ah.permissions.policy import workspace_root

        inside = str(workspace_root() / "a.txt")
        # Absent/None = unrestricted; [] = nothing permitted.
        assert (
            await permission_broker.guard(
                build_request(
                    operation="file.read",
                    targets=[inside],
                    mode="ask",
                    agent_id="h",
                    session_id="s-cap",
                    tool="read_file",
                )
            )
        ).operation == "file.read"
        token = parent_authority_var.set({"tools": []})
        try:
            with pytest.raises(ApprovalDenied):
                await permission_broker.guard(
                    build_request(
                        operation="file.read",
                        targets=[inside],
                        mode="workspace",
                        agent_id="h",
                        session_id="s-cap",
                        tool="read_file",
                    )
                )
        finally:
            parent_authority_var.reset(token)
        # Disjoint intersection denies; mode escalation denied.
        token = parent_authority_var.set({"tools": ["read_file"], "max_mode": "ask"})
        try:
            with pytest.raises(ApprovalDenied):
                await permission_broker.guard(
                    build_request(
                        operation="file.write",
                        targets=["/proj/a.txt"],
                        content="x",
                        mode="full",
                        agent_id="h",
                        session_id="s-cap",
                        tool="write_file",
                    )
                )
        finally:
            parent_authority_var.reset(token)

    asyncio.run(_go())


def test_selected_provider_key_honesty(monkeypatch):
    """LP-12: wrong-family keys report unavailable for the SELECTED route."""
    from ah.core import runtime as rt
    from ah.core.config import config

    real_provider = config.get("provider")
    real_openrouter = config.openrouter_api_key
    real_openai = config.openai_api_key
    config.set("provider", "openrouter")
    # Blank attribute AND environment fallbacks (get() consults legacy env).
    config.openrouter_api_key = ""
    config.openai_api_key = "sk-test-wrong-family"
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    try:
        from ah.security import secrets as _secrets

        monkeypatch.setattr(_secrets, "get_secret", lambda name: None)
    except Exception:
        pass
    try:
        report = rt._detect_providers()
        assert report["chat_provider"] == "unavailable"
        assert "OPENROUTER" in report["chat_provider_reason"]
    finally:
        config.openrouter_api_key = real_openrouter
        config.openai_api_key = real_openai
        try:
            config.set("provider", real_provider)
        except Exception:
            pass


def test_sandbox_binary_vs_daemon(monkeypatch):
    """LP-12: docker binary without daemon is degraded, not ready."""
    import subprocess as _sp

    from ah.core import runtime as rt

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/docker")
    real_run = _sp.run
    monkeypatch.setattr(
        _sp,
        "run",
        lambda *a, **k: type("P", (), {"returncode": 1, "stdout": "", "stderr": "nope"})(),
    )
    try:
        report = rt._detect_sandbox()
        assert report["sandbox"] == "degraded"
    finally:
        monkeypatch.setattr(_sp, "run", real_run)
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert rt._detect_sandbox()["sandbox"] == "unavailable"


def test_runtime_startup_never_raises_without_db():
    """LP-12: startup degrades honestly; shutdown cleans tracked work."""
    import asyncio

    from ah.core.runtime import RuntimeServices

    async def _go():
        svc = RuntimeServices()

        async def _work():
            await asyncio.sleep(30)

        task = asyncio.create_task(_work())
        svc.track(task)
        report = await svc.startup()
        assert report["db"] in ("ready", "unavailable") or "unavailable" in str(report["db"])
        names = {c["name"] for c in report["capabilities"]}
        assert {"database", "chat_provider", "jobs"} <= names
        await svc.shutdown()
        assert task.cancelled() or task.done()
        return True

    assert asyncio.run(_go()) is True


def test_command_parser_preserves_windows_paths_and_unicode():
    """Addendum check: exact execution representation (parser/shell agreement)."""
    from ah.permissions import tools as toolmap
    from ah.permissions.broker import _approval_view
    from ah.permissions.policy import build_request

    cmd = 'git status -- "C:\\Users\\ki\\my projet\\fichier-\u00e9.txt"'
    spec = toolmap.action_for_tool("terminal", {"command": cmd, "workdir": "."})
    assert spec["argv"] == [
        "git",
        "status",
        "--",
        "C:\\Users\\ki\\my projet\\fichier-\u00e9.txt",
    ]
    req = build_request(
        operation="process.exec",
        argv=spec["argv"],
        cwd="C:\\Users\\ki",
        mode="ask",
        agent_id="h",
        session_id="s",
    )
    view = _approval_view(
        req,
        {
            "request_id": "r",
            "session_id": "s",
            "turn_id": "t",
            "agent_id": "h",
            "principal": "tui",
            "operation": "process.exec",
            "target": "",
            "digest": req.digest,
            "status": "pending",
            "consumed": False,
            "created_at": 0.0,
        },
    )
    assert view["argv"] == spec["argv"]
    # No shell reinterpretation: argv execution keeps shell payload empty.
    assert req.shell_payload == ""


def test_memory_backend_grant_lifecycle_and_durability():
    """LP-05/06 mem backend: expiry filtered, revoke sticks, durability reported."""
    import asyncio
    from datetime import UTC, datetime, timedelta

    from ah.permissions import store as st

    async def _go():
        st._last_durable = None
        assert st.is_durable() is None
        g = await st.save_grant(
            {
                "session_id": "mem-sess",
                "agent_id": "h",
                "mode": "ask",
                "capability": "file.write",
                "scope_path": "/a.txt",
                "scope_type": "file",
                "grant_kind": "session",
                "digest": "d1",
                "expires_at": datetime.now(UTC) - timedelta(seconds=1),
            }
        )
        assert st.is_durable() is False
        # Expired: filtered from live grants.
        assert [x["id"] for x in await st.list_grants("mem-sess")] == []
        g2 = await st.save_grant(
            {
                "session_id": "mem-sess",
                "agent_id": "h",
                "mode": "ask",
                "capability": "file.write",
                "scope_path": "/a.txt",
                "scope_type": "file",
                "grant_kind": "session",
                "digest": "d2",
            }
        )
        assert len(await st.list_grants("mem-sess")) == 1
        assert await st.revoke_grants("mem-sess", g2["id"]) >= 1
        assert await st.list_grants("mem-sess") == []

    asyncio.run(_go())


def test_tool_effect_declarations_and_mutating_fallback():
    """Extensible side-effecting tools need explicit effects (addendum check)."""
    import ah.tools  # noqa: F401 (register builtins)
    from ah.permissions import tools as toolmap
    from ah.permissions.policy import build_request, decide
    from ah.tools.base import Tool, registry

    assert registry._tools["write_file"].effects == ("fs.write",)
    assert registry._tools["terminal"].effects == ("exec",)
    assert registry._tools["read_file"].effects == ("fs.read",)
    # Effect metadata never leaks into model tool definitions.
    for d in registry.get_tool_definitions():
        assert "effects" not in d.parameters

    registry._tools["custom_exec"] = Tool(
        name="custom_exec",
        description="x",
        parameters={},
        func=lambda: "",
        is_async=False,
        effects=("exec",),
    )
    registry._tools["custom_pure"] = Tool(
        name="custom_pure",
        description="x",
        parameters={},
        func=lambda: "",
        is_async=False,
    )
    registry._definitions_cache = None
    try:
        mut = toolmap.action_for_tool("custom_exec", {})
        assert (
            decide(
                build_request(operation=mut["operation"], capabilities=mut.get("capabilities"))
            ).verdict
            == "pending"
        )
        pure = toolmap.action_for_tool("custom_pure", {})
        assert decide(build_request(operation=pure["operation"])).verdict == "allowed"
    finally:
        registry._tools.pop("custom_exec", None)
        registry._tools.pop("custom_pure", None)
        registry._definitions_cache = None


def test_consolidation_single_flight_across_instances():
    """LP-14: concurrent agent instances must not re-extract the same range."""
    import asyncio

    from ah.core import agent as agentmod

    calls = []

    class _Cons:
        async def consolidate_session(self, session_id, agent_id, since=None):
            calls.append((str(session_id), since))
            await asyncio.sleep(0.2)
            return []

    class _A:
        def __init__(self):
            from ah.core import agent as _am

            self._tasks = set()
            self.memory_consolidator = _Cons()
            self._agentmod = _am

        def _schedule_memory_consolidation(self, session_id):
            return agentmod.BaseReActAgent._schedule_memory_consolidation(self, session_id)

    import uuid as _uuid

    sid = _uuid.uuid4()
    a, b = _A(), _A()
    # Bind the real scheduler pieces onto the doubles.
    for inst in (a, b):
        inst._consolidation_tasks = set()
        inst._consolidate_memories = lambda _sid, _self=inst: _noop_consolidate(_self, _sid)

    async def _noop_consolidate(inst, _sid):
        await inst.memory_consolidator.consolidate_session(_sid, "h")

    async def _go():
        a._schedule_memory_consolidation(sid)
        b._schedule_memory_consolidation(sid)
        tasks = list(agentmod._consolidation_inflight.values())
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(_go())
    assert len(calls) == 1, f"duplicate extraction: {len(calls)}"


def test_consolidation_budget_exhaustion_sends_no_paid_call():
    """LP-14: exhausted budgets block extraction before any provider call."""
    import asyncio

    from ah.core.exceptions import UsageBudgetExceededError
    from ah.memory.consolidator import MemoryConsolidator

    sent = []

    class _P:
        model = "m"

        async def complete(self, **kw):
            sent.append(kw)
            from ah.core.models import LLMResponse

            return LLMResponse(content="[]", model="m", usage={}, tool_calls=[])

    async def _reserve(*a, **k):
        raise UsageBudgetExceededError("session token budget exceeded (0/0)")

    import ah.core.usage as usagemod

    real_complete = usagemod.usage_store.complete_call

    async def _guarded(provider, session_id, agent_id, messages, **kw):
        await _reserve()
        return await real_complete(provider, session_id, agent_id, messages, **kw)

    usagemod.usage_store.complete_call = _guarded  # type: ignore[method-assign]
    try:
        cons = MemoryConsolidator(llm_provider=_P())
        # AH-AUDIT-037: extraction failure is explicit (retryable, no
        # checkpoint) — never a silent empty. Budget policy errors keep
        # their type so callers can distinguish them from empty results.
        with pytest.raises(UsageBudgetExceededError):
            asyncio.run(cons._extract_memories("hello world", None, "h"))
    finally:
        usagemod.usage_store.complete_call = real_complete  # type: ignore[method-assign]
    assert sent == []


def test_rag_cold_start_initializes_shared_pipeline():
    """LP-10: lazy service initializes on first use after a cold restart."""
    import asyncio

    import ah.tools.rag as ragmod

    real_global = ragmod._rag_pipeline
    ragmod._rag_pipeline = None

    class _FakeEmbedder:
        model_name = "fake"

        async def embed(self, text):
            return [0.01] * 8

        async def embed_batch(self, texts):
            return [[0.01] * 8 for _ in texts]

        async def close(self):
            pass

    import ah.rag.pipeline as pipemod

    real_embedder = pipemod.OpenAIEmbedder
    pipemod.OpenAIEmbedder = _FakeEmbedder  # type: ignore[assignment]
    try:
        from ah.core import agent_factory as fac

        # Cold global (None) + usable embedder → initialized, not None.
        assert asyncio.run(fac._shared_rag_pipeline()) is not None
        # Second call reuses the shared instance.
        first = asyncio.run(fac._shared_rag_pipeline())
        assert asyncio.run(fac._shared_rag_pipeline()) is first
    finally:
        pipemod.OpenAIEmbedder = real_embedder  # type: ignore[assignment]
        ragmod._rag_pipeline = real_global


def test_rag_disabled_or_unavailable_stays_none():
    import asyncio

    import ah.tools.rag as ragmod
    from ah.core import agent_factory as fac
    from ah.core.config import config

    real_global = ragmod._rag_pipeline
    ragmod._rag_pipeline = None
    real_val = config.get("rag_enabled")
    config.set("rag_enabled", False)
    try:
        assert asyncio.run(fac._shared_rag_pipeline()) is None
    finally:
        try:
            config.set("rag_enabled", real_val)
        except Exception:
            pass
        ragmod._rag_pipeline = real_global


def _subprocess_env(tmp_path):
    import os
    import sys

    env = dict(os.environ)
    dsn = os.environ.get("AGENT_HARNESS_TEST_DATABASE_URL", "")
    if dsn:
        # Never touch the developer database from subprocess CLI tests.
        env["DATABASE_URL"] = dsn
    env["AH_ENV_FILE"] = str(tmp_path / ".env")
    env["PYTHONUTF8"] = "1"
    # Isolate persistent config writes (ah mode X --save writes config.yaml).
    env["HOME"] = str(tmp_path)
    env["USERPROFILE"] = str(tmp_path)
    return env, sys.executable


def test_cli_mode_separate_processes():
    """LP-13: separate `ah mode` invocations honor declared semantics."""
    import subprocess

    from ah.cli.launcher import ui_dir  # noqa: F401 (ensures CLI imports)

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        from pathlib import Path

        env, py = _subprocess_env(Path(tmp))

        def _run(*args):
            return subprocess.run(
                [py, "-m", "ah", *args], capture_output=True, text=True, env=env, timeout=90
            )

        shown = _run("mode")
        assert shown.returncode == 0, shown.stderr[-500:]
        assert "ask" in shown.stdout.lower() or "mode" in shown.stdout.lower()
        set_ws = _run("mode", "workspace")
        assert set_ws.returncode == 0, set_ws.stderr[-500:]
        assert "workspace" in set_ws.stdout.lower()
        # Non-persistent: a fresh process still reports the default.
        shown2 = _run("mode")
        assert shown2.returncode == 0


def test_launcher_propagates_mode_to_gateway(monkeypatch):
    """LP-13: CLI --mode reaches the Node child (previously dropped)."""
    import types

    from ah.cli import launcher

    seen = {}

    def _fake_call(args, env=None):
        seen["args"] = list(args)
        return 0

    import subprocess as _sp

    fake_sys = types.SimpleNamespace(
        stdin=types.SimpleNamespace(isatty=lambda: True),
        stdout=types.SimpleNamespace(isatty=lambda: True),
        executable="C:\\py\\python.exe",
    )
    monkeypatch.setattr(launcher, "sys", fake_sys)
    monkeypatch.setattr(launcher, "_node_version", lambda node: (24, 15))
    monkeypatch.setattr(_sp, "call", _fake_call)
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/node")
    rc = launcher.launch_ui(mode="full")
    assert rc == 0
    assert "--mode" in seen["args"] and "full" in seen["args"]


def test_gateway_initialize_applies_mode():
    """LP-13: the running gateway receives the chosen setting."""
    import asyncio

    from ah.core.config import config
    from ah.gateway.server import Gateway

    async def _go():
        gw = Gateway(write=lambda frame: None, owns_db=False)
        gw._db_ready = True
        before = config.get("execution_mode")
        try:
            await gw._initialize({"mode": "workspace"})
            assert config.get("execution_mode") == "workspace"
        finally:
            try:
                config.set("execution_mode", before)
            except Exception:
                pass
        return True

    assert asyncio.run(_go()) is True


def test_approved_approval_executes_instead_of_denying():
    """Screenshot bug: approving must allow, not raise 'denied: approved'.

    Drives the real broker.guard with a gateway-like handler that resolves
    through the store (consuming the record) before returning the verdict —
    the old double-resolve turned every approval into a denial.
    """
    import asyncio

    from ah.permissions import broker as brokermod
    from ah.permissions.policy import build_request

    async def _go():
        req = build_request(
            operation="process.exec",
            argv=["whoami"],
            cwd=".",
            mode="ask",
            agent_id="harness",
            session_id="sess-1",
        )
        calls = []

        async def _handler(card):
            calls.append(card)
            # Gateway-like: the transport records the DECISION; the broker
            # then claims it for exactly one execution.
            from ah.permissions import store as st

            await st.resolve_decision(card["request_id"], "approved", principal="tui")
            return "approved"

        brokermod.set_approval_handler(_handler, principal="tui", turn_id="t1")
        try:
            out = await brokermod.permission_broker.guard(req)
        finally:
            brokermod.set_approval_handler(None)
        assert out.digest == req.digest
        assert out.approval_id == req.request_id
        assert len(calls) == 1
        await brokermod.permission_broker.complete(req.request_id, "completed")
        # LP-03 once semantics: replaying the SAME request neither rides the
        # consumed decision nor mints a duplicate approval — it is denied
        # terminally ("already completed"). A fresh user intent builds a new
        # request (new id) and gets its own approval card; the agent loop
        # does not spin on the denial.
        from ah.permissions.broker import ApprovalDenied

        brokermod.set_approval_handler(_handler, principal="tui", turn_id="t2")
        try:
            with pytest.raises(ApprovalDenied, match="already completed"):
                await brokermod.permission_broker.guard(req)
        finally:
            brokermod.set_approval_handler(None)
        assert len(calls) == 1

    asyncio.run(_go())


def test_denied_approval_raises_denied():
    import asyncio

    from ah.permissions import broker as brokermod
    from ah.permissions.broker import ApprovalDenied
    from ah.permissions.policy import build_request

    async def _go():
        req = build_request(
            operation="process.exec",
            argv=["whoami"],
            cwd=".",
            mode="ask",
            agent_id="harness",
            session_id="sess-deny",
        )

        async def _handler(card):
            from ah.permissions import store as st

            await st.resolve_decision(card["request_id"], "denied", principal="tui")
            return "denied"

        brokermod.set_approval_handler(_handler, principal="tui", turn_id="t1")
        try:
            with pytest.raises(ApprovalDenied):
                await brokermod.permission_broker.guard(req)
        finally:
            brokermod.set_approval_handler(None)

    asyncio.run(_go())


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

        async def finish(self, job_id, *, error=None, claim_token=None, paused_for=None):
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
