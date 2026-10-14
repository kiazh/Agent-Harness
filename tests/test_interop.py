"""Interop self-setup tests (``machina/interop.py`` + ``machina/cli/interop.py``).

Every subprocess is faked: these tests never spawn a real harness CLI and
never register anything. The interop module routes all spawns through
``machina.interop._run`` and all PATH lookups through ``_which``, so pinning
those two seams (plus the config root helpers) fully isolates the suite from
the machine. The autouse guard below turns any accidental real spawn into a
loud failure instead of a silent registration.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

import machina.cli as cli
import machina.interop as interop

runner = CliRunner()


def _result(output: str = "", rc: int = 0, stderr: str = "", timed_out: bool = False):
    return interop.RunResult(
        returncode=rc, output=output, stderr=stderr, timed_out=timed_out, truncated=False
    )


def _is_pgrep(argv: list[str]) -> bool:
    return Path(argv[0]).name == "pgrep"


@pytest.fixture(autouse=True)
def _sandboxed_homes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect every config/state root into tmp_path and forbid real spawns."""
    monkeypatch.setenv(interop.STATE_ENV_OVERRIDE, str(tmp_path / "state" / "interop.json"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    monkeypatch.setattr(interop, "_repo_root", lambda: repo)

    def _no_real_spawn(*args, **kwargs):  # pragma: no cover - only fires on a bug
        raise AssertionError("test attempted a real subprocess spawn")

    monkeypatch.setattr(interop.subprocess, "Popen", _no_real_spawn)
    return tmp_path


class FakeRunner:
    """Records argv/stdin and answers by a list of (matcher, result) rules."""

    def __init__(self, rules: list[tuple[object, object]] | None = None) -> None:
        self.rules = list(rules or [])
        self.calls: list[dict] = []

    def __call__(self, argv, *, env=None, timeout=0.0, stdin_text=None, cwd=None):
        self.calls.append({"argv": list(argv), "stdin": stdin_text, "timeout": timeout, "cwd": cwd})
        for matcher, answer in self.rules:
            if matcher(argv):
                return answer(argv) if callable(answer) else answer
        raise AssertionError(f"unexpected spawn in test: {argv!r}")

    def argvs(self) -> list[list[str]]:
        return [call["argv"] for call in self.calls]


# ─── Listing parsers ────────────────────────────────────────────────────────


def test_parse_opencode_listing_finds_machina_and_keeps_statuses():
    text = (
        "✓ machina              connected\n"
        "✓ mcp-server-linkedin  connected\n"
        "✗ broken               failed\n"
    )
    rows = interop._parse_opencode_listing(text)
    assert ("machina", "connected") in rows
    assert ("mcp-server-linkedin", "connected") in rows
    assert ("broken", "failed") in rows


def test_parse_codex_json_listing_reports_enabled_state():
    payload = json.dumps(
        [
            {
                "name": "machina",
                "enabled": True,
                "disabled_reason": None,
                "transport": {"type": "stdio", "command": "/x/python", "args": ["-m"]},
            },
            {"name": "other", "enabled": False, "disabled_reason": "config issue"},
        ]
    )
    rows = interop._parse_codex_listing(payload, json_mode=True)
    assert ("machina", "enabled") in rows
    assert ("other", "disabled (config issue)") in rows


def test_parse_codex_json_rejects_unexpected_shape():
    with pytest.raises(ValueError):
        interop._parse_codex_listing(json.dumps({"nope": 1}), json_mode=True)


def test_parse_hermes_listing_skips_headers_and_separators():
    text = (
        "\n  MCP Servers:\n\n"
        "  Name             Transport                      Tools        Status    \n"
        "  ──────────────── ────────────────────────────── ──────────── ──────────\n"
        "  machina          /Users/x/.venv/bin/python -m   all          ✓ enabled\n"
        "\n"
    )
    assert interop._parse_hermes_listing(text) == [("machina", "enabled")]


# ─── Detection ──────────────────────────────────────────────────────────────


def _detect_runner() -> FakeRunner:
    return FakeRunner(
        [
            (
                lambda a: _is_pgrep(a) and "opencode" in a[-1],
                lambda a: _result("4242 /fake/bin/opencode run\n"),
            ),
            (lambda a: _is_pgrep(a), lambda a: _result("", rc=1)),
            (
                lambda a: a[1:] == ["--version"] and "opencode" in a[0],
                lambda a: _result("opencode v2.0.26\n"),
            ),
            (
                lambda a: a[1:] == ["--version"] and "codex" in a[0],
                lambda a: _result("codex-cli 0.162.1\n"),
            ),
            (
                lambda a: a[1:] == ["--version"] and "hermes" in a[0],
                lambda a: _result("Hermes Agent v0.21.6\n"),
            ),
            (
                lambda a: "opencode" in a[0] and a[1:3] == ["mcp", "list"],
                lambda a: _result("✓ machina              connected\n✓ other  connected\n"),
            ),
            (
                lambda a: "codex" in a[0] and a[1:4] == ["mcp", "list", "--json"],
                lambda a: _result(json.dumps([])),
            ),
            (lambda a: "hermes" in a[0] and a[1:3] == ["mcp", "list"], lambda a: _result("")),
        ]
    )


def test_detect_uses_list_output_and_config_file_evidence(monkeypatch, tmp_path):
    # opencode: listed as connected AND present in the project file.
    (tmp_path / "repo" / "opencode.json").write_text(
        json.dumps({"mcp": {"servers": {"machina": {"type": "local", "command": ["x"]}}}}),
        encoding="utf-8",
    )
    # codex: not in `mcp list --json`, present in the user config.toml instead.
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        'model = "x"\n\n[mcp_servers.machina]\ncommand = "/x/python"\nargs = ["-m", "machina.mcp_server"]\n',
        encoding="utf-8",
    )
    # hermes: not listed, present in config.yaml instead.
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        "model:\n  default: x\nmcp_servers:\n  machina:\n    command: /x/python\n    args:\n      - -m\n      - machina.mcp_server\n    enabled: true\n_config_version: 50\n",
        encoding="utf-8",
    )
    fake = _detect_runner()
    monkeypatch.setattr(interop, "_run", fake)
    monkeypatch.setattr(interop, "_which", lambda spec: f"/fake/bin/{spec.key}")

    results = interop.detect()

    opencode = results["opencode"]
    assert opencode.installed and opencode.version == "opencode v2.0.26"
    assert opencode.running
    assert opencode.registered and opencode.registered_detail == "listed (connected)"
    assert opencode.scopes == ["project"]
    assert opencode.enabled is None and opencode.enabled_cell == "manual"
    assert any("outside Machina's state" in note for note in opencode.notes)

    codex = results["codex"]
    assert codex.registered and codex.scopes == ["global"] and not codex.running
    assert any("config entry" in note for note in codex.notes)

    hermes = results["hermes"]
    assert hermes.installed and hermes.registered and hermes.scopes == ["global"]
    assert hermes.registered_detail == "not listed"
    assert not hermes.running


