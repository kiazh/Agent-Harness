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


# AH-AUDIT-010: subprocesses must not inherit provider/gateway/database
# secrets. Exact secret names plus sensitive suffixes; additional variables
# only via explicit AGENT_HARNESS_TERMINAL_EXTRA_ENV passthrough.
_SECRET_ENV_EXACT = frozenset(
    {
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "MISTRAL_API_KEY",
        "GROQ_API_KEY",
        "TOGETHER_API_KEY",
        "DEEPSEEK_API_KEY",
        "XAI_API_KEY",
        "COHERE_API_KEY",
        "DATABASE_URL",
        "AGENT_HARNESS_API_KEY",
        "AGENT_HARNESS_GATEWAY_TOKEN",
        "GATEWAY_TOKEN",
        "PROVENANCE_KEY",
        "AGENT_HARNESS_PROVENANCE_KEY",
        "VAULT_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    }
)

_SECRET_ENV_SUFFIXES = ("_API_KEY", "_API_SECRET", "_SECRET_KEY", "_PASSWORD", "_DB_PASSWORD")


def _sanitized_env() -> dict[str, str]:
    """Minimal child environment without secrets (AH-AUDIT-010)."""
    try:
        extra = {
            e.strip().upper()
            for e in os.environ.get("AGENT_HARNESS_TERMINAL_EXTRA_ENV", "").split(",")
            if e.strip()
        }
    except Exception:
        extra = set()
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if upper in extra:
            env[key] = value
            continue
        if upper in _SECRET_ENV_EXACT:
            continue
        if upper.endswith(_SECRET_ENV_SUFFIXES):
            continue
        if upper in ("GH_TOKEN", "GITHUB_TOKEN", "HF_TOKEN"):
            continue
        env[key] = value
    # Required platform/runtime variables always survive filtering.
    for key in ("PATH", "SystemRoot", "WINDIR", "TMP", "TEMP", "TMPDIR", "HOME", "USER"):
        if key not in env and key in os.environ:
            env[key] = os.environ[key]
    if os.name != "nt" and not env.get("PATH"):
        env["PATH"] = "/usr/bin:/bin"
    return env


def _approved_snapshot() -> dict | None:
    """Broker-stamped execution snapshot for this guard+execute window."""
    try:
        from ah.permissions.broker import get_execution_context

        return get_execution_context()
    except Exception:
        return None


def _call_mode() -> str:
    """Effective mode: approved snapshot wins, else session-effective mode.

    Backends never re-resolve mutable globals directly (AH-AUDIT-002): a
    concurrent mode change cannot alter approved execution semantics.
    """
    snap = _approved_snapshot()
    if snap and snap.get("mode"):
        return str(snap["mode"]).lower()
    try:
        sid = None
        try:
            from ah.tools.agents import current_session_id as _sid_var

            sid = _sid_var.get()
        except Exception:
            sid = None
        from ah.core.session_mode import get_effective_mode

        return get_effective_mode(str(sid) if sid else None)
    except Exception:
        pass
    try:
        from ah.core.config import config as _cfg

        return str(_cfg.get("execution_mode") or "ask").lower()
    except Exception:
        return "ask"


def _kill_tree(process: subprocess.Popen[str]) -> None:
    """Terminate a whole process tree (AH-AUDIT-023).

    Cancelling an asyncio await never stops a thread or child on its own:
    ownership here is explicit. POSIX kills the process group (children
    share it via start_new_session); Windows uses taskkill /T for the
    tree. Best effort, never raises.
    """
    try:
        if process.poll() is not None:
            return
    except Exception:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                timeout=10,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            import signal as _signal

            try:
                os.killpg(os.getpgid(process.pid), _signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    process.kill()
                except OSError:
                    pass
    except Exception:
        try:
            process.kill()
        except OSError:
            pass


def _stop_container(name: str) -> None:
    """Stop a Docker container by name (AH-AUDIT-023).

    Terminating the Docker CLI is not a guarantee the container stops:
    the retained --name handle lets timeout/cancel/lease-loss stop and
    (via --rm) remove it. Best effort, bounded, never raises.
    """
    try:
        subprocess.run(
            ["docker", "stop", "-t", "5", name],
            timeout=15,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def _run_bounded(
    args: list[str],
    timeout: int,
    cwd: str | None,
    stop_event: threading.Event | None = None,
    container_name: str | None = None,
    process_holder: dict | None = None,
) -> str:
    """Read at most one character past the limit before stopping the child."""
    popen_kwargs: dict = {}
    if os.name == "nt":
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        popen_kwargs["start_new_session"] = True
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
        env=_sanitized_env(),
        **popen_kwargs,
    ) as process:
        if process_holder is not None:
            process_holder["process"] = process
        expired = threading.Event()

        def stop_on_timeout() -> None:
            if process.poll() is None:
                expired.set()
                _kill_tree(process)
                if container_name:
                    _stop_container(container_name)

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

            if stop_event is not None and stop_event.is_set() and process.poll() is None:
                _kill_tree(process)
                if container_name:
                    _stop_container(container_name)
            if truncated and process.poll() is None:
                _kill_tree(process)
                if container_name:
                    _stop_container(container_name)
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
    _mode = _call_mode()
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
    _mode3 = _call_mode()
    # AH-AUDIT-002: an approved sandbox backend is never silently downgraded
    # to host execution. Snapshot backend wins over re-resolved settings.
    _snap = _approved_snapshot()
    _snap_backend = str((_snap or {}).get("backend") or "").lower()
    if sandbox == "disabled" and _mode3 == "sandbox":
        raise ValidationError("sandbox mode requires the Docker backend; Docker is not configured")
    use_docker = sandbox == "docker" or _mode3 == "sandbox" or _snap_backend == "sandbox"
    if use_docker and sandbox != "docker" and _mode3 == "sandbox":
        raise ValidationError("sandbox mode requires the Docker backend; Docker is not configured")
    container_name: str | None = None
    if use_docker:
        import uuid as _uuid

        # Identifiable handle retained for stop/remove on cancellation,
        # timeout, or lease loss (AH-AUDIT-023). --rm removes it on stop.
        container_name = f"ah-term-{_uuid.uuid4().hex[:12]}"
        workspace = resolved_workdir
        image = os.environ.get("AGENT_HARNESS_TERMINAL_IMAGE", "agent-harness-tool-sandbox:latest")
        # Read-write project mounts when the approved mode allows writes
        # (workspace/full); ask keeps read-only. The container root fs stays
        # read-only; /tmp + /workspace (when rw) are the writable locations.
        _mm = _call_mode()
        _mount_ro = "" if _mm in ("workspace", "full") else ",readonly"
        args = [
            "docker",
            "run",
            "--rm",
            "--name",
            container_name,
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
    #
    # AH-AUDIT-023: cancellation-aware ownership. A cancelled await does not
    # stop the worker thread or child — the stop event + explicit tree kill
    # (and container stop) do. No further file/network effects occur after
    # cancellation returns; output collection is bounded without holding
    # session ownership forever.
    try:
        exec_cwd = resolved_workdir if workdir != "." else None
        _validate_workdir(resolved_workdir)
        stop = threading.Event()
        holder: dict = {}
        outcome: dict = {}

        def _target() -> None:
            try:
                outcome["result"] = _run_bounded(
                    args,
                    timeout,
                    exec_cwd,
                    stop_event=stop,
                    container_name=container_name,
                    process_holder=holder,
                )
            except BaseException as e:  # noqa: BLE001 — marshalled to caller
                outcome["error"] = e

        worker = threading.Thread(target=_target, daemon=True)
        worker.start()
        try:
            while worker.is_alive():
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            stop.set()
            proc = holder.get("process")
            if proc is not None:
                _kill_tree(proc)
            if container_name:
                await asyncio.to_thread(_stop_container, container_name)
            worker.join(timeout=10)
            raise
        worker.join(timeout=10)
        if "error" in outcome:
            raise outcome["error"]
        if "result" not in outcome:
            raise ToolError("terminal worker exited without a result")
        return outcome["result"]
    except subprocess.TimeoutExpired:
        raise ToolError(f"Command timed out after {timeout}s") from None
    except FileNotFoundError:
        raise ToolError(f"Command not found: {args[0]}") from None
    except Exception as e:
        raise ToolError(f"Error executing command: {e}") from e


registry.declare_effects({"terminal": ("exec",)})
