"""Config-gated Firecrawl/SearXNG stack lifecycle for the interactive UI.

The self-hosted Firecrawl + SearXNG stack runs inside a Lima ``docker`` VM:
``limactl start docker`` boots the VM and ``$HOME/firecrawl/up.sh up|down``
drives the compose project over that VM's socket (``DOCKER_HOST``). Opening
the interactive UI can bring that stack up in the background; closing the
last live session takes it back down (containers, then the VM — ~5 GiB).

Gated by config keys ``firecrawl_autostart`` (default off),
``firecrawl_stop_on_exit`` (default on) and ``firecrawl_home`` (default
``~/firecrawl``). The contract is deliberately fail-open: every failure is
appended to ``~/.machina/logs/firecrawl-lifecycle.log`` and returned as
``False`` — nothing in this module may raise into the CLI, TUI or gateway.

Cross-session refcounting uses one marker file per session PID under the
Machina state root (``machina_home``/``MACHINA_HOME``, else ``~/.machina``).
A marker records the owner's process start token (a recycled PID is never
mistaken for a live session) and the detached start chain's PID; teardown
stops that chain, and the chain itself re-checks for a live marker
immediately before ``up.sh up``. Prune + decide + teardown run under an
``flock``'d lock file, so concurrent sessions never stop a stack another live
session still holds: only the session that removed the last live marker tears
down.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import signal
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from machina.core.config import config

try:  # POSIX advisory locks for the cross-session lifecycle decision.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX platforms
    fcntl = None  # type: ignore[assignment]

__all__ = ["start_if_enabled", "stop_if_enabled"]

logger = logging.getLogger(__name__)

#: Whole-teardown bound: ``up.sh down`` plus ``limactl stop docker`` share
#: this deadline, so process exit (and any relaunch) can never race an
#: unbounded teardown.
STOP_TIMEOUT_SECONDS = 60.0
#: Lifecycle-lock wait bound: covers one full teardown plus margin, so a
#: session that opens while a peer is stopping waits it out and then decides
#: against an already-pruned marker set.
_LOCK_TIMEOUT_SECONDS = STOP_TIMEOUT_SECONDS + 10.0
_LOCK_POLL_SECONDS = 0.05
#: Floor for a teardown step once the shared deadline is spent, so the second
#: step is still attempted rather than silently skipped.
_MIN_STEP_SECONDS = 0.1
#: Grace for stopping a detached start chain before teardown escalates to
#: SIGKILL; bounded so the fail-open lifecycle can never wedge on it.
_CHAIN_STOP_GRACE_SECONDS = 2.0
#: Legacy markers with no recorded process identity are pruned at this age so
#: a recycled PID cannot pin the stack (and teardown) forever.
_MARKER_MAX_AGE_SECONDS = 30 * 24 * 3600.0

_MARKER_PREFIX = "firecrawl-session-"
_MARKER_SUFFIX = ".marker"

#: PID whose session started the stack in this process, or None. Together
#: with the marker file this makes stop() a no-op for sessions that never
#: brought the stack up (a user-managed stack is never torn down).
_started_pid: int | None = None
#: Warn-once keys already emitted to the lifecycle log (per process).
_warned: set[str] = set()


def _state_dir() -> Path:
    """Machina state root, resolved per call (tests repoint HOME or config).

    Honors the same ``machina_home`` override (settable via ``MACHINA_HOME``)
    the auth stores honor; an empty value falls back to ``~/.machina``.
    """
    try:
        home = str(config.get("machina_home") or "").strip()
    except Exception:
        home = ""
    return Path(home).expanduser() if home else Path.home() / ".machina"


def _run_dir() -> Path:
    return _state_dir() / "run"


def _log_path() -> Path:
    return _state_dir() / "logs" / "firecrawl-lifecycle.log"


def _lifecycle_lock_path() -> Path:
    return _run_dir() / "firecrawl-lifecycle.lock"


def _marker_path(pid: int) -> Path:
    return _run_dir() / f"{_MARKER_PREFIX}{pid}{_MARKER_SUFFIX}"


def _firecrawl_home() -> Path:
    return Path(str(config.get("firecrawl_home") or "").strip()).expanduser()


def _limactl_path() -> str | None:
    return shutil.which("limactl")


def _log(message: str) -> None:
    """Append one timestamped line to the lifecycle log; never raises."""
    try:
        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {message}\n")
    except OSError:
        pass
    logger.debug("firecrawl lifecycle: %s", message)


def _warn_once(key: str, message: str) -> None:
    """Log *message* at most once per process for *key* (spec: warn once)."""
    if key in _warned:
        return
    _warned.add(key)
    _log(f"warning: {message}")


def _spawn_env() -> dict[str, str]:
    """Child environment: the caller's, plus the documented Lima socket.

    ``up.sh`` exports ``DOCKER_HOST`` itself; defaulting it here keeps the
    lifecycle working when a user's script expects the caller to provide it.
    An explicit ``DOCKER_HOST`` in the environment always wins.
    """
    env = dict(os.environ)
    env.setdefault("DOCKER_HOST", f"unix://{Path.home()}/.lima/docker/sock/docker.sock")
    return env


def _spawn_detached(command: str) -> int:
    """Run *command* under ``/bin/sh`` in a new session, output into the log.

    The child survives this process (``start_new_session``), so a slow VM
    boot never blocks the UI; its stdout/stderr are appended to the
    lifecycle log for post-hoc debugging. Returns the child PID.
    """
    log_path = _log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "ab") as handle:
        process = subprocess.Popen(
            ["/bin/sh", "-c", command],
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=_spawn_env(),
        )
    return process.pid


@contextmanager
def _lifecycle_lock() -> Iterator[bool]:
    """Bounded exclusive lifecycle lock; yields False when not acquired.

    Acquisition is bounded by ``_LOCK_TIMEOUT_SECONDS`` and never raises: a
    lock failure degrades to a logged best-effort path so a wedged peer can
    never block the interactive UI. Callers decide what the lock means (start
    proceeds without it; stop refuses to tear down without it).
    """
    if fcntl is None:  # pragma: no cover - Lima/VM feature is POSIX-only
        yield True
        return
    try:
        lock_path = _lifecycle_lock_path()
        lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as error:
        _log(f"warning: lifecycle lock unavailable ({type(error).__name__})")
        yield False
        return
    acquired = False
    try:
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(_LOCK_POLL_SECONDS)
                continue
            acquired = True
            break
        if not acquired:
            _log("warning: lifecycle lock busy; proceeding best-effort")
        yield acquired
    finally:
        try:
            if acquired:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _marker_pid(marker: Path) -> int | None:
    """PID encoded in a marker filename, or None when the name is unreadable."""
    name = marker.name
    if not (name.startswith(_MARKER_PREFIX) and name.endswith(_MARKER_SUFFIX)):
        return None
    try:
        return int(name[len(_MARKER_PREFIX) : -len(_MARKER_SUFFIX)])
    except ValueError:
        return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        # PermissionError and anything else: assume the process exists.
        return True
    return True


def _process_started(pid: int) -> str:
    """Bounded, locale-stable ``ps`` start-time token for *pid*, or ``""``.

    The token is only ever compared against itself (recorded at marker-write
    time), so no date parsing is needed; ``LC_ALL=C`` keeps the format stable
    across environments. Never raises.
    """
    try:
        completed = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=2,
            env={**os.environ, "LC_ALL": "C", "LANG": "C"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if completed.returncode != 0:
        return ""
    return " ".join(completed.stdout.split())


def _read_marker(marker: Path) -> dict:
    """Marker JSON as a dict; an unreadable marker degrades to ``{}``."""
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _marker_owner_alive(pid: int, data: dict) -> bool:
    """Whether the marker's session PID is still the process that wrote it.

    ``os.kill(pid, 0)`` alone cannot see PID reuse: a recycled PID would pin
    the marker (and the stack) forever. The marker records the owner's
    ``ps`` start token, so a mismatch means the PID now belongs to another
    process and the marker is stale. Legacy markers without a token stay
    conservative except for the age bound.
    """
    if not _pid_alive(pid):
        return False
    recorded = data.get("process_started")
    if not isinstance(recorded, str) or not recorded:
        try:
            age = time.time() - float(data.get("started_at"))
        except (TypeError, ValueError):
            return True
        return age <= _MARKER_MAX_AGE_SECONDS
    current = _process_started(pid)
    if not current or current == recorded:
        return True
    return False


def _terminate_chain(child_pid: object, child_started: str) -> None:
    """Best-effort stop of a detached start chain before teardown.

    The chain boots the VM and then composes the stack up; if it outlives the
    last session, it resurrects containers and a ~5 GiB VM with no marker
    left. The chain is a session leader (``start_new_session``), so it is
    signalled as a process group. Identity is verified first: a recycled PID
    whose start token differs is not ours to kill. Never raises.
    """
    if not isinstance(child_pid, int) or child_pid <= 1:
        return
    if not child_started:
        _log("stop: chain identity unavailable; leaving it to the marker fence")
        return
    current = _process_started(child_pid)
    if current and current != child_started:
        _log(f"stop: chain pid {child_pid} was reused; not signalling")
        return
    try:
        os.killpg(child_pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError as error:
        _log(f"stop: could not signal chain pid {child_pid} ({type(error).__name__})")
        return
    deadline = time.monotonic() + _CHAIN_STOP_GRACE_SECONDS
    while time.monotonic() < deadline:
        try:
            os.killpg(child_pid, 0)
        except OSError:
            _log(f"stop: start chain {child_pid} stopped before teardown")
            return
        time.sleep(0.05)
    try:
        os.killpg(child_pid, signal.SIGKILL)
        _log(f"stop: start chain {child_pid} killed after the grace period")
    except OSError:
        pass


def _prune_dead_markers() -> None:
    """Remove markers whose session is gone or whose PID was reused.

    Runs under the lifecycle lock. A stale session's start chain can still be
    booting the VM, so the chain recorded in the marker is stopped too: it
    cannot resurrect the stack after the last live marker is gone.
    """
    pruned = 0
    for marker in _run_dir().glob(f"{_MARKER_PREFIX}*{_MARKER_SUFFIX}"):
        data = _read_marker(marker)
        pid = data.get("pid")
        if not isinstance(pid, int):
            pid = _marker_pid(marker)
        if pid is not None and _marker_owner_alive(pid, data):
            continue
        try:
            marker.unlink()
        except OSError:
            continue
        pruned += 1
        _terminate_chain(data.get("child_pid"), str(data.get("child_started") or ""))
    if pruned:
        _log(f"pruned {pruned} stale session marker(s)")


def _write_marker(
    marker: Path,
    pid: int,
    *,
    child_pid: int | None = None,
    child_started: str = "",
) -> None:
    """Persist this session's marker; a write failure only narrows refcounting.

    ``process_started`` records the owner's process start token so a later
    prune can tell a recycled PID apart; ``child_pid``/``child_started``
    record the detached start chain for the teardown kill.
    """
    try:
        marker.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        marker.write_text(
            json.dumps(
                {
                    "pid": pid,
                    "started_at": time.time(),
                    "process_started": _process_started(pid),
                    "child_pid": child_pid,
                    "child_started": child_started,
                }
            ),
            encoding="utf-8",
        )
    except OSError as error:
        _log(f"warning: could not write session marker ({type(error).__name__})")


def _remove_marker(marker: Path) -> None:
    try:
        marker.unlink()
    except FileNotFoundError:
        pass
    except OSError as error:
        _log(f"warning: could not remove session marker ({type(error).__name__})")


def _launcher_argv(up_script: Path, action: str) -> list[str]:
    """Argv for the user's launcher, tolerating a missing exec bit.

    Executable scripts run directly (their shebang decides the interpreter).
    The user's ``~/firecrawl/up.sh`` ships non-executable, so otherwise the
    script is handed to its declared interpreter (``#!`` line, ``env``-aware)
    with a plain ``/bin/sh`` fallback — never a direct exec of a 0644 file.
    """
    if os.access(str(up_script), os.X_OK):
        return [str(up_script), action]
    interpreter = "/bin/sh"
    try:
        with open(up_script, encoding="utf-8", errors="replace") as handle:
            shebang = handle.readline().strip()
    except OSError:
        shebang = ""
    if shebang.startswith("#!"):
        parts = shlex.split(shebang[2:])
        if parts and Path(parts[0]).name == "env":
            parts = parts[1:]
        if parts:
            interpreter = shutil.which(parts[0]) or parts[0]
    return [interpreter, str(up_script), action]


def _fence_command() -> str:
    """Shell step that aborts the chain when no live session marker remains.

    The VM boot takes ~33 s; a session can quit (and teardown finish) while
    the detached chain is still starting Lima. The fence is evaluated
    immediately before ``up.sh up`` and re-checks liveness at that moment
    (``kill -0``), so the chain never composes the stack up for a session
    set that is already gone.
    """
    pattern = shlex.quote(str(_run_dir())) + f"/{_MARKER_PREFIX}*{_MARKER_SUFFIX}"
    fence = (
        f"for _m in {pattern}; do"
        ' [ -e "$_m" ] || continue;'
        " _p=${_m##*" + _MARKER_PREFIX + "};"
        " _p=${_p%" + _MARKER_SUFFIX + "};"
        ' kill -0 "$_p" 2>/dev/null && exit 0;'
        " done;"
        ' echo "fence: no live session marker; skipping up.sh up";'
        " exit 1"
    )
    # The subshell keeps the fence's `exit` from terminating the whole chain
    # (a bare `exit 0` would skip `up.sh up` entirely).
    return f"( {fence} )"


def _start_command(limactl: str | None, up_argv: list[str]) -> str:
    """Shell chain: boot the VM (best effort), fence, then compose the stack up."""
    steps = []
    if limactl is not None:
        steps.append(f"{shlex.quote(limactl)} start docker")
    steps.append(_fence_command())
    steps.append(" ".join(shlex.quote(part) for part in up_argv))
    return " && ".join(steps)


def _run_bounded(argv: list[str], label: str, deadline: float) -> None:
    """Run one teardown step under the shared deadline; failures are logged."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        _log(f"stop: {label} skipped (timeout budget exhausted)")
        return
    _log(f"stop: running {label}")
    try:
        completed = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=max(remaining, _MIN_STEP_SECONDS),
            env=_spawn_env(),
        )
    except subprocess.TimeoutExpired:
        _log(f"stop: {label} timed out; moved on")
    except OSError as error:
        _log(f"stop: {label} failed to start ({type(error).__name__})")
    else:
        if completed.returncode == 0:
            _log(f"stop: {label} ok")
        else:
            detail = " ".join((completed.stdout + completed.stderr).split())[:300]
            _log(f"stop: {label} exited {completed.returncode} (tolerated): {detail}")