def test_detect_reports_not_installed_cleanly(monkeypatch):
    monkeypatch.setattr(interop, "_which", lambda spec: None)
    monkeypatch.setattr(
        interop,
        "_run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no spawn for missing binary")),
    )
    result = interop.detect(["opencode"])["opencode"]
    assert not result.installed and result.version is None and not result.running
    assert not result.registered and result.enabled_cell == "no"
    assert "not installed" in result.notes[0]


def test_detect_matches_running_by_binary_boundary(monkeypatch):
    fake = FakeRunner(
        [
            (lambda a: _is_pgrep(a), lambda a: _result("777 /Users/x/.hermes-agent/notes.txt\n")),
        ]
    )
    monkeypatch.setattr(interop, "_which", lambda spec: "/fake/bin/hermes")
    monkeypatch.setattr(interop, "_run", fake)
    # `--version`/`mcp list` would also be needed for a full detect; call the
    # running scan directly to pin the boundary rule.
    assert interop._is_running(interop.PLATFORMS["hermes"]) is False


@pytest.mark.skipif(os.name == "nt", reason="process-list scan is PGREP/PS based")
def test_is_running_parses_pgrep_output(monkeypatch):
    fake = FakeRunner(
        [
            (lambda a: _is_pgrep(a), lambda a: _result("4242 /fake/bin/opencode run --json\n")),
        ]
    )
    monkeypatch.setattr(interop, "_run", fake)
    assert interop._is_running(interop.PLATFORMS["opencode"]) is True


