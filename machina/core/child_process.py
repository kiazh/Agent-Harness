"""Shared child-process leaf mechanics (stdlib only).

Single implementation for the pieces ``machina/tools/terminal.py`` and
``machina/mcp_server`` had duplicated (AH-AUDIT-010/023, R07): the child
environment allowlist, environment sanitizing, process-group liveness, and
the kill-tree terminal. Round-5 interop review (F2) moved the dual-stream
bounded runner ``run_bounded`` here too — ``machina/interop.py`` and the MCP
shim had each kept its own copy of its mechanics. This module carries no
machina imports and no policy: approval, sandbox selection, per-caller
output shaping, and per-caller result types stay with each caller.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "BoundedResult",
    "CHILD_ENV_ALLOWLIST",
    "group_members",
    "kill_tree",
    "run_bounded",
    "sanitized_env",
]

# AH-AUDIT-010: child processes inherit only the platform variables they
# need to start. Other variables, including credentials with unfamiliar
# names, require explicit passthrough by variable name (see sanitized_env).
CHILD_ENV_ALLOWLIST = frozenset(
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


def sanitized_env(extra_env_var: str | None = None) -> dict[str, str]:
    """Minimal child environment without secrets (AH-AUDIT-010).

    ``extra_env_var`` optionally names an environment variable holding a
    comma-separated list of additional variable names to pass through;
    ``None`` disables passthrough entirely.
    """
    extra: set[str] = set()
    if extra_env_var:
        extra = {
            e.strip().upper() for e in os.environ.get(extra_env_var, "").split(",") if e.strip()
        }
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if upper in CHILD_ENV_ALLOWLIST or upper in extra:
            env[key] = value
    if os.name != "nt" and not env.get("PATH"):
        env["PATH"] = "/usr/bin:/bin"
    return env


def group_members(pgid: int | None, process: subprocess.Popen[str]) -> bool:
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


def kill_tree(process: subprocess.Popen[str], pgid: int | None = None) -> None:
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
        try:
            os.killpg(group, signal.SIGKILL)
            return
        except ProcessLookupError:
            return
        except (PermissionError, OSError):
            pass
    try:
        process.kill()
    except OSError:
        pass


@dataclass(frozen=True)
class BoundedResult:
    """Outcome of one bounded child process (``run_bounded``).

    ``spawn_error`` is set only when the child could not be launched (or its
    pipes were unavailable); in that case ``returncode`` is None and the
    message also travels in ``stderr``. ``timed_out`` marks a deadline kill;
    ``truncated`` marks a stdout-cap kill.
    """

    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool
    truncated: bool
    spawn_error: str | None = None


def run_bounded(
    argv: list[str],
    *,
    env: dict[str, str] | None = None,
    extra_env_var: str | None = None,
    timeout: float,
    stdin_text: str | None = None,
    cwd: Path | str | None = None,
    max_stdout: int = 64_000,
    max_stderr: int = 8_000,
    cancel_event: threading.Event | None = None,
) -> BoundedResult:
    """Run one child in its own process group with a hard kill-tree deadline.

    The union of the two runners this replaces (round-5 interop review, F2):
    stdout and stderr are read on twin capped reader threads; prolific stdout
    (past ``max_stdout``) stops the whole group, while stderr past
    ``max_stderr`` is drained and discarded so its pipe cannot deadlock.
    Completion follows the retained process group (R07), the deadline kills
    the tree, and a set ``cancel_event`` kills it too — cancelling an await
    never stops a thread or child on its own (AH-AUDIT-023). ``env=None``
    builds the shared minimal allowlist (plus ``extra_env_var`` passthrough);
    ``cwd=None`` runs in the call-time launch directory (N2). Spawning never
    raises: an OSError comes back as a typed result (``spawn_error``), and
    ``stdin_text`` writes and closes the child's stdin.
    """
    target_cwd = Path(cwd) if cwd is not None else Path(os.getcwd())
    if env is None:
        env = sanitized_env(extra_env_var)
    popen_kwargs: dict[str, Any] = {}
    if os.name == "nt":
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        popen_kwargs["start_new_session"] = True
    try:
        process = subprocess.Popen(
            argv,
            shell=False,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(target_cwd),
            env=env,
            **popen_kwargs,
        )
    except OSError as error:
        message = str(error)
        return BoundedResult(
            returncode=None,
            stdout="",
            stderr=message,
            timed_out=False,
            truncated=False,
            spawn_error=message,
        )

    def cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    with process:
        # R07: retain the group id independently of the launcher so an
        # already-exited leader cannot hide surviving descendants.
        pgid: int | None = None if os.name == "nt" else process.pid
        expired = threading.Event()
        exceeded = threading.Event()

        def stop_on_timeout() -> None:
            if not group_members(pgid, process):
                return
            expired.set()
            kill_tree(process, pgid)

        def read_stream(stream: Any, parts: list[str], is_stdout: bool, cap: int) -> None:
            total = 0
            while True:
                chunk = stream.read(8192)
                if not chunk:
                    return
                room = cap - total
                if room <= 0:
                    if is_stdout:
                        exceeded.set()
                        return
                    continue  # stderr past the cap: keep draining, discard
                kept = chunk[:room]
                parts.append(kept)
                total += len(kept)
                if is_stdout and len(chunk) > room:
                    exceeded.set()
                    return

        if stdin_text is not None and process.stdin is not None:
            try:
                process.stdin.write(stdin_text)
                process.stdin.close()
            except (OSError, ValueError):
                pass

        timer = threading.Timer(timeout, stop_on_timeout)
        timer.daemon = True
        timer.start()
        try:
            if process.stdout is None or process.stderr is None:
                message = "process pipes unavailable"
                return BoundedResult(
                    returncode=None,
                    stdout="",
                    stderr=message,
                    timed_out=False,
                    truncated=False,
                    spawn_error=message,
                )
            output_parts: list[str] = []
            error_parts: list[str] = []
            readers = [
                threading.Thread(
                    target=read_stream,
                    args=(process.stdout, output_parts, True, max_stdout),
                    daemon=True,
                ),
                threading.Thread(
                    target=read_stream,
                    args=(process.stderr, error_parts, False, max_stderr),
                    daemon=True,
                ),
            ]
            for reader in readers:
                reader.start()
            # The readers block until the child (group) closes its pipes, so
            # keep a bounded wait with periodic cancel checks: caller
            # cancellation must kill the tree promptly, not only once the
            # deadline expires. Prolific stdout is handled the same way: the
            # reader sets ``exceeded`` as soon as the cap is passed, and the
            # supervisor observes it here and stops the group immediately —
            # waiting for the readers to drain would let the child keep
            # running past the cap (review round 3, F9). The shared deadline
            # keeps the total wait bounded by ``timeout`` + teardown for both
            # readers.
            drain_deadline = time.monotonic() + timeout + 5
            for reader in readers:
                while reader.is_alive() and time.monotonic() < drain_deadline:
                    if cancelled() or exceeded.is_set():
                        kill_tree(process, pgid)
                    reader.join(0.05)
            if exceeded.is_set() or cancelled():
                # Prolific stdout (or caller cancellation): stop the group.
                kill_tree(process, pgid)
            # The launcher may exit while group descendants keep running:
            # wait for the whole group, bounded by the deadline timer.
            while not expired.is_set() and not cancelled() and group_members(pgid, process):
                time.sleep(0.02)
            if (expired.is_set() or exceeded.is_set() or cancelled()) and group_members(
                pgid, process
            ):
                kill_tree(process, pgid)
            process.wait()
        except BaseException:
            # Caller cancellation delivered as an exception: no child may
            # outlive this call (AH-AUDIT-023).
            kill_tree(process, pgid)
            raise
        finally:
            timer.cancel()
        return BoundedResult(
            returncode=process.returncode,
            stdout="".join(output_parts),
            stderr="".join(error_parts),
            timed_out=expired.is_set(),
            truncated=exceeded.is_set(),
        )
