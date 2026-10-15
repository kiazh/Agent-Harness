"""H1 harnesses core: FEATURES entry, run interface, CLI rename/alias, status block.

RED-first companion to renaming ``machina interop`` to ``machina harnesses``. It
pins the frozen ``machina.interop`` run interface (``RunResult``,
``RUN_PLATFORMS``, ``supported_runs``, ``run_task``), the CLI group rename with
the kept ``interop`` alias, the new ``harnesses run`` surface, and the
spawn-free ``harnesses`` block in ``harness_status`` (so an agent can answer
"are you connected to opencode?" truthfully). No test here spawns a real
harness CLI.
"""

from __future__ import annotations

import dataclasses
import json
import os
import uuid
from unittest.mock import AsyncMock

import pytest
from typer.testing import CliRunner

import machina.cli as cli
import machina.interop as interop
from machina.core.models import Session

runner = CliRunner()


# ─── self_knowledge: the always-on brief must know the feature exists ───────


def test_features_has_harnesses_entry_covering_the_live_surface():
    from machina.core.self_knowledge import FEATURES

    assert "harnesses" in FEATURES, "the brief denied this feature existed"
    entry = FEATURES["harnesses"]
    for token in (
        "OpenCode",
        "Codex",
        "Hermes",
        "machina harnesses status",
        "enable",
        "disable",
        "setup",
        "run",
        "interop",
        "~/.machina/interop.json",
        "/harnesses",
        "auth files",
    ):
        assert token in entry, f"harnesses brief missing {token!r}"


def test_capability_context_includes_harnesses_and_no_longer_contradicts():
    from machina.core.self_knowledge import capability_context

    session = Session(id=uuid.uuid4())
    context = capability_context(session, tool_names=set(), growth_state="skipped")
    assert "harnesses:" in context
    # The plain-language mappings the agent must not deny.
    assert "machina harnesses enable opencode" in context
    assert "machina harnesses run" in context


# ─── frozen run interface ───────────────────────────────────────────────────


def test_run_interface_shape_is_frozen():
    fields = {f.name for f in dataclasses.fields(interop.RunResult)}
    assert fields == {
        "platform",
        "command",
        "exit_code",
        "stdout",
        "stderr",
        "ok",
        "unsupported_reason",
    }
    assert interop.RunResult.__dataclass_fields__["unsupported_reason"].default == ""
    assert isinstance(interop.RUN_PLATFORMS, dict)
    assert set(interop.RUN_PLATFORMS) == set(interop.PLATFORMS)
    descriptions = interop.supported_runs()
    assert set(descriptions) == set(interop.RUN_PLATFORMS)
    assert all(isinstance(value, str) and value for value in descriptions.values())


def _proc(output: str = "", rc: int = 0, stderr: str = "", timed_out: bool = False):
    return interop._ProcessResult(
        returncode=rc, output=output, stderr=stderr, timed_out=timed_out, truncated=False
    )


def test_run_task_unknown_platform_is_typed_and_never_spawns(monkeypatch):
    monkeypatch.setattr(
        interop,
        "_run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no spawn for unknown platform")),
    )
    result = interop.run_task("nope", "do a thing")

    assert result.ok is False and result.exit_code is None
    assert result.unsupported_reason and "nope" in result.unsupported_reason
    assert result.command == [] and result.stdout == ""


def test_run_task_known_but_unsupported_platform_is_typed(monkeypatch):
    # Forward-proofs the spec's "unsupported platform returns unsupported_reason"
    # branch even though every shipped platform is currently run-capable.
    monkeypatch.setattr(interop, "RUN_PLATFORMS", {"opencode": ["opencode", "run"]})
    monkeypatch.setattr(
        interop,
        "_run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no spawn when unsupported")),
    )

    result = interop.run_task("codex", "x")

    assert result.ok is False and result.exit_code is None
    assert result.unsupported_reason and "headless" in result.unsupported_reason


def test_run_task_builds_the_documented_headless_command_and_is_typed(monkeypatch):
    seen: dict = {}

    def fake_run(argv, *, env=None, timeout=0.0, stdin_text=None, cwd=None):
        seen["argv"] = list(argv)
        seen["timeout"] = timeout
        seen["env"] = env
        seen["stdin_text"] = stdin_text
        return _proc(output="\x1b[32mdelegated ok\x1b[0m\n")

    monkeypatch.setattr(interop, "_which", lambda spec: f"/fake/bin/{spec.key}")
    monkeypatch.setattr(interop, "_run", fake_run)

    result = interop.run_task("opencode", "fix the build", timeout=12.5)

    assert result.platform == "opencode"
    # H09: the `--` option terminator keeps the prompt out of flag position
    # (all of it becomes `run`'s variadic message).
    assert result.command == ["/fake/bin/opencode", "run", "--", "fix the build"]
    assert result.ok and result.exit_code == 0
    assert result.stdout == "delegated ok", "ANSI must be stripped"
    assert seen["timeout"] == 12.5
    assert seen["env"]["NO_COLOR"] == "1", "minimal child env must be used"
    assert seen["stdin_text"] is None, "opencode gets the prompt in argv, not stdin"


