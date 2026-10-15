"""LANE F: machina-managed Firecrawl stack lifecycle (config-gated, off by default).

Hermetic by construction: fake ``$HOME``, a stub ``limactl`` on ``PATH`` and a
stub ``<home>/up.sh`` inside a temp "home". The detached start chain runs for
real against the stubs, so process order, child environment and log routing
are asserted end to end. The real Lima VM / docker stack is never touched.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from machina.core import firecrawl_lifecycle as fl
from machina.core.config import DEFAULTS, Config, config, validate_value

WAIT_SECONDS = 10.0


@pytest.fixture(autouse=True)
def lifecycle_state(monkeypatch):
    """Fresh module state so no test inherits a warmed session's refcount."""
    monkeypatch.setattr(fl, "_warned", set())
    monkeypatch.setattr(fl, "_started_pid", None)


@pytest.fixture
def stack(tmp_path, monkeypatch):
    """Fake HOME + stub ``limactl``/``up.sh`` + enabled config keys."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    for key in ("MACHINA_FIRECRAWL_AUTOSTART", "MACHINA_FIRECRAWL_STOP_ON_EXIT"):
        monkeypatch.delenv(key, raising=False)

    trace = tmp_path / "trace.log"
    firecrawl_dir = home / "firecrawl"
    firecrawl_dir.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    up_script = firecrawl_dir / "up.sh"
    up_script.write_text(
        "#!/bin/sh\n"
        f'echo "up.sh $* docker_host=${{DOCKER_HOST:-unset}}" >> "{trace}"\n'
        'if [ "$1" = "down" ]; then exit "${STUB_DOWN_EXIT:-0}"; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    up_script.chmod(0o755)

    limactl = bin_dir / "limactl"
    limactl.write_text(
        "#!/bin/sh\n"
        f'echo "limactl $*" >> "{trace}"\n'
        'if [ "$1 $2" = "stop docker" ]; then exit "${STUB_LIMACTL_STOP_EXIT:-0}"; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    limactl.chmod(0o755)

    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(config, "firecrawl_autostart", True)
    monkeypatch.setattr(config, "firecrawl_stop_on_exit", True)
    monkeypatch.setattr(config, "firecrawl_home", str(firecrawl_dir))

    return SimpleNamespace(
        home=home,
        firecrawl_dir=firecrawl_dir,
        up_script=up_script,
        limactl=limactl,
        trace=trace,
        socket=f"unix://{home}/.lima/docker/sock/docker.sock",
    )


def _log_text(stack: SimpleNamespace) -> str:
    return (stack.home / ".machina" / "logs" / "firecrawl-lifecycle.log").read_text(
        encoding="utf-8"
    )


def _trace_lines(stack: SimpleNamespace, count: int) -> list[str]:
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        if stack.trace.exists():
            lines = stack.trace.read_text(encoding="utf-8").splitlines()
            if len(lines) >= count:
                if len(lines) > count:
                    pytest.fail(f"unexpected extra trace lines: {lines}")
                return lines
        time.sleep(0.05)
    pytest.fail(f"stub trace never reached {count} line(s): {stack.trace}")


def _dead_pid() -> int:
    """PID of a process that has exited (marker for a stale session)."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=WAIT_SECONDS)
    return child.pid


def _plant_marker(pid: int) -> Path:
    marker = fl._marker_path(pid)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"pid": pid, "started_at": time.time()}), encoding="utf-8")
    return marker


# --- config surface ---------------------------------------------------------


def test_config_keys_defaults_env_and_coercion(monkeypatch):
    assert DEFAULTS["firecrawl_autostart"] is False
    assert DEFAULTS["firecrawl_stop_on_exit"] is True
    assert DEFAULTS["firecrawl_home"] == "~/firecrawl"
    fresh = Config()
    assert fresh.firecrawl_autostart is False
    assert fresh.firecrawl_stop_on_exit is True
    assert fresh.firecrawl_home == "~/firecrawl"
    assert validate_value("firecrawl_autostart", "yes") is True
    assert validate_value("firecrawl_stop_on_exit", "off") is False
    assert validate_value("firecrawl_home", " /srv/firecrawl ") == " /srv/firecrawl "
    with pytest.raises(ValueError):
        validate_value("firecrawl_autostart", "maybe")
    monkeypatch.setenv("MACHINA_FIRECRAWL_AUTOSTART", "true")
    monkeypatch.setenv("MACHINA_FIRECRAWL_STOP_ON_EXIT", "false")
    monkeypatch.setenv("MACHINA_FIRECRAWL_HOME", "/srv/firecrawl")
    assert config.get("firecrawl_autostart") is True
    assert config.get("firecrawl_stop_on_exit") is False
    assert config.get("firecrawl_home") == "/srv/firecrawl"


# --- disabled / no-op paths -------------------------------------------------


def test_disabled_is_a_full_noop(stack, monkeypatch):
    monkeypatch.setattr(config, "firecrawl_autostart", False)  # the shipped default
    assert fl.start_if_enabled() is False
    assert fl.stop_if_enabled() is False
    assert not stack.trace.exists()
    assert not (stack.home / ".machina").exists()


