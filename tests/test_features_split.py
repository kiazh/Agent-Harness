"""Test that features.py split into domain modules preserves all methods."""

from __future__ import annotations


def test_all_feature_methods_accessible_after_split():
    """All methods from the original features.py must be importable from sub-modules."""
    from machina.gateway.features import METHODS

    expected = {
        "auth.chatgpt.status",
        "auth.chatgpt.login",
        "auth.chatgpt.logout",
        "auth.chatgpt.importCodex",
        "session.fork",
        "session.delete",
        "session.rename",
        "session.setGoal",
        "session.setModel",
        "session.search",
        "session.recall",
        "session.recall.window",
        "session.export",
        "context.get",
        "context.compress",
        "memory.list",
        "memory.search",
        "memory.share",
        "memory.add",
        "memory.forget",
        "memory.pending",
        "memory.approve",
        "memory.reject",
        "memory.approveAll",
        "memory.rejectAll",
        "memory.stats",
        "growth.get",
        "growth.teach",
        "growth.feedback",
        "growth.forget",
        "growth.installPolicy",
        "growth.rollback",
        "interop.status",
        "interop.enable",
        "interop.disable",
        "mcp.list",
        "mcp.status",
        "mcp.configure",
        "mcp.remove",
        "mcp.preview",
        "mcp.connect",
        "mcp.disconnect",
        "mcp.tools",
        "mcp.oauth.preview",
        "mcp.oauth.start",
        "mcp.oauth.status",
        "mcp.oauth.url",
        "mcp.oauth.open",
        "mcp.oauth.cancel",
        "mcp.oauth.forget",
        "skills.list",
        "skills.show",
        "skills.learn",
        "skills.delete",
        "skills.curator",
        "learning.list",
        "learning.approve",
        "learning.reject",
        "learning.revert",
        "config.get",
        "secrets.list",
        "secrets.set",
        "secrets.clear",
        "models.list",
        "models.setEffort",
        "providers.list",
        "providers.add",
        "providers.remove",
        "profile.get",
        "profile.set",
        "profile.list",
        "status",
        "agents.list",
        "agents.save",
        "agents.delete",
        "agents.run",
        "agents.history",
        "jobs.create",
        "jobs.list",
        "jobs.setEnabled",
        "jobs.resume",
        "jobs.delete",
        "mode.get",
        "mode.set",
        "mode.revoke",
        "approvals.list",
        "approvals.resolve",
    }
    assert set(METHODS) == expected


def test_submodules_importable():
    """Each domain module must be importable."""
    from machina.gateway.features import (
        agents,
        auth_chatgpt,
        config,
        interop,
        jobs,
        memory,
        providers_feature,
        secrets,
        sessions,
        skills,
    )

    assert hasattr(sessions, "session_fork")
    assert hasattr(memory, "memory_list")
    assert hasattr(skills, "skills_list")
    assert hasattr(agents, "agents_list")
    assert hasattr(jobs, "jobs_create")
    assert hasattr(config, "config_get")
    assert hasattr(secrets, "secrets_list")
    assert hasattr(interop, "interop_status")
    assert hasattr(auth_chatgpt, "auth_chatgpt_status")
    assert hasattr(providers_feature, "models_list")
    assert hasattr(providers_feature, "providers_add")


def test_register_still_works():
    """register() must still add all methods to a gateway."""
    from machina.gateway.features import METHODS, register
    from machina.gateway.server import Gateway

    gw = Gateway(lambda frame: None)
    register(gw)
    assert set(METHODS) <= set(gw._methods)


def test_coerce_config_value_preserved():
    """coerce_config_value must still be accessible."""
    from machina.gateway.features import coerce_config_value

    assert coerce_config_value("max_iterations", "7") == 7
    assert coerce_config_value("verbose", "off") is False


# ─── interop feature handlers (in-process delegation to machina.interop) ─────


def _interop_row(**over):
    from machina.interop import DetectResult

    values = {
        "platform": "opencode",
        "label": "OpenCode",
        "installed": True,
        "binary": "/fake/bin/opencode",
        "version": "opencode v2.0.26",
        "running": True,
        "registered": True,
        "registered_detail": "listed (connected)",
        "scopes": ["project"],
        "enabled": None,
        "state_scope": None,
        "notes": ["registered outside Machina's state (shows as manual)"],
    }
    values.update(over)
    return DetectResult(**values)