def test_run_task_codex_prompt_goes_on_stdin_and_never_touches_argv(monkeypatch):
    seen: dict = {}

    def fake_run(argv, *, env=None, timeout=0.0, stdin_text=None, cwd=None):
        seen["argv"] = list(argv)
        seen["stdin_text"] = stdin_text
        return _proc(output="delegated ok\n")

    monkeypatch.setattr(interop, "_which", lambda spec: f"/fake/bin/{spec.key}")
    monkeypatch.setattr(interop, "_run", fake_run)

    result = interop.run_task("codex", "--help")

    # H09: `codex exec -` is the documented stdin form; the prompt cannot be
    # read as an option because it is never an argv token.
    assert result.command == ["/fake/bin/codex", "exec", "-"]
    assert seen["stdin_text"] == "--help"
    assert "--help" not in result.command
    assert result.ok


def test_run_task_redacts_secrets_and_reports_a_failed_run(monkeypatch):
    secret = "ghp_" + "a" * 36
    monkeypatch.setattr(interop, "_which", lambda spec: f"/fake/bin/{spec.key}")
    monkeypatch.setattr(
        interop,
        "_run",
        lambda *a, **k: _proc(rc=1, stderr=f"failed: Authorization: Bearer {secret}\n"),
    )

    result = interop.run_task("codex", "x")

    assert result.ok is False and result.exit_code == 1
    assert secret not in result.stderr and "[REDACTED" in result.stderr


def test_run_task_timeout_is_a_typed_failure(monkeypatch):
    monkeypatch.setattr(interop, "_which", lambda spec: f"/fake/bin/{spec.key}")
    monkeypatch.setattr(interop, "_run", lambda *a, **k: _proc(rc=-9, timed_out=True))

    result = interop.run_task("codex", "x", timeout=1.0)

    assert result.ok is False and result.exit_code == -9
    assert "timed out" in result.stderr


def test_run_task_not_installed_is_typed_and_never_spawns(monkeypatch):
    monkeypatch.setattr(interop, "_which", lambda spec: None)
    monkeypatch.setattr(
        interop,
        "_run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no spawn when not installed")),
    )

    result = interop.run_task("hermes", "x")

    assert result.ok is False and result.exit_code is None
    assert "not installed" in result.stderr
    assert result.command == ["hermes", "--oneshot=x"], "hermes prompt is an option value"


# H09: option-like prompts must reach the harness literally -------------------
#
# The stubs are shell executables (run for real, under a PATH pointing at
# them) that record their argv and emulate the installed CLIs' parsing:
#   * opencode v2.0.26 — `--` ends flag parsing; trailing operands are
#     appended to `run`'s variadic message. An option-like token before `--`
#     is a flag (`--help` prints usage, exit 0).
#   * codex-cli 0.162.1 — `exec [OPTIONS] [PROMPT]`; `-` reads the prompt
#     from stdin; `--help` prints usage, exit 0.
#   * Hermes v0.21.6 — argparse `-z/--oneshot PROMPT`; an option-like value
#     after the split flag is refused (`expected one argument`, exit 2);
#     `--oneshot=<value>` binds the value literally.