def _teardown(up_script: Path, limactl: str | None) -> None:
    """Stop the compose project, then the VM, within ``STOP_TIMEOUT_SECONDS``.

    Synchronous by design: when this returns, teardown is finished or was
    abandoned at the bound — a relaunch can never race it.
    """
    deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
    _run_bounded(_launcher_argv(up_script, "down"), "up.sh down", deadline)
    if limactl is None:
        _log("stop: limactl not found; docker VM left running")
        return
    _run_bounded([str(limactl), "stop", "docker"], "limactl stop docker", deadline)


def start_if_enabled() -> bool:
    """Bring the local Firecrawl/SearXNG stack up in the background.

    Returns True when a start chain was scheduled; the chain (``limactl
    start docker`` then ``<home>/up.sh up``) detaches immediately, so the UI
    never waits for a VM boot. No-op (False) when ``firecrawl_autostart`` is
    off or ``<home>/up.sh`` is missing. Never raises.
    """
    global _started_pid
    try:
        if not config.get("firecrawl_autostart"):
            return False
        up_script = _firecrawl_home() / "up.sh"
        if not up_script.is_file():
            _warn_once(
                f"up-missing:{up_script}",
                f"start skipped: {up_script} does not exist",
            )
            return False
        pid = os.getpid()
        marker = _marker_path(pid)
        limactl = _limactl_path()
        if limactl is None:
            _warn_once(
                "limactl-missing",
                "limactl not found on PATH; expecting an already-running docker VM",
            )
        command = _start_command(limactl, _launcher_argv(up_script, "up"))
        _log(f"start: launching {command}")
        # Marker write + spawn + chain recording happen under one lock hold:
        # a concurrent stop then never observes a marker that misses the
        # chain it must be able to stop (fast-quit orphan fence).
        with _lifecycle_lock() as locked:
            _prune_dead_markers()
            _write_marker(marker, pid)
            try:
                child_pid = _spawn_detached(command)
            except OSError as error:
                _log(f"start failed: could not launch ({type(error).__name__}: {error})")
                _remove_marker(marker)
                return False
            _write_marker(
                marker,
                pid,
                child_pid=child_pid,
                child_started=_process_started(child_pid),
            )
        if not locked:
            _log("warning: start proceeded without the lifecycle lock (busy)")
        _started_pid = pid
        _log(f"start: scheduled (pid {child_pid}); output appends below")
        return True
    except Exception as error:
        # Contract: the lifecycle is best-effort glue — never raise into machina.
        _log(f"start failed: {type(error).__name__}: {error}")
        return False