# ─── Enable: idempotency, dry-run, verification, state ─────────────────────


def test_enable_already_registered_short_circuits_without_add(monkeypatch, tmp_path):
    (tmp_path / "repo" / "opencode.json").write_text(
        json.dumps(
            {
                "mcp": {
                    "servers": {
                        "machina": {
                            "type": "local",
                            "command": ["/x/python", "-m", "machina.mcp_server"],
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    fake = FakeRunner(
        [
            (lambda a: a[1:] == ["--version"], lambda a: _result("opencode v2.0.26\n")),
            (
                lambda a: a[1:3] == ["mcp", "list"],
                lambda a: _result("✓ machina              connected\n"),
            ),
        ]
    )
    monkeypatch.setattr(interop, "_which", lambda spec: "/fake/bin/opencode")
    monkeypatch.setattr(interop, "_run", fake)

    result = interop.enable("opencode", scope="project")

    assert result.ok and result.changed is False
    assert "already registered" in result.message
    assert all("add" not in argv for argv in fake.argvs()), "no add command may run"
    state = interop.load_state()
    assert state["opencode"]["enabled"] is True
    assert state["opencode"]["scope"] == "project"
    assert state["opencode"]["verify"]["ok"] is True


def test_enable_dry_run_plans_argv_and_spawns_nothing(monkeypatch):
    calls: list = []
    monkeypatch.setattr(interop, "_which", lambda spec: "/fake/bin/codex")
    monkeypatch.setattr(interop, "_run", lambda *a, **k: calls.append(a) or _result())

    result = interop.enable("codex", dry_run=True)

    assert result.ok and result.changed is False and calls == []
    argv = result.argv[0]
    assert argv[:4] == ["/fake/bin/codex", "mcp", "add", "machina"]
    assert argv[4] == "--"
    assert argv[5:] == [sys.executable, "-m", "machina.mcp_server"]
    assert interop.load_state() == {}
    # codex is global-only: the clamp is surfaced, not silent.
    assert any("user-level only" in step for step in result.steps)


def test_enable_registers_verifies_and_persists_state(monkeypatch):
    adds: list[dict] = []

    def fake_run(argv, *, env=None, timeout=0.0, stdin_text=None, cwd=None):
        if argv[1:] == ["--version"]:
            return _result("codex-cli 0.162.1\n")
        if argv[1:4] == ["mcp", "list", "--json"]:
            listed = bool(adds)
            return _result(json.dumps([{"name": "machina", "enabled": True}]) if listed else "[]")
        if argv[1:3] == ["mcp", "add"]:
            adds.append({"argv": argv, "stdin": stdin_text})
            return _result("Added global MCP server 'machina'.\n")
        raise AssertionError(f"unexpected: {argv}")

    monkeypatch.setattr(interop, "_which", lambda spec: "/fake/bin/codex")
    monkeypatch.setattr(interop, "_run", fake_run)

    result = interop.enable("codex")

    assert result.ok and result.changed
    assert adds == [
        {
            "argv": [
                "/fake/bin/codex",
                "mcp",
                "add",
                "machina",
                "--",
                sys.executable,
                "-m",
                "machina.mcp_server",
            ],
            "stdin": None,
        }
    ]
    assert result.verify["ok"] is True
    entry = interop.load_state()["codex"]
    assert entry["enabled"] is True and entry["scope"] == "global"
    assert entry["binary"] == "/fake/bin/codex" and entry["version"] == "codex-cli 0.162.1"
    assert entry["verify"] == {"ok": True, "detail": "present in `codex mcp list --json` (enabled)"}
    assert entry["registered_at"]


def test_enable_reports_failure_when_registration_does_not_verify(monkeypatch, tmp_path):
    def fake_run(argv, *, env=None, timeout=0.0, stdin_text=None, cwd=None):
        if argv[1:] == ["--version"]:
            return _result("codex-cli 0.162.1\n")
        if argv[1:3] == ["mcp", "add"]:
            return _result("Added global MCP server 'machina'.\n")
        if argv[1:4] == ["mcp", "list", "--json"]:
            return _result("[]", rc=0)
        raise AssertionError(f"unexpected: {argv}")

    monkeypatch.setattr(interop, "_which", lambda spec: "/fake/bin/codex")
    monkeypatch.setattr(interop, "_run", fake_run)

    result = interop.enable("codex")

    assert result.ok is False and result.changed is True
    assert "did not verify" in result.message
    assert result.verify["ok"] is False
    assert interop.load_state()["codex"]["enabled"] is False
    assert not (tmp_path / "codex" / "config.toml").exists()


def test_hermes_enable_answers_the_interactive_prompt(monkeypatch):
    adds: list[dict] = []
    saved = {"done": False}

    def fake_run(argv, *, env=None, timeout=0.0, stdin_text=None, cwd=None):
        if argv[1:] == ["--version"]:
            return _result("Hermes Agent v9.9.9\n")
        if argv[1:3] == ["mcp", "add"]:
            adds.append({"argv": argv, "stdin": stdin_text})
            saved["done"] = True
            return _result("✓ Saved 'machina' to config.yaml (3/3 tools enabled)\n")
        if argv[1:3] == ["mcp", "list"]:
            row = (
                "  machina          /x/python -m   all          ✓ enabled\n"
                if saved["done"]
                else ""
            )
            return _result(row)
        raise AssertionError(f"unexpected: {argv}")

    monkeypatch.setattr(interop, "_which", lambda spec: "/fake/bin/hermes")
    monkeypatch.setattr(interop, "_run", fake_run)

    result = interop.enable("hermes")

    assert result.ok and result.changed
    assert adds == [
        {
            "argv": [
                "/fake/bin/hermes",
                "mcp",
                "add",
                "machina",
                "--command",
                sys.executable,
                "--args",
                "-m",
                "machina.mcp_server",
            ],
            "stdin": "\n",
        }
    ]
    assert interop.load_state()["hermes"]["enabled"] is True


def test_enable_unknown_platform_raises_and_bad_scope_reports():
    with pytest.raises(interop.UnknownPlatform):
        interop.enable("nope")
    result = interop.enable("opencode", scope="session")
    assert result.ok is False and "scope must be project or global" in result.message


# ─── Disable ────────────────────────────────────────────────────────────────


def test_disable_opencode_reports_the_manual_step(monkeypatch):
    fake = FakeRunner(
        [
            (lambda a: a[1:] == ["--version"], lambda a: _result("opencode v2.0.26\n")),
            (
                lambda a: a[1:3] == ["mcp", "list"],
                lambda a: _result("✓ machina              connected\n"),
            ),
        ]
    )
    monkeypatch.setattr(interop, "_which", lambda spec: "/fake/bin/opencode")
    monkeypatch.setattr(interop, "_run", fake)

    result = interop.disable("opencode")

    assert result.ok is False and result.changed is False
    assert "no MCP remove subcommand" in result.message
    assert "opencode.json" in result.message
    assert "opencode" not in interop.load_state() or not interop.load_state()["opencode"]["enabled"]


def test_disable_codex_removes_verifies_and_records(monkeypatch):
    removed = {"done": False}

    def fake_run(argv, *, env=None, timeout=0.0, stdin_text=None, cwd=None):
        if argv[1:] == ["--version"]:
            return _result("codex-cli 0.162.1\n")
        if argv[1:3] == ["mcp", "remove"]:
            removed["done"] = True
            return _result("Removed global MCP server 'machina'.\n")
        if argv[1:4] == ["mcp", "list", "--json"]:
            listing = [] if removed["done"] else [{"name": "machina", "enabled": True}]
            return _result(json.dumps(listing))
        raise AssertionError(f"unexpected: {argv}")

    monkeypatch.setattr(interop, "_which", lambda spec: "/fake/bin/codex")
    monkeypatch.setattr(interop, "_run", fake_run)

    result = interop.disable("codex")

    assert result.ok and result.changed
    assert result.verify["ok"] is True
    entry = interop.load_state()["codex"]
    assert entry["enabled"] is False
    assert entry["verify"]["ok"] is True


def test_disable_when_not_registered_is_a_clean_noop(monkeypatch):
    fake = FakeRunner(
        [
            (lambda a: a[1:] == ["--version"], lambda a: _result("Hermes Agent v9\n")),
            (lambda a: a[1:3] == ["mcp", "list"], lambda a: _result("")),
        ]
    )
    monkeypatch.setattr(interop, "_which", lambda spec: "/fake/bin/hermes")
    monkeypatch.setattr(interop, "_run", fake)

    result = interop.disable("hermes")

    assert result.ok and result.changed is False
    assert "nothing to remove" in result.message
    assert interop.load_state()["hermes"]["enabled"] is False


# ─── State file ─────────────────────────────────────────────────────────────


def test_state_write_is_atomic_0600_and_reads_back(monkeypatch):
    interop._record_state(
        interop.PLATFORMS["codex"],
        enabled=True,
        scope="global",
        verify={"ok": True, "detail": "listed"},
        binary="/fake/bin/codex",
        version="codex-cli 0.162.1",
    )
    path = interop.state_path()
    assert path.is_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list(path.parent.glob(".interop-*")) == [], "temp file must not survive"
    state = interop.load_state()
    assert state["codex"] == {
        "enabled": True,
        "scope": "global",
        "registered_at": state["codex"]["registered_at"],
        "verify": {"ok": True, "detail": "listed"},
        "binary": "/fake/bin/codex",
        "version": "codex-cli 0.162.1",
    }

    # Updating preserves registered_at and replaces the file atomically.
    registered_at = state["codex"]["registered_at"]
    interop._record_state(
        interop.PLATFORMS["codex"],
        enabled=False,
        scope="global",
        verify={"ok": True, "detail": "removed"},
        binary="/fake/bin/codex",
        version=None,
    )
    updated = interop.load_state()["codex"]
    assert updated["enabled"] is False
    assert updated["registered_at"] == registered_at


def test_load_state_tolerates_missing_and_corrupt_files(tmp_path):
    assert interop.load_state() == {}
    path = interop.state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json {", encoding="utf-8")
    assert interop.load_state() == {}
    path.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
    assert interop.load_state() == {}


# ─── CLI surface ────────────────────────────────────────────────────────────


def _fake_detect_results() -> dict[str, interop.DetectResult]:
    return {
        "opencode": interop.DetectResult(
            platform="opencode",
            label="OpenCode",
            installed=True,
            binary="/fake/bin/opencode",
            version="opencode v2.0.26",
            running=True,
            registered=True,
            registered_detail="listed (connected)",
            scopes=["project"],
            enabled=None,
            state_scope=None,
            notes=[],
        ),
        "codex": interop.DetectResult(
            platform="codex",
            label="Codex",
            installed=True,
            binary="/fake/bin/codex",
            version="codex-cli 0.162.1",
            running=False,
            registered=False,
            registered_detail="not listed",
            scopes=[],
            enabled=None,
            state_scope=None,
            notes=[],
        ),
        "hermes": interop.DetectResult(
            platform="hermes",
            label="Hermes",
            installed=False,
            binary=None,
            version=None,
            running=False,
            registered=False,
            registered_detail="not installed",
            scopes=[],
            enabled=None,
            state_scope=None,
            notes=[],
        ),
    }


def test_cli_status_renders_table_and_hints(monkeypatch):
    monkeypatch.setattr(interop, "detect", lambda platforms=None: _fake_detect_results())
    result = runner.invoke(cli.app, ["interop", "status"])
    assert result.exit_code == 0, result.output
    for expected in ("OpenCode", "Codex", "Hermes", "project", "manual", "machina.mcp_server"):
        assert expected in result.output
    # Installed-but-unregistered harnesses get an actionable hint.
    assert "interop enable codex" in result.output


def test_cli_status_survives_detection_failure(monkeypatch):
    def boom(platforms=None):
        raise RuntimeError("pgrep exploded")

    monkeypatch.setattr(interop, "detect", boom)
    result = runner.invoke(cli.app, ["interop", "status"])
    assert result.exit_code == 1
    assert "pgrep exploded" in result.output


def test_cli_enable_unknown_platform_exits_2():
    result = runner.invoke(cli.app, ["interop", "enable", "not-a-platform"])
    assert result.exit_code == 2
    assert "Unknown platform" in result.output


def test_cli_enable_unknown_scope_exits_2():
    result = runner.invoke(cli.app, ["interop", "enable", "codex", "--scope", "session"])
    assert result.exit_code == 2
    assert "Invalid --scope" in result.output


def test_cli_enable_success_exits_0_and_prints_verification(monkeypatch):
    def fake_enable(platform, *, scope="project", dry_run=False):
        return interop.ActionResult(
            platform=platform,
            action="enable",
            ok=True,
            changed=True,
            message="registered `machina` for Codex (global scope) and verified it.",
            steps=["run: codex mcp add machina -- ..."],
            verify={"ok": True, "detail": "present in `codex mcp list --json` (enabled)"},
        )

    monkeypatch.setattr(interop, "enable", fake_enable)
    result = runner.invoke(cli.app, ["interop", "enable", "codex"])
    assert result.exit_code == 0, result.output
    assert "registered" in result.output and "verify" in result.output


def test_cli_enable_dry_run_exits_0_without_spawning(monkeypatch):
    calls: list = []
    monkeypatch.setattr(interop, "_which", lambda spec: "/fake/bin/codex")
    monkeypatch.setattr(interop, "_run", lambda *a, **k: calls.append(a) or _result())

    result = runner.invoke(cli.app, ["interop", "enable", "codex", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "dry run" in result.output
    assert calls == []


def test_cli_enable_failure_exits_1(monkeypatch):
    monkeypatch.setattr(
        interop,
        "enable",
        lambda platform, *, scope="project", dry_run=False: interop.ActionResult(
            platform=platform,
            action="enable",
            ok=False,
            changed=True,
            message="registration did not verify",
            verify={"ok": False, "detail": "not present"},
        ),
    )
    result = runner.invoke(cli.app, ["interop", "enable", "codex"])
    assert result.exit_code == 1
    assert "did not verify" in result.output


def test_cli_disable_manual_exits_1(monkeypatch):
    monkeypatch.setattr(
        interop,
        "disable",
        lambda platform: interop.ActionResult(
            platform=platform,
            action="disable",
            ok=False,
            changed=False,
            message="OpenCode's CLI has no MCP remove subcommand; delete it manually from: opencode.json",
        ),
    )
    result = runner.invoke(cli.app, ["interop", "disable", "opencode"])
    assert result.exit_code == 1
    assert "no MCP remove subcommand" in result.output