_STUB_HARNESS_SCRIPTS = {
    # Emulates: `opencode run [flags] [<message...>]` with `--` splitting.
    "opencode": """#!/bin/sh
for a in "$@"; do printf 'ARGV: %s\\n' "$a"; done
parse=1
prompt=""
for a in "$@"; do
  if [ "$parse" = 1 ] && [ "$a" = "--" ]; then parse=0; continue; fi
  if [ "$parse" = 1 ] && [ "$a" = "run" ]; then continue; fi
  if [ "$parse" = 1 ]; then
    case "$a" in
      --help|-h) printf 'OPENCODE HELP (usage text, exit 0)\\n'; exit 0 ;;
      -*) printf 'unknown option %s\\n' "$a" >&2; exit 1 ;;
    esac
  fi
  prompt="$a"
done
printf 'TASK: %s\\n' "$prompt"
""",
    # Emulates: `codex exec [OPTIONS] [PROMPT]`, `-` = prompt on stdin.
    "codex": """#!/bin/sh
for a in "$@"; do printf 'ARGV: %s\\n' "$a"; done
prompt=""
stdin_prompt=0
for a in "$@"; do
  case "$a" in
    exec) continue ;;
    -) stdin_prompt=1; continue ;;
    --help|-h) printf 'CODEX HELP (usage text, exit 0)\\n'; exit 0 ;;
    -*) printf 'error: unexpected argument %s\\n' "$a" >&2; exit 2 ;;
  esac
  prompt="$a"
done
if [ "$stdin_prompt" = 1 ]; then
  IFS= read -r prompt
  printf 'STDIN: %s\\n' "$prompt"
fi
printf 'TASK: %s\\n' "$prompt"
""",
    # Emulates: argparse `-z/--oneshot PROMPT` binding rules.
    "hermes": """#!/bin/sh
for a in "$@"; do printf 'ARGV: %s\\n' "$a"; done
for a in "$@"; do
  case "$a" in
    --oneshot=*) printf 'TASK: %s\\n' "${a#--oneshot=}"; exit 0 ;;
  esac
done
if [ "$1" = "-z" ] || [ "$1" = "--oneshot" ]; then
  case "${2:-}" in
    ""|-*) printf 'usage: hermes: error: argument -z/--oneshot: expected one argument\\n' >&2; exit 2 ;;
    *) printf 'TASK: %s\\n' "$2"; exit 0 ;;
  esac
fi
printf 'no oneshot flag\\n' >&2
exit 2
""",
}


def _install_stub_harnesses(monkeypatch, tmp_path) -> dict[str, str]:
    """Put one executable stub per platform first on PATH; return their paths."""
    stub_dir = tmp_path / "h09-stubs"
    stub_dir.mkdir()
    resolved: dict[str, str] = {}
    for name, body in _STUB_HARNESS_SCRIPTS.items():
        path = stub_dir / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
        resolved[name] = str(path)
    monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return resolved


def test_run_task_delivers_option_like_prompts_literally(monkeypatch, tmp_path):
    """A `--help` prompt reaches each harness as the task, not as a flag.

    RED before the fix: opencode/codex return usage text with ``ok=True``
    without running a task; hermes fails with a argparse usage error (exit 2).
    """
    stub = _install_stub_harnesses(monkeypatch, tmp_path)

    opencode = interop.run_task("opencode", "--help", timeout=10.0)
    assert opencode.command == [stub["opencode"], "run", "--", "--help"]
    assert opencode.ok and opencode.exit_code == 0
    assert opencode.stdout.splitlines() == [
        "ARGV: run",
        "ARGV: --",
        "ARGV: --help",
        "TASK: --help",
    ]

    codex = interop.run_task("codex", "--help", timeout=10.0)
    assert codex.command == [stub["codex"], "exec", "-"]
    assert codex.ok and codex.exit_code == 0
    assert codex.stdout.splitlines() == [
        "ARGV: exec",
        "ARGV: -",
        "STDIN: --help",
        "TASK: --help",
    ]

    hermes = interop.run_task("hermes", "--help", timeout=10.0)
    assert hermes.command == [stub["hermes"], "--oneshot=--help"]
    assert hermes.ok and hermes.exit_code == 0
    assert hermes.stdout.splitlines() == [
        "ARGV: --oneshot=--help",
        "TASK: --help",
    ]


def test_run_task_refuses_a_platform_without_a_literal_prompt_mechanism(monkeypatch):
    """A run-capable platform with no literal delivery is a typed refusal.

    Forward-proofs the H09 guard: a future platform added to RUN_PLATFORMS
    without an option-proof prompt mechanism must never be spawned with an
    option-interpretable prompt.
    """
    monkeypatch.setitem(
        interop.PLATFORMS,
        "future",
        interop.PlatformSpec(
            key="future",
            label="Future",
            binaries=("future",),
            version_args=(),
            list_args=(),
            supports_scopes=(),
            remove_args=None,
        ),
    )
    monkeypatch.setitem(interop.RUN_PLATFORMS, "future", ["future"])
    monkeypatch.setattr(
        interop,
        "_run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no spawn without a mechanism")),
    )

    result = interop.run_task("future", "--help")

    assert result.ok is False and result.exit_code is None
    assert "literal prompt" in result.unsupported_reason
    assert result.command == []


# ─── CLI rename + kept alias + `harnesses run` ──────────────────────────────


def test_cli_primary_group_is_harnesses_and_interop_still_works(monkeypatch):
    monkeypatch.setattr(interop, "detect", lambda platforms=None: {})

    primary = runner.invoke(cli.app, ["harnesses", "status"])
    alias = runner.invoke(cli.app, ["interop", "status"])

    assert primary.exit_code == 0, primary.output
    assert alias.exit_code == 0, alias.output


