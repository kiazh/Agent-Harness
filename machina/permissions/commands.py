"""Classify routine project commands; unknown execution still needs review.

Build/test/lint workflows execute trusted project code. This is an operator
trust decision, not a claim that host execution is isolated or read-only.
"""

from __future__ import annotations

import re
from pathlib import Path

_PROJECT_COMMANDS = frozenset(
    {
        "ls",
        "cat",
        "grep",
        "rg",
        "find",
        "echo",
        "pwd",
        "head",
        "tail",
        "wc",
        "diff",
        "mkdir",
        "touch",
        "cp",
        "pytest",
        "ruff",
        "mypy",
        "tsc",
        "vitest",
        "whoami",
        "uname",
        "date",
        "uptime",
    }
)
_GIT_COMMANDS = frozenset(
    {
        "status",
        "diff",
        "log",
        "show",
        "ls-files",
        "ls-tree",
        "rev-parse",
        "rev-list",
        "describe",
        "blame",
        "shortlog",
        "grep",
        "add",
        "commit",
    }
)
_WORKFLOWS = frozenset({"test", "build", "lint", "typecheck", "check", "format"})
_ESCAPE_ARGS = frozenset(
    {
        "-exec",
        "-execdir",
        "-ok",
        "-okdir",
        "-delete",
        # GNU find output-writing primaries consume a file operand and write
        # attacker-directed content; they require file-write review.
        "-fprint",
        "-fprint0",
        "-fprintf",
        "-fls",
        "-c",
        "-C",
        "--config",
        "-o",
        "--output",
        "--output-file",
        "--pre",
        "--ext-diff",
        "--textconv",
        "--no-index",
        "--upload-pack",
        "--exec-path",
        "--git-dir",
        "--work-tree",
    }
)
# R4-061: bare `-c`/`-C`/`-o` are format/output selectors for the read-only
# commands below — `wc -c`, `ls -C`, `ls -o`, `grep -C 2`, `head -c N`,
# `diff -c` — and must stay routine. For every other command, and for every
# ATTACHED spelling (`-cVALUE`, `-CVALUE`, `-oVALUE`; guarded below), the
# same letters redirect behavior (git's global -c/-C config/chdir, python -c
# code evaluation, find's boolean -o) and stay review.
_CALIBRATED_BARE_FLAGS = {
    "ls": frozenset({"-C", "-o"}),
    "wc": frozenset({"-c"}),
    "grep": frozenset({"-C"}),
    "head": frozenset({"-c"}),
    "diff": frozenset({"-c"}),
}


def executable_name(value: str) -> str:
    return value.replace("\\", "/").rsplit("/", 1)[-1].lower().removesuffix(".exe")


def command_path_values(argv: list[str]) -> list[str]:
    """Include common attached file-option operands in path inspection."""
    if not argv:
        return []
    file_options = {
        "cp": ("-t",),
        "touch": ("-r",),
        "grep": ("-f",),
        "rg": ("-f",),
        "git": ("-F",),
    }.get(executable_name(argv[0]), ())
    values = []
    for arg in argv[1:]:
        if arg.startswith("--") and "=" in arg:
            values.append(arg.split("=", 1)[1])
        elif arg.startswith("-") and not arg.startswith("--"):
            for option in file_options:
                match = re.fullmatch(r"-[A-Za-z]*?" + re.escape(option[1:]) + r"(.+)", arg)
                if match:
                    values.append(match[1])
                    break
        elif not arg.startswith("-"):
            values.append(arg)
    return values


def routine_project_command(argv: list[str], cwd: str, root: Path) -> bool:
    if not argv or not Path(cwd).resolve().is_relative_to(root):
        return False
    # Absolute/relative executable paths could be a substituted project script.
    # Common virtualenv bin paths are still opaque until explicitly approved.
    if "/" in argv[0] or "\\" in argv[0]:
        return False
    calibrated = _CALIBRATED_BARE_FLAGS.get(executable_name(argv[0]), frozenset())
    if any(arg.split("=", 1)[0] in _ESCAPE_ARGS and arg not in calibrated for arg in argv[1:]):
        return False
    if any(
        arg.startswith(("-o", "-c", "-C")) and not arg.startswith("--") and len(arg) > 2
        for arg in argv[1:]
    ):
        return False
    # Unrecognized compact options containing paths/configuration are not
    # ordinary boolean flags; require review rather than ignoring operands.
    if any(
        arg.startswith("-") and not arg.startswith("--") and not re.fullmatch(r"-[A-Za-z0-9]+", arg)
        for arg in argv[1:]
    ):
        return False
    # Arguments that clearly name outside paths must not inherit project
    # authority. Unknown grammars never reach this path.
    for value in command_path_values(argv):
        if value.startswith("~") or "://" in value:
            return False
        if not value.startswith("-"):
            path = Path(value.replace("\\", "/"))
            path = path if path.is_absolute() else Path(cwd) / path
            if not path.resolve().is_relative_to(root):
                return False
    command, args = executable_name(argv[0]), argv[1:]
    if command == "git":
        if not args:
            return False
        return args[0] in _GIT_COMMANDS and not any(
            arg in {"--amend", "--force", "-f"} for arg in args[1:]
        )
    if command in {"npm", "pnpm", "yarn"}:
        if args and args[0] == "run":
            args = args[1:]
        return bool(args and args[0] in _WORKFLOWS)
    if command in {"python", "python3"}:
        return len(args) >= 2 and args[0] == "-m" and args[1] in {"pytest", "ruff", "mypy"}
    if command == "uv":
        return bool(args and args[0] == "run" and routine_project_command(args[1:], cwd, root))
    return command in _PROJECT_COMMANDS


def routine_shell_command(payload: str, cwd: str, root: Path) -> bool:
    from machina.permissions.shell import simple_shell_commands

    stages = simple_shell_commands(payload)
    return bool(stages and all(routine_project_command(stage, cwd, root) for stage in stages))
