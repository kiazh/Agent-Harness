"""Terminal tool — execute shell commands securely."""
from __future__ import annotations

import logging
import os
import shlex
import subprocess
from pathlib import Path

from ah.tools.base import registry

logger = logging.getLogger(__name__)

# Allowlist of safe commands
ALLOWED_COMMANDS = frozenset({
    "git", "ls", "cat", "grep", "find", "pytest", "python", "pip",
    "npm", "node", "echo", "pwd", "cd", "mkdir", "cp", "mv", "rm",
    "touch", "head", "tail", "wc", "diff", "curl", "wget",
})

# Characters that could be used for command injection
DANGEROUS_CHARS = frozenset(";|&$()`<>\\\n")

# Allowed base directories for workdir
ALLOWED_WORKDIR_PREFIXES = (
    str(Path.home()),
    "/tmp",
    os.environ.get("TMPDIR", ""),
    os.environ.get("TEMP", ""),
)


def _validate_workdir(workdir: str) -> str | None:
    """Validate that workdir is within allowed paths. Returns error message or None."""
    if not workdir or workdir == ".":
        return None
    resolved = Path(workdir).resolve()
    for prefix in ALLOWED_WORKDIR_PREFIXES:
        if prefix and str(resolved).startswith(str(Path(prefix).resolve())):
            return None
    return f"Error: workdir '{workdir}' is not within allowed paths"


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
def terminal(command: str, timeout: int = 60, workdir: str = ".") -> str:
    """Execute a shell command securely (no shell injection)."""
    # Validate timeout
    if not isinstance(timeout, int) or timeout < 1 or timeout > 300:
        return f"Error: timeout must be an integer between 1 and 300, got {timeout}"

    # Reject dangerous characters
    found_dangerous = DANGEROUS_CHARS.intersection(command)
    if found_dangerous:
        chars = ", ".join(repr(c) for c in sorted(found_dangerous))
        return f"Error: Command contains dangerous characters: {chars}"

    # Parse command with shlex (no shell)
    try:
        args = shlex.split(command)
    except ValueError as e:
        return f"Error: Failed to parse command: {e}"

    if not args:
        return "Error: Empty command"

    # Check allowlist
    base_cmd = os.path.basename(args[0])
    if base_cmd not in ALLOWED_COMMANDS:
        return f"Error: Command '{base_cmd}' is not in the allowlist"

    # Validate workdir
    workdir_error = _validate_workdir(workdir)
    if workdir_error:
        return workdir_error

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
        return f"Error: Command timed out after {timeout}s"
    except FileNotFoundError:
        return f"Error: Command not found: {args[0]}"
    except Exception as e:
        return f"Error executing command: {e}"
