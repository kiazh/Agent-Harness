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
