"""Terminal tool — execute shell commands securely."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import threading
import time
from pathlib import Path

from machina.core.config import config
from machina.core.exceptions import ToolError, ValidationError
from machina.observability.diagnostics import record_failure
from machina.tools.base import registry

logger = logging.getLogger(__name__)
MAX_OUTPUT_CHARS = 64_000


class CommandOutput(str):
    """String-compatible output with the actual subprocess completion status."""

    def __new__(cls, text: str, *, returncode: int, truncated: bool = False):
        instance = super().__new__(cls, text)
        instance.is_error = returncode != 0 or truncated
        instance.returncode = returncode
        return instance


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

# A command may only see the configured workspace, never the whole home
# directory or a system temporary directory that may contain credentials.
ALLOWED_WORKDIR_PREFIXES = (str(Path(config.get("machina_home") or Path.cwd()).resolve()),)
_IMPORT_WORKDIR_PREFIXES = ALLOWED_WORKDIR_PREFIXES

# Argument blocklist: git aliases, find -exec, and test-runner overrides can
# escape the command allowlist (e.g. `find . -exec`, `git -c`, `pytest -o`).
_BLOCKED_ARGS = frozenset({"-exec", "-execdir", "-delete", "-c", "-C", "--config", "-o"})


def _allowed_workdir_prefixes() -> tuple[str, ...]:
    from machina.core.execution_context import execution_context
    from machina.permissions.broker import get_execution_context

    if not get_execution_context() and ALLOWED_WORKDIR_PREFIXES != _IMPORT_WORKDIR_PREFIXES:
        return tuple(ALLOWED_WORKDIR_PREFIXES)
    return (str(execution_context("terminal").workspace),)


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


# AH-AUDIT-010: child processes inherit only the platform variables they
# need to start. Other variables, including credentials with unfamiliar
# names, require explicit MACHINA_TERMINAL_EXTRA_ENV passthrough.
_CHILD_ENV_ALLOWLIST = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "TMPDIR",
        "TMP",
        "TEMP",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TERM",
        "VIRTUAL_ENV",
        "SYSTEMROOT",
        "WINDIR",
        "USERPROFILE",
        "COMSPEC",
        "PATHEXT",
        "APPDATA",
        "LOCALAPPDATA",
    }
)


def _sanitized_env() -> dict[str, str]:
    """Minimal child environment without secrets (AH-AUDIT-010)."""
    extra = {
        e.strip().upper()
        for e in os.environ.get("MACHINA_TERMINAL_EXTRA_ENV", "").split(",")
        if e.strip()
    }
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if upper in _CHILD_ENV_ALLOWLIST or upper in extra:
            env[key] = value
    if os.name != "nt" and not env.get("PATH"):
        env["PATH"] = "/usr/bin:/bin"
    return env


def _approved_snapshot() -> dict | None:
    from machina.permissions.broker import get_execution_context

    return get_execution_context()


def _call_mode() -> str:
    from machina.core.execution_context import execution_context

    return execution_context("terminal").mode


def _group_members(pgid: int | None, process: subprocess.Popen[str]) -> bool:
    """True while the launcher or any of its session descendants remains.

    R07: a command like ``(long-work) &`` exits immediately while its child
    keeps running, so completion must be judged on the retained process
    group, not on ``process.poll()`` alone. Reaps an exited launcher first
    so only live descendants (plus not-yet-reaped group zombies) count.
    """
    if os.name == "nt" or pgid is None:
        return process.poll() is None
    process.poll()
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return process.returncode is None
    return True


def _kill_tree(process: subprocess.Popen[str], pgid: int | None = None) -> None:
    """Terminate a whole process tree (AH-AUDIT-023, R07).

    Cancelling an asyncio await never stops a thread or child on its own:
    ownership here is explicit. POSIX kills the process group; the group id
    is retained independently of the launcher (captured at spawn), so an
    already-exited launcher cannot hide surviving descendants. Windows uses
    taskkill /T while the launcher pid is live. Best effort, never raises.
    """
    if os.name == "nt":
        try:
            if process.poll() is not None:
                return
        except Exception:
            return
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                timeout=10,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            try:
                process.kill()
            except OSError:
                pass
        return
    group = pgid
    if group is None:
        try:
            derived = os.getpgid(process.pid)
        except OSError:
            derived = None
        # Never killpg an inherited group this process did not create.
        group = derived if derived == process.pid else None
    if group is not None:
        import signal as _signal

        try:
            os.killpg(group, _signal.SIGKILL)
            return
        except ProcessLookupError:
            return
        except (PermissionError, OSError):
            pass
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
    except Exception as _boundary_error:
        # Optional fallback preserves the primary outcome; report no payload.
        record_failure("terminal._stop_container", _boundary_error)


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
        # R07: the process-group identity is retained independently of the
        # launcher so an exited leader cannot hide surviving descendants.
        pgid: int | None = None if os.name == "nt" else process.pid
        if process_holder is not None:
            process_holder["process"] = process
            process_holder["pgid"] = pgid
        expired = threading.Event()

        def stop_on_timeout() -> None:
            if not _group_members(pgid, process):
                return
            expired.set()
            _kill_tree(process, pgid)
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

            def cancelled() -> bool:
                return stop_event is not None and stop_event.is_set()

            if cancelled():
                _kill_tree(process, pgid)
                if container_name:
                    _stop_container(container_name)
            if truncated:
                # Prolific output: stop the whole group, including
                # descendants that outlived an already-exited launcher.
                _kill_tree(process, pgid)
                if container_name:
                    _stop_container(container_name)
            # R07 completion semantics: the launcher may exit while its
            # descendants keep running (with or without the pipe). Never
            # return while the group still has members; the deadline timer
            # kills whatever is alive when the deadline expires.
            while not expired.is_set() and not cancelled() and _group_members(pgid, process):
                time.sleep(0.02)
            if (expired.is_set() or cancelled() or truncated) and _group_members(pgid, process):
                _kill_tree(process, pgid)
                if container_name:
                    _stop_container(container_name)
            process.wait()
            # Bounded retirement: a killed descendant must not be able to act
            # on the workspace after this call returns.
            retire_until = time.monotonic() + 2.0
            while _group_members(pgid, process) and time.monotonic() < retire_until:
                time.sleep(0.02)
        finally:
            timer.cancel()

        if expired.is_set():
            raise subprocess.TimeoutExpired(args, timeout)
        output = "".join(output_parts)
        if truncated:
            return CommandOutput(
                output[:MAX_OUTPUT_CHARS] + "\n[output truncated]",
                returncode=process.returncode,
                truncated=True,
            )
        return CommandOutput(
            output or f"(exit code {process.returncode}, no output)", returncode=process.returncode
        )


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

    from machina.permissions.shell import parse_terminal_command

    parsed = parse_terminal_command(command)
    args = parsed.argv

    # The broker already classified or obtained approval for this exact
    # command. Keep the narrow allowlist for direct, unbrokered calls.
    base_cmd = os.path.basename(args[0])
    _mode = _call_mode()
    snapshot = _approved_snapshot()
    authorized = bool(snapshot and snapshot.get("operation") == "process.exec")
    if authorized and snapshot.get("argv") != args:
        raise ValidationError("command differs from broker-authorized argv")
    if authorized and snapshot.get("shell_payload", "") != parsed.payload:
        raise ValidationError("command differs from broker-authorized shell payload")
    if parsed.payload and not authorized:
        raise ValidationError("Shell syntax requires broker authorization")
    if _mode == "full" or authorized:
        import shutil as _shutil

        if _shutil.which(args[0]) is None and _shutil.which(base_cmd) is None:
            raise ValidationError(f"Command '{base_cmd}' not found on PATH")
    elif base_cmd not in ALLOWED_COMMANDS:
        raise ValidationError(f"Command '{base_cmd}' is not in the allowlist")

    # Tighten allowlist: block escape hatches even for allowed commands.
    if not authorized:
        _validate_blocked_args(args)

    # Relative workdirs share the policy's workspace root, not process CWD.
    from machina.core.execution_context import execution_context

    workdir_path = Path(workdir or ".")
    if not workdir_path.is_absolute():
        workdir_path = execution_context("terminal").workspace / workdir_path
    resolved_workdir = str(workdir_path.resolve())
    if authorized and resolved_workdir != snapshot.get("cwd"):
        raise ValidationError("workdir differs from broker-authorized cwd")
    _validate_workdir(resolved_workdir)

    # Local subprocesses are not a security boundary: git aliases, find -exec,
    # and test runners can execute commands outside the command allowlist.
    # Phase D: the permission broker (agent loop) already authorized this
    # action; this layer validates backend availability. Host execution is
    # available in ask/workspace/full modes once approved — Docker sandbox
    # use stays independent of permission policy.
    sandbox = str(config.get("terminal_sandbox") or "disabled").lower()
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
        container_name = f"machina-term-{_uuid.uuid4().hex[:12]}"
        workspace = resolved_workdir
        image = config.get("terminal_image")
        # Broker-authorized project workflows may write in every mode. The
        # container root stays read-only; only /tmp and /workspace are writable.
        _mm = _call_mode()
        _mount_ro = "" if authorized or _mm in ("workspace", "full") else ",readonly"
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
            "/tmp:rw,nosuid,size=64m",  # nosec B108 # Private Docker tmpfs; no host temporary file.
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
        exec_cwd = resolved_workdir
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
            except BaseException as e:  # marshalled to caller
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
                _kill_tree(proc, holder.get("pgid"))
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