def stop_if_enabled() -> bool:
    """Stop the stack and the VM when this session may be the last live one.

    Returns True when a teardown ran. No-op (False) when
    ``firecrawl_stop_on_exit`` is off, when this session never started the
    stack, or when another live session's marker remains. Never raises.
    """
    global _started_pid
    try:
        if not config.get("firecrawl_stop_on_exit"):
            return False
        pid = _started_pid
        if pid is None:
            # Never started by this session (autostart off, a failed start,
            # or a repeat call after teardown): leave a user-managed stack
            # alone.
            return False
        up_script = _firecrawl_home() / "up.sh"
        with _lifecycle_lock() as locked:
            if not locked:
                # Tearing down without the lock could stop a stack a live
                # session still holds: keep everything and only log.
                _log("stop: lifecycle lock busy; keeping the stack (marker retained)")
                return False
            _prune_dead_markers()
            marker = _marker_path(pid)
            # Read the chain details before unlinking: removing the marker
            # closes the chain's fence, and no new chained start can follow.
            data = _read_marker(marker)
            _remove_marker(marker)
            remaining = list(_run_dir().glob(f"{_MARKER_PREFIX}*{_MARKER_SUFFIX}"))
            if remaining:
                _started_pid = None
                _log("stop: another live session still holds the stack; keeping it")
                return False
            # Last live session: stop this session's still-booting chain
            # before teardown, so `up.sh up` can never run after the down
            # completes and leave an unfenced stack behind.
            _terminate_chain(data.get("child_pid"), str(data.get("child_started") or ""))
            _teardown(up_script, _limactl_path())
        _started_pid = None
        return True
    except Exception as error:
        # Same fail-open contract as start_if_enabled.
        _log(f"stop failed: {type(error).__name__}: {error}")
        _started_pid = None
        return False
