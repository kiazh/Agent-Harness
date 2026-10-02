"""Terminal tool — execute shell commands securely."""
from __future__ import annotations

import asyncio
import logging
import os
import shlex
import subprocess
from pathlib import Path

from ah.core.config import config
from ah.core.exceptions import ToolError, ValidationError
from ah.tools.base import registry

logger = logging.getLogger(__name__)

# Allowlist of safe commands — intentionally narrow
# Removed: python, pip, npm, node, curl, wget, rm, cp, mv (command injection risk)
ALLOWED_COMMANDS = frozenset({
    "git", "ls", "cat", "grep", "find", "pytest",
    "echo", "pwd", "cd", "mkdir", "touch", "head", "tail", "wc", "diff",
})

# Characters that could be used for command injection
DANGEROUS_CHARS = frozenset(";|&$()`<>\\\n")

# Allowed base directories for workdir
ALLOWED_WORKDIR_PREFIXES = (
    str(Path.home()),
    "/tmp",
    config.get("tmpdir") or "",
    config.get("temp") or "",
)


def _validate_workdir(workdir: str) -> None:
    """Validate that workdir is within allowed paths. Raises ValidationError if not."""
    if not workdir or workdir == ".":
        return
    resolved = Path(workdir).resolve()
    for prefix in ALLOWED_WORKDIR_PREFIXES:
        if prefix and str(resolved).startswith(str(Path(prefix).resolve())):
            return
    raise ValidationError(f"workdir '{workdir}' is not within allowed paths")


@registry.register(
    name="terminal",
    description="Execute a shell command and return stdout/stderr. Use for git, builds, tests, etc.",
    parameters={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to execute"},
            "timeout": {"type": "integer", "description": "Timeout in seconds (default 60, max 300)"},
            "workdir": {"type": "string", "description": "Working directory for the command"},
        },
        "required": ["command"],
    },
)
async def terminal(command: str, timeout: int = 60, workdir: str = ".") -> str:
    """Execute a shell command securely (no shell injection)."""
    # Validate timeout
    if not isinstance(timeout, int) or timeout < 1 or timeout > 300:
        raise ValidationError(f"timeout must be an integer between 1 and 300, got {timeout}")

    # Reject dangerous characters
    found_dangerous = DANGEROUS_CHARS.intersection(command)
    if found_dangerous:
        chars = ", ".join(repr(c) for c in sorted(found_dangerous))
        raise ValidationError(f"Command contains dangerous characters: {chars}")

    # Parse command with shlex (no shell)
    try:
        args = shlex.split(command)
    except ValueError as e:
        raise ValidationError(f"Failed to parse command: {e}")

    if not args:
        raise ValidationError("Empty command")

    # Check allowlist
    base_cmd = os.path.basename(args[0])
    if base_cmd not in ALLOWED_COMMANDS:
        raise ValidationError(f"Command '{base_cmd}' is not in the allowlist")

    # Validate workdir
    _validate_workdir(workdir)

    # Execute with shell=False
    try:
        result = subprocess.run(
            args,
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=workdir if workdir != "." else None,
        )
        output = ""
        if result.stdout:
            output += result.stdout
        if result.stderr:
            output += ("\n" if output else "") + result.stderr
        if not output:
            output = f"(exit code {result.returncode}, no output)"
        return output
    except subprocess.TimeoutExpired:
        raise ToolError(f"Command timed out after {timeout}s")
    except FileNotFoundError:
        raise ToolError(f"Command not found: {args[0]}")
    except Exception as e:
        raise ToolError(f"Error executing command: {e}")
