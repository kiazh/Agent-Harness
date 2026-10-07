"""Terminal tool — execute shell commands securely."""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import subprocess
import threading
from pathlib import Path

from ah.core.config import config
from ah.core.exceptions import ToolError, ValidationError
from ah.tools.base import registry

logger = logging.getLogger(__name__)
MAX_OUTPUT_CHARS = 64_000

# Allowlist of safe commands — intentionally narrow
# Removed: python, pip, npm, node, curl, wget, rm, cp, mv (command injection risk)
ALLOWED_COMMANDS = frozenset(
    {
        "git",
        "ls",
        "cat",
        "grep",
        "find",
        "pytest",
        "echo",
        "pwd",
        "mkdir",
        "touch",
        "head",
        "tail",
        "wc",
        "diff",
    }
)

# Characters that could be used for command injection
DANGEROUS_CHARS = frozenset(";|&$()`<>\\\n")

# A command may only see the configured workspace, never the whole home
# directory or a system temporary directory that may contain credentials.
ALLOWED_WORKDIR_PREFIXES = (str(Path(config.get("agent_harness_home") or Path.cwd()).resolve()),)

# Argument blocklist: git aliases, find -exec, and test-runner overrides can
# escape the command allowlist (e.g. `find . -exec`, `git -c`, `pytest -o`).
_BLOCKED_ARGS = frozenset({"-exec", "-execdir", "-delete", "-c", "-C", "--config", "-o"})


def _allowed_workdir_prefixes() -> tuple[str, ...]:
    """Refresh allowed prefixes from config (not frozen at import)."""
    # Respect monkeypatched ALLOWED_WORKDIR_PREFIXES in tests.
    import ah.tools.terminal as _self

    try:
        explicit = getattr(_self, "ALLOWED_WORKDIR_PREFIXES", None)
        # If tests override it to a tmp dir different from config, honour it.
        # Detect override by comparing to config-derived default.
        try:
            home = config.get("agent_harness_home") or Path.cwd()
        except Exception:
            home = Path.cwd()
        default = str(Path(home).resolve())
        if explicit and tuple(explicit) != (default,):
            return tuple(explicit)
    except Exception:
        pass
    try:
        home = config.get("agent_harness_home") or Path.cwd()
    except Exception:
        home = Path.cwd()
    return (str(Path(home).resolve()),)


def _validate_blocked_args(args: list[str]) -> None:
    for arg in args[1:]:
        if arg in _BLOCKED_ARGS or arg.startswith("--config="):
            raise ValidationError(f"argument '{arg}' is not allowed")


def _validate_workdir(workdir: str) -> None:
    """Validate that workdir is within allowed paths. Raises ValidationError if not."""
    resolved = Path(workdir or ".").resolve()
    for prefix in _allowed_workdir_prefixes():
        if prefix and resolved.is_relative_to(Path(prefix).resolve()):
            return
    raise ValidationError(f"workdir '{workdir}' is not within allowed paths")


def _run_bounded(args: list[str], timeout: int, cwd: str | None) -> str:
    """Read at most one character past the limit before stopping the child."""
    with subprocess.Popen(
        args,
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=cwd,
    ) as process:
        expired = threading.Event()

        def stop_on_timeout() -> None:
            if process.poll() is None:
                expired.set()
                try:
                    process.kill()
                except OSError:
                    pass  # The child may have exited after poll().

        timer = threading.Timer(timeout, stop_on_timeout)
        timer.daemon = True
        timer.start()
        try:
            if process.stdout is None:
                raise RuntimeError("process.stdout is None")
            # Read output in a separate thread to avoid deadlock on large output
            # (blocking read() may not return promptly after kill on Windows)
            output_parts = []
            total_read = 0
            truncated = False

            def read_output():
                nonlocal total_read, truncated
                while total_read <= MAX_OUTPUT_CHARS:
                    chunk = process.stdout.read(MAX_OUTPUT_CHARS + 1 - total_read)
                    if not chunk:
                        break
                    output_parts.append(chunk)
                    total_read += len(chunk)
                    if total_read > MAX_OUTPUT_CHARS:
                        truncated = True
                        break

            reader_thread = threading.Thread(target=read_output, daemon=True)
            reader_thread.start()
            reader_thread.join(timeout=timeout + 1)  # Give extra time for read to complete

            if truncated and process.poll() is None:
                process.kill()
            process.wait()
        finally:
            timer.cancel()

        if expired.is_set():
            raise subprocess.TimeoutExpired(args, timeout)
        output = "".join(output_parts)
        if truncated:
            return output[:MAX_OUTPUT_CHARS] + "\n[output truncated]"
        return output or f"(exit code {process.returncode}, no output)"