def test_cli_harnesses_help_lists_all_subcommands():
    result = runner.invoke(cli.app, ["harnesses", "--help"])

    assert result.exit_code == 0, result.output
    flat = " ".join(result.output.split())
    for name in ("status", "enable", "disable", "setup", "run"):
        assert name in flat, f"missing {name}"


def test_cli_harnesses_run_exit_codes(monkeypatch):
    ok = interop.RunResult(
        platform="opencode",
        command=["opencode", "run", "hi"],
        exit_code=0,
        stdout="hello from the harness",
        stderr="",
        ok=True,
    )
    monkeypatch.setattr(interop, "run_task", lambda platform, prompt, *, timeout=300.0: ok)
    result = runner.invoke(cli.app, ["harnesses", "run", "opencode", "hi"])
    assert result.exit_code == 0, result.output
    assert "hello from the harness" in result.output

    failed = interop.RunResult(
        platform="codex",
        command=["codex", "exec", "x"],
        exit_code=1,
        stdout="",
        stderr="boom",
        ok=False,
    )
    monkeypatch.setattr(interop, "run_task", lambda *a, **k: failed)
    result = runner.invoke(cli.app, ["harnesses", "run", "codex", "x"])
    assert result.exit_code == 1, result.output
    assert "boom" in result.output

    unsupported = interop.RunResult(
        platform="mystery",
        command=[],
        exit_code=None,
        stdout="",
        stderr="",
        ok=False,
        unsupported_reason="unknown platform 'mystery'",
    )
    monkeypatch.setattr(interop, "run_task", lambda *a, **k: unsupported)
    result = runner.invoke(cli.app, ["harnesses", "run", "mystery", "x"])
    assert result.exit_code == 2, result.output
    assert "unknown platform" in result.output


# ─── harness_status: spawn-free harnesses block ─────────────────────────────


@pytest.mark.asyncio
async def test_harness_status_includes_spawn_free_harnesses_block(monkeypatch, tmp_path):
    from machina.tools import harness as harness_tool

    state_file = tmp_path / "interop.json"
    monkeypatch.setenv(interop.STATE_ENV_OVERRIDE, str(state_file))
    state_file.write_text(
        json.dumps(
            {
                "opencode": {
                    "enabled": True,
                    "scope": "project",
                    "registered_at": "2026-10-15T00:00:00+00:00",
                    "verify": {"ok": True, "detail": "listed (connected)"},
                    "binary": "/x/opencode",
                    "version": "opencode v2.0.26",
                },
                # A verified *removal* also stores verify.ok true (absence
                # confirmed): it must never read back as "registered".
                "codex": {
                    "enabled": False,
                    "scope": "global",
                    "registered_at": "2026-10-15T00:00:00+00:00",
                    "verify": {"ok": True, "detail": "absent from `mcp list`"},
                    "binary": "/x/codex",
                    "version": "codex-cli 0.162.1",
                },
            }
        ),
        encoding="utf-8",
    )
    # Any probe spawn would be a bug: the block is state + PATH lookup only.
    monkeypatch.setattr(
        interop,
        "_run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("harness_status must not spawn")),
    )
    monkeypatch.setattr(interop, "_which", lambda spec: f"/fake/bin/{spec.key}")

    sid = uuid.uuid4()
    owner = "harnesses-status-owner"
    monkeypatch.setattr(harness_tool, "active_agent_scope", AsyncMock(return_value=(owner, sid)))
    monkeypatch.setattr(
        harness_tool.session_manager,
        "get",
        AsyncMock(return_value=Session(id=sid, agent_id=owner)),
    )
    monkeypatch.setattr(
        harness_tool.growth_store,
        "get",
        AsyncMock(return_value={"notes": {}, "interactions": 0, "feedback_count": 0}),
    )
    monkeypatch.setattr(harness_tool.growth_store, "rsi_status", AsyncMock(return_value={}))

    payload = json.loads(await harness_tool.harness_status())

    block = payload["harnesses"]
    assert block["state_file"] == str(state_file)
    assert block["platforms"]["opencode"] == {
        "label": "OpenCode",
        "installed": True,
        "enabled": True,
        "scope": "project",
        "registered": True,
    }
    assert block["platforms"]["codex"]["enabled"] is False
    assert block["platforms"]["codex"]["registered"] is False, (
        "a verified removal must not read back as registered"
    )
    assert block["platforms"]["hermes"]["enabled"] is None
    assert block["platforms"]["hermes"]["registered"] is False