async def test_interop_status_returns_json_safe_rows(monkeypatch):
    import json

    from machina.gateway.features import interop as feature

    fake_results = {
        "opencode": _interop_row(),
        "codex": _interop_row(
            platform="codex",
            label="Codex",
            version=None,
            registered=False,
            registered_detail="not listed",
            scopes=[],
            enabled=False,
            state_scope="global",
            notes=[],
        ),
    }
    monkeypatch.setattr(feature, "detect", lambda platforms=None: fake_results)

    result = await feature.interop_status(None, {})

    assert result == {
        "platforms": [
            {
                "platform": "opencode",
                "label": "OpenCode",
                "installed": True,
                "version": "opencode v2.0.26",
                "running": True,
                "registered": True,
                "enabled": None,
                "scope": "project",
                "notes": ["registered outside Machina's state (shows as manual)"],
            },
            {
                "platform": "codex",
                "label": "Codex",
                "installed": True,
                "version": None,
                "running": True,
                "registered": False,
                "enabled": False,
                "scope": "global",
                "notes": [],
            },
        ]
    }
    json.dumps(result)  # wire-safe: explicit fields, no dataclasses or binary paths


async def test_interop_handlers_reject_unknown_platform_with_invalid_params(monkeypatch):
    import pytest

    from machina.gateway.errors import INVALID_PARAMS, RpcError
    from machina.gateway.features import interop as feature

    calls: list[str] = []
    monkeypatch.setattr(feature, "enable", lambda *a, **k: calls.append("enable"))
    monkeypatch.setattr(feature, "disable", lambda *a, **k: calls.append("disable"))

    with pytest.raises(RpcError) as enable_error:
        await feature.interop_enable(None, {"platform": "not-a-platform"})
    assert enable_error.value.code == INVALID_PARAMS
    assert "Unknown platform" in enable_error.value.message

    with pytest.raises(RpcError) as disable_error:
        await feature.interop_disable(None, {"platform": "not-a-platform"})
    assert disable_error.value.code == INVALID_PARAMS
    assert "Unknown platform" in disable_error.value.message

    assert calls == [], "unknown platforms must not reach the interop module"


async def test_interop_enable_rejects_bad_input_before_delegating(monkeypatch):
    import pytest

    from machina.gateway.errors import INVALID_PARAMS, RpcError
    from machina.gateway.features import interop as feature

    calls: list = []
    monkeypatch.setattr(feature, "enable", lambda *a, **k: calls.append((a, k)))

    for params in (
        {},  # platform is required
        {"platform": "codex", "scope": "session"},
        {"platform": "codex", "dry_run": "maybe"},
    ):
        with pytest.raises(RpcError) as error:
            await feature.interop_enable(None, params)
        assert error.value.code == INVALID_PARAMS

    assert calls == []


async def test_interop_enable_delegates_and_surfaces_failures(monkeypatch):
    from machina.gateway.features import interop as feature
    from machina.interop import ActionResult

    calls: list[dict] = []

    def fake_enable(platform, *, scope="project", dry_run=False):
        calls.append({"platform": platform, "scope": scope, "dry_run": dry_run})
        return ActionResult(
            platform=platform,
            action="enable",
            ok=False,
            changed=False,
            message="registration of `machina` for Codex did not verify: not present",
            steps=["run: codex mcp add machina -- ..."],
            verify={"ok": False, "detail": "not present"},
        )

    monkeypatch.setattr(feature, "enable", fake_enable)

    result = await feature.interop_enable(
        None, {"platform": "Codex", "scope": "global", "dry_run": True}
    )
    assert calls == [{"platform": "codex", "scope": "global", "dry_run": True}]
    assert result["ok"] is False and result["changed"] is False
    assert result["action"] == "enable"
    assert "did not verify" in result["message"]
    assert result["steps"] == ["run: codex mcp add machina -- ..."]
    assert result["verify"] == {"ok": False, "detail": "not present"}

    # Omitted options fall back to the interop defaults (project, live run).
    await feature.interop_enable(None, {"platform": "codex"})
    assert calls[-1] == {"platform": "codex", "scope": "project", "dry_run": False}


async def test_interop_disable_passes_through_manual_step_failure(monkeypatch):
    from machina.gateway.features import interop as feature
    from machina.interop import ActionResult

    def fake_disable(platform):
        return ActionResult(
            platform=platform,
            action="disable",
            ok=False,
            changed=False,
            message="OpenCode's CLI has no MCP remove subcommand; delete the `machina` entry manually",
            steps=["edit /x/opencode.json and remove the `machina` entry"],
        )

    monkeypatch.setattr(feature, "disable", fake_disable)

    result = await feature.interop_disable(None, {"platform": "opencode"})
    assert result["ok"] is False and result["changed"] is False
    assert result["action"] == "disable"
    assert "no MCP remove subcommand" in result["message"]
    assert result["steps"] == ["edit /x/opencode.json and remove the `machina` entry"]
    assert result["verify"] == {}