@registry.register(
    name="terminal",
    description="Execute a shell command and return stdout/stderr. Use for git, builds, tests, etc.",
    parameters={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to execute"},
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds (default 60, max 300)",
            },
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
        raise ValidationError(f"Failed to parse command: {e}") from e

    if not args:
        raise ValidationError("Empty command")

    # Check allowlist. In full mode (explicit session grant) any on-PATH
    # binary is a *valid* operation — approval already authorized it via the
    # broker; validation only rejects unknown binaries. Other modes keep the
    # narrow allowlist.
    base_cmd = os.path.basename(args[0])
    try:
        from ah.core.config import config as _cfg

        _mode = (_cfg.get("execution_mode") or "ask").lower()
    except Exception:
        _mode = "ask"
    if _mode == "full":
        import shutil as _shutil

        if _shutil.which(args[0]) is None and _shutil.which(base_cmd) is None:
            raise ValidationError(f"Command '{base_cmd}' not found on PATH")
    elif base_cmd not in ALLOWED_COMMANDS:
        raise ValidationError(f"Command '{base_cmd}' is not in the allowlist")

    # Tighten allowlist: block escape hatches even for allowed commands.
    _validate_blocked_args(args)

    # Validate workdir (refresh config, not frozen) and re-resolve at exec time.
    _validate_workdir(workdir)
    resolved_workdir = str(Path(workdir or ".").resolve())
    _validate_workdir(resolved_workdir)

    # Local subprocesses are not a security boundary: git aliases, find -exec,
    # and test runners can execute commands outside the command allowlist.
    # Phase D: the permission broker (agent loop) already authorized this
    # action; this layer validates backend availability. Host execution is
    # available in ask/workspace/full modes once approved — Docker sandbox
    # use stays independent of permission policy.
    sandbox = os.environ.get("AGENT_HARNESS_TERMINAL_SANDBOX", "disabled").lower()
    try:
        from ah.core.config import config as _cfg2

        cfg_sandbox = (_cfg2.get("terminal_sandbox") or "").lower()
        if cfg_sandbox in {"disabled", "local", "docker"}:
            # Config wins when explicitly set; env stays as override for ops.
            if os.environ.get("AGENT_HARNESS_TERMINAL_SANDBOX") is None:
                sandbox = cfg_sandbox
    except Exception:
        pass
    if sandbox not in {"disabled", "local", "docker"}:
        raise ValidationError("terminal sandbox must be 'disabled', 'local', or 'docker'")
    # "disabled" previously hard-rejected everything. With the broker in place,
    # host execution proceeds once approved (mode ask/workspace/full); only
    # sandbox mode forces container isolation.
    try:
        from ah.core.config import config as _cfg3

        _mode3 = (_cfg3.get("execution_mode") or "ask").lower()
    except Exception:
        _mode3 = "ask"
    if sandbox == "disabled" and _mode3 == "sandbox":
        raise ValidationError("sandbox mode requires the Docker backend; Docker is not configured")
    use_docker = sandbox == "docker" or _mode3 == "sandbox"
    if use_docker and sandbox != "docker" and _mode3 == "sandbox":
        raise ValidationError("sandbox mode requires the Docker backend; Docker is not configured")
    if use_docker:
        workspace = resolved_workdir
        image = os.environ.get("AGENT_HARNESS_TERMINAL_IMAGE", "agent-harness-tool-sandbox:latest")
        # Read-write project mounts when the approved mode allows writes
        # (workspace/full); ask keeps read-only. The container root fs stays
        # read-only; /tmp + /workspace (when rw) are the writable locations.
        try:
            from ah.core.config import config as _cfgm

            _mm = (_cfgm.get("execution_mode") or "ask").lower()
        except Exception:
            _mm = "ask"
        _mount_ro = "" if _mm in ("workspace", "full") else ",readonly"
        args = [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "64",
            "--memory",
            "256m",
            "--cpus",
            "1",
            "--user",
            "65534:65534",
            "--tmpfs",
            "/tmp:rw,nosuid,size=64m",
            "--mount",
            f"type=bind,src={workspace},dst=/workspace{_mount_ro}",
            "--workdir",
            "/workspace",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "--env",
            "PYTEST_ADDOPTS=-p no:cacheprovider",
            image,
            *args,
        ]

    # Execute off the event loop. Stop reading and kill prolific children before
    # their output can exhaust gateway memory. Re-resolve workdir just before
    # exec to close TOCTOU swaps (MAX_OUTPUT_CHARS caps output at 64 KiB).
    try:
        exec_cwd = resolved_workdir if workdir != "." else None
        _validate_workdir(resolved_workdir)
        return await asyncio.to_thread(_run_bounded, args, timeout, exec_cwd)
    except subprocess.TimeoutExpired:
        raise ToolError(f"Command timed out after {timeout}s") from None
    except FileNotFoundError:
        raise ToolError(f"Command not found: {args[0]}") from None
    except Exception as e:
        raise ToolError(f"Error executing command: {e}") from e
