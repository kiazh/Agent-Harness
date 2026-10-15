"""H2 /harnesses gateway feature: ``interop.list`` / ``interop.run`` (+ enable/disable).

External-process boundary
-------------------------
Every function that could spawn a harness CLI lives in ``machina.interop``. The
feature module delegates to those functions — ``detect`` / ``enable`` /
``disable`` directly, and the frozen run surface (``supported_runs`` /
``run_task`` / ``state_path``) through module-level indirections. These tests
stub those feature-module references, so no external process is ever launched;
the real delegation targets are ``machina.interop``.
"""

from __future__ import annotations

import json

import pytest

from machina.gateway.errors import INVALID_PARAMS, UNAUTHORIZED, RpcError
from machina.gateway.features import interop as feature
from machina.gateway.server import Gateway
from machina.interop import ActionResult, DetectResult


def _row(**over) -> DetectResult:
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


class _RunResult:
    """Stand-in for the frozen ``machina.interop.RunResult``."""

    def __init__(
        self,
        *,
        command=(),
        exit_code=0,
        stdout="",
        stderr="",
        ok=True,
        unsupported_reason=None,
    ) -> None:
        self.platform = "codex"
        self.command = command
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.ok = ok
        self.unsupported_reason = unsupported_reason


class WireHarness:
    """Drive one gateway over the JSON-RPC frame path with stubbed interop."""

    def __init__(self) -> None:
        self.frames: list[dict] = []
        self.gateway = Gateway(
            lambda frame: self.frames.append(json.loads(json.dumps(frame))),
            owns_db=False,
            inherit_env_token=False,
        )

    async def call(self, method: str, params: dict | None = None) -> dict:
        request_id = len(self.frames) + 1
        await self.gateway.handle_line(
            json.dumps(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
            )
        )
        return next(frame for frame in self.frames if frame.get("id") == request_id)


@pytest.fixture
def stub_interop(monkeypatch):
    """Stub the external-process boundary; return the recorded run calls."""
    calls: dict[str, list] = {"detect": [], "runs": [], "enable": [], "disable": []}

    def detect():
        calls["detect"].append(True)
        return {
            "opencode": _row(),
            "codex": _row(
                platform="codex",
                label="Codex",
                version=None,
                running=False,
                registered=False,
                registered_detail="not listed",
                scopes=[],
                enabled=False,
                state_scope="global",
                notes=[],
            ),
        }

    monkeypatch.setattr(feature, "detect", detect)
    monkeypatch.setattr(feature, "supported_runs", lambda: {"codex": "codex exec <prompt>"})
    monkeypatch.setattr(feature, "state_path", lambda: "/tmp/interop-state.json")
    monkeypatch.setattr(
        feature,
        "enable",
        lambda platform, *, scope="project", dry_run=False: (
            calls["enable"].append({"platform": platform, "scope": scope, "dry_run": dry_run})
            or ActionResult(platform=platform, action="enable", ok=True, changed=True, message="ok")
        ),
    )
    monkeypatch.setattr(
        feature,
        "disable",
        lambda platform: (
            calls["disable"].append(platform)
            or ActionResult(
                platform=platform, action="disable", ok=True, changed=True, message="ok"
            )
        ),
    )

    def run_task(platform, prompt, *, timeout=feature.DEFAULT_RUN_TIMEOUT_SECONDS):
        calls["runs"].append({"platform": platform, "prompt": prompt, "timeout": timeout})
        return _RunResult(
            command=["codex", "exec", "sk-abcdefghijklmnopqrstuv"],
            exit_code=0,
            stdout="seeded output\n",
            stderr="",
        )

    monkeypatch.setattr(feature, "run_task", run_task)
    return calls


# ─── interop.list ───────────────────────────────────────────────────────────


async def test_interop_list_reports_state_and_runnability(stub_interop):
    result = await feature.interop_list(None, {})

    assert result == {
        "harnesses": [
            {
                "key": "opencode",
                "label": "OpenCode",
                "installed": True,
                "version": "opencode v2.0.26",
                "running": True,
                "registered": True,
                "scope": "project",
                "runSupported": False,
                "runHint": None,
            },
            {
                "key": "codex",
                "label": "Codex",
                "installed": True,
                "version": None,
                "running": False,
                "registered": False,
                "scope": "global",
                "runSupported": True,
                "runHint": "codex exec <prompt>",
            },
        ],
        "stateFile": "/tmp/interop-state.json",
    }
    json.dumps(result)  # wire-safe: explicit fields, no dataclasses or binary paths
    assert stub_interop["detect"], "detection must run on-demand"


async def test_interop_list_is_empty_when_nothing_detected(monkeypatch):
    monkeypatch.setattr(feature, "detect", lambda: {})
    monkeypatch.setattr(feature, "supported_runs", lambda: {})
    monkeypatch.setattr(feature, "state_path", lambda: "/tmp/interop-state.json")
    result = await feature.interop_list(None, {})
    assert result == {"harnesses": [], "stateFile": "/tmp/interop-state.json"}


# ─── interop.run ────────────────────────────────────────────────────────────