def test_stop_without_start_never_touches_a_user_stack(stack):
    """stop_on_exit defaults on, but a session that did not start the stack
    must never tear one down (e.g. autostart off, stack brought up by hand)."""
    assert fl.stop_if_enabled() is False
    assert not stack.trace.exists()
    assert not (stack.home / ".machina").exists()


# --- start ------------------------------------------------------------------


def test_start_spawns_limactl_then_up_with_env_marker_and_log(stack):
    assert fl.start_if_enabled() is True
    assert _trace_lines(stack, 2) == [
        "limactl start docker",
        f"up.sh up docker_host={stack.socket}",
    ]
    marker = fl._marker_path(os.getpid())
    assert marker.exists()
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload["pid"] == os.getpid()
    log = _log_text(stack)
    assert "start: launching" in log
    assert "start: scheduled" in log


def test_start_respects_an_explicit_docker_host(stack, monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "unix:///custom/docker.sock")
    assert fl.start_if_enabled() is True
    assert _trace_lines(stack, 2)[1] == "up.sh up docker_host=unix:///custom/docker.sock"


def test_non_executable_launcher_runs_via_its_interpreter(stack):
    """The user's real ~/firecrawl/up.sh is mode 0644: a direct exec would
    EACCES, so start and stop must run it under its shebang interpreter."""
    stack.up_script.chmod(0o644)
    assert fl.start_if_enabled() is True
    assert _trace_lines(stack, 2) == [
        "limactl start docker",
        f"up.sh up docker_host={stack.socket}",
    ]
    assert fl.stop_if_enabled() is True
    lines = _trace_lines(stack, 4)
    assert lines[2] == f"up.sh down docker_host={stack.socket}"
    assert lines[3] == "limactl stop docker"


def test_start_without_limactl_runs_up_only_and_warns_once(stack, monkeypatch):
    monkeypatch.setattr(fl, "_limactl_path", lambda: None)
    assert fl.start_if_enabled() is True
    assert _trace_lines(stack, 1) == [f"up.sh up docker_host={stack.socket}"]
    assert fl.start_if_enabled() is True  # repeat: no duplicate warning
    _trace_lines(stack, 2)
    assert _log_text(stack).count("limactl not found") == 1


def test_start_is_idempotent_for_the_same_session(stack):
    assert fl.start_if_enabled() is True
    assert fl.start_if_enabled() is True
    lines = _trace_lines(stack, 4)
    # Each call fires the (idempotent) compose chain; two concurrent chains may
    # interleave, so pin the per-chain shape by counts, not global positions.
    assert lines[0] == "limactl start docker"
    assert lines.count("limactl start docker") == 2
    assert sum(1 for line in lines if line.startswith("up.sh up ")) == 2
    markers = list((stack.home / ".machina" / "run").glob("*.marker"))
    assert [m.name for m in markers] == [f"firecrawl-session-{os.getpid()}.marker"]


def test_start_prunes_dead_markers(stack):
    stale = _plant_marker(_dead_pid())
    assert fl.start_if_enabled() is True
    _trace_lines(stack, 2)
    assert not stale.exists()
    assert fl._marker_path(os.getpid()).exists()


def test_missing_up_script_warns_once_and_does_not_start(stack):
    stack.up_script.unlink()
    assert fl.start_if_enabled() is False
    assert fl.start_if_enabled() is False  # warn once per process
    assert not stack.trace.exists()
    assert list((stack.home / ".machina" / "run").glob("*.marker")) == []
    log = _log_text(stack)
    assert log.count("does not exist") == 1


def test_spawn_failure_is_logged_not_raised_and_marker_cleaned(stack, monkeypatch):
    def _boom(command: str) -> int:
        raise OSError("spawn unavailable")

    monkeypatch.setattr(fl, "_spawn_detached", _boom)
    assert fl.start_if_enabled() is False
    assert "start failed" in _log_text(stack)
    assert not fl._marker_path(os.getpid()).exists()


def test_invalid_config_value_is_logged_not_raised(stack, monkeypatch):
    monkeypatch.setenv("MACHINA_FIRECRAWL_AUTOSTART", "maybe")
    assert fl.start_if_enabled() is False
    assert "start failed" in _log_text(stack)
    assert not stack.trace.exists()


# --- stop / refcounting -----------------------------------------------------


def test_stop_keeps_stack_while_another_live_session_marker_exists(stack):
    assert fl.start_if_enabled() is True
    _trace_lines(stack, 2)
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        other_marker = _plant_marker(other.pid)
        assert fl.stop_if_enabled() is False
        assert not stack.trace.read_text(encoding="utf-8").splitlines()[2:]  # no teardown
        assert not fl._marker_path(os.getpid()).exists()
        assert other_marker.exists()
    finally:
        other.terminate()
        other.wait(timeout=WAIT_SECONDS)


