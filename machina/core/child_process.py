"""Shared child-process leaf mechanics (stdlib only).

Single implementation for the pieces ``machina/tools/terminal.py`` and
``machina/mcp_server`` had duplicated (AH-AUDIT-010/023, R07): the child
environment allowlist, environment sanitizing, process-group liveness, and
the kill-tree terminal. This module carries no machina imports and no
policy: approval, sandbox selection, bounded output shaping, and per-caller
result types stay with each caller.
"""

from __future__ import annotations

import os
import signal
import subprocess

__all__ = [
    "CHILD_ENV_ALLOWLIST",
    "group_members",
    "kill_tree",
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