async def test_interop_run_returns_bounded_redacted_output(stub_interop):
    result = await feature.interop_run(None, {"platform": "codex", "prompt": "summarize the repo"})

    assert stub_interop["runs"] == [
        {"platform": "codex", "prompt": "summarize the repo", "timeout": 300.0}
    ]
    assert result["ok"] is True
    assert result["exitCode"] == 0
    assert result["stdout"] == "seeded output"
    assert result["stderr"] == ""
    assert result["unsupportedReason"] is None
    # Secrets never cross the wire even when the CLI echoed one in argv.
    assert "sk-abcdefghijklmnopqrstuv" not in json.dumps(result)
    assert "[REDACTED" in result["command"]


async def test_interop_run_caps_oversized_streams(monkeypatch):
    huge = "x" * (feature.MAX_RUN_STDOUT_CHARS + 5_000)
    monkeypatch.setattr(
        feature,
        "run_task",
        lambda platform, prompt, *, timeout: _RunResult(stdout=huge, stderr=huge),
    )
    result = await feature.interop_run(None, {"platform": "codex", "prompt": "go"})
    assert len(result["stdout"]) <= feature.MAX_RUN_STDOUT_CHARS
    assert len(result["stderr"]) <= feature.MAX_RUN_STDERR_CHARS
    assert result["stdout"].endswith("[truncated]")


async def test_interop_run_forwards_timeout(monkeypatch):
    seen: list[float] = []
    monkeypatch.setattr(
        feature,
        "run_task",
        lambda platform, prompt, *, timeout: seen.append(timeout) or _RunResult(),
    )
    await feature.interop_run(None, {"platform": "codex", "prompt": "go", "timeout": 42})
    assert seen == [42.0]


async def test_interop_run_surfaces_unsupported_surface(monkeypatch):
    monkeypatch.setattr(
        feature,
        "run_task",
        lambda platform, prompt, *, timeout: _RunResult(
            command=(),
            exit_code=None,
            ok=False,
            unsupported_reason="codex cannot run a delegated prompt",
        ),
    )
    result = await feature.interop_run(None, {"platform": "codex", "prompt": "go"})
    assert result["ok"] is False
    assert result["exitCode"] is None
    assert result["command"] == ""
    assert result["unsupportedReason"] == "codex cannot run a delegated prompt"


@pytest.mark.parametrize(
    "params",
    [
        {},  # platform required
        {"platform": "codex"},  # prompt required
        {"platform": "not-a-harness", "prompt": "hi"},  # unknown platform
        {"platform": "codex", "prompt": "hi", "timeout": "soon"},  # bad type
        {"platform": "codex", "prompt": "hi", "timeout": 0},  # out of range
        {"platform": "codex", "prompt": "hi", "timeout": 10_000},  # out of range
    ],
)
async def test_interop_run_rejects_bad_input_before_delegating(monkeypatch, params):
    called: list = []
    monkeypatch.setattr(feature, "run_task", lambda *a, **k: called.append((a, k)) or _RunResult())
    with pytest.raises(RpcError) as error:
        await feature.interop_run(None, params)
    assert error.value.code == INVALID_PARAMS
    assert called == [], "invalid input must not reach the interop module"


# ─── enable / disable delegation ────────────────────────────────────────────


async def test_interop_enable_and_disable_delegate(stub_interop):
    enabled = await feature.interop_enable(None, {"platform": "codex", "scope": "global"})
    assert enabled["ok"] is True and enabled["action"] == "enable"
    disabled = await feature.interop_disable(None, {"platform": "codex"})
    assert disabled["ok"] is True and disabled["action"] == "disable"
    assert stub_interop["enable"] == [{"platform": "codex", "scope": "global", "dry_run": False}]
    assert stub_interop["disable"] == ["codex"]


# ─── auth + wire path ───────────────────────────────────────────────────────


async def test_harness_methods_require_gateway_auth(stub_interop):
    harness = WireHarness()
    harness.gateway._auth_token = "fixture-token"

    for method in ("interop.list", "interop.run"):
        denied = await harness.call(method)
        assert denied["error"]["code"] == UNAUTHORIZED, (method, denied)

    listed = await harness.call("interop.list", {"token": "fixture-token"})
    assert listed["result"]["harnesses"][0]["key"] == "opencode"


async def test_harness_methods_are_registered(stub_interop):
    harness = WireHarness()
    for method in ("interop.list", "interop.enable", "interop.disable", "interop.run"):
        assert method in harness.gateway._methods


async def test_interop_run_over_the_wire_is_bounded(stub_interop):
    harness = WireHarness()
    harness.gateway._auth_token = None
    response = await harness.call(
        "interop.run", {"platform": "codex", "prompt": "hello harness", "timeout": 30}
    )
    assert response.get("error") is None, response
    result = response["result"]
    assert result["ok"] is True and result["exitCode"] == 0
    assert result["stdout"] == "seeded output"
    assert "sk-abcdefghijklmnopqrstuv" not in json.dumps(result)
    assert stub_interop["runs"][0]["timeout"] == 30.0