def test_last_session_tears_down_and_prunes_stale_marker(stack):
    assert fl.start_if_enabled() is True
    _trace_lines(stack, 2)
    stale = _plant_marker(_dead_pid())

    assert fl.stop_if_enabled() is True
    lines = _trace_lines(stack, 4)
    assert lines[2:] == [
        f"up.sh down docker_host={stack.socket}",
        "limactl stop docker",
    ]
    assert not stale.exists()
    assert not fl._marker_path(os.getpid()).exists()
    assert "stop: up.sh down ok" in _log_text(stack)
    # The refcount is consumed: a repeat call (e.g. the atexit safety net) no-ops.
    assert fl.stop_if_enabled() is False
    assert len(stack.trace.read_text(encoding="utf-8").splitlines()) == 4


def test_stop_on_exit_false_skips_teardown_and_keeps_marker(stack, monkeypatch):
    assert fl.start_if_enabled() is True
    _trace_lines(stack, 2)
    monkeypatch.setattr(config, "firecrawl_stop_on_exit", False)
    assert fl.stop_if_enabled() is False
    assert len(stack.trace.read_text(encoding="utf-8").splitlines()) == 2
    assert fl._marker_path(os.getpid()).exists()


def test_stop_skips_teardown_when_the_lifecycle_lock_is_unavailable(stack, monkeypatch):
    assert fl.start_if_enabled() is True
    _trace_lines(stack, 2)

    @contextlib.contextmanager
    def _never_locked():
        yield False

    monkeypatch.setattr(fl, "_lifecycle_lock", _never_locked)
    assert fl.stop_if_enabled() is False
    assert len(stack.trace.read_text(encoding="utf-8").splitlines()) == 2
    assert fl._marker_path(os.getpid()).exists()
    assert "lock busy" in _log_text(stack)


def test_stop_timeout_is_bounded_and_logged(stack, monkeypatch):
    assert fl.start_if_enabled() is True
    _trace_lines(stack, 2)
    stack.up_script.write_text(
        '#!/bin/sh\nif [ "$1" = "down" ]; then sleep 30; fi\nexit 0\n',
        encoding="utf-8",
    )
    stack.up_script.chmod(0o755)
    monkeypatch.setattr(fl, "STOP_TIMEOUT_SECONDS", 0.7)

    started = time.monotonic()
    assert fl.stop_if_enabled() is True
    elapsed = time.monotonic() - started
    assert elapsed < 5.0  # far below the stub's 30s; the bound cut it off
    assert "timed out" in _log_text(stack)


def test_stop_tolerates_failing_down_and_missing_limactl(stack, monkeypatch):
    assert fl.start_if_enabled() is True
    _trace_lines(stack, 2)
    monkeypatch.setenv("STUB_DOWN_EXIT", "3")
    monkeypatch.setattr(fl, "_limactl_path", lambda: None)

    assert fl.stop_if_enabled() is True
    assert _trace_lines(stack, 3)[2] == f"up.sh down docker_host={stack.socket}"
    log = _log_text(stack)
    assert "exited 3 (tolerated)" in log
    assert "limactl not found; docker VM left running" in log


# --- interactive-entry wiring -----------------------------------------------


def _fake_ui_env(tmp_path, monkeypatch):
    """Point ``launch_ui`` at a stub node + empty UI so preconditions pass."""
    from machina.cli import launcher

    ui = tmp_path / "ui"
    (ui / "src").mkdir(parents=True)
    (ui / "src" / "main.ts").write_text("// stub ui entry\n", encoding="utf-8")
    (ui / "node_modules" / "@earendil-works" / "pi-tui").mkdir(parents=True)
    node = tmp_path / "node-bin" / "node"
    node.parent.mkdir()
    node.write_text("#!/bin/sh\necho v22.19.0\n", encoding="utf-8")
    node.chmod(0o755)
    monkeypatch.setenv("MACHINA_UI_DIR", str(ui))
    monkeypatch.setenv("PATH", f"{node.parent}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(launcher.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(launcher.sys, "stdout", SimpleNamespace(isatty=lambda: True))
    return launcher


def test_launch_ui_starts_then_stops_and_registers_atexit(tmp_path, monkeypatch):
    launcher = _fake_ui_env(tmp_path, monkeypatch)
    events: list[str] = []
    stops: list[object] = []

    def _start() -> None:
        events.append("start")

    def _stop() -> None:
        events.append("stop")

    monkeypatch.setattr(launcher, "start_if_enabled", _start)
    monkeypatch.setattr(launcher, "stop_if_enabled", _stop)
    monkeypatch.setattr(launcher.atexit, "register", lambda fn: stops.append(fn))

    assert launcher.launch_ui() == 0
    assert events == ["start", "stop"]
    assert stops == [_stop]


def test_launch_ui_stops_on_keyboard_interrupt(tmp_path, monkeypatch):
    launcher = _fake_ui_env(tmp_path, monkeypatch)
    events: list[str] = []

    def _interrupt(args, env=None):
        raise KeyboardInterrupt

    monkeypatch.setattr(launcher, "start_if_enabled", lambda: events.append("start"))
    monkeypatch.setattr(launcher, "stop_if_enabled", lambda: events.append("stop"))
    monkeypatch.setattr(launcher.subprocess, "call", _interrupt)

    assert launcher.launch_ui() == 130
    assert events == ["start", "stop"]
