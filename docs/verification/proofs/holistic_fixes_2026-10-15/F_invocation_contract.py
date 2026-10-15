"""LANE F: real-module invocation-contract check (no real stack, no VM touched).

Runs the real ``machina.core.firecrawl_lifecycle`` against a temp HOME:

1. default config (``firecrawl_autostart`` off) -> both calls no-op, nothing on disk;
2. autostart on but ``<home>/up.sh`` missing    -> warn-once, no process, no marker;
3. autostart on + stub stack                    -> detached chain runs in order,
   and two concurrent *processes* exercise the cross-process refcount: the first
   stop keeps the stack while the peer lives, the peer's stop (last marker)
   tears down (stub up.sh down -> stub limactl stop docker) and prunes strays.

Usage:  .venv/bin/python docs/verification/proofs/holistic_fixes_2026-10-15/F_invocation_contract.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="machina-f-lifecycle-"))
os.environ["HOME"] = str(TMP / "home")
os.environ.pop("DOCKER_HOST", None)
TMP.joinpath("home").mkdir()

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO))

from machina.core import firecrawl_lifecycle as fl  # noqa: E402
from machina.core.config import config  # noqa: E402

failures: list[str] = []

PEER_SOURCE = """
import os, sys, time
sys.path.insert(0, sys.argv[1])
from machina.core.config import config
from machina.core import firecrawl_lifecycle as fl
config.firecrawl_autostart = True
config.firecrawl_stop_on_exit = True
config.firecrawl_home = os.environ["F_FIRECRAWL_HOME"]
assert fl.start_if_enabled() is True
time.sleep(5)  # stay alive so the other session's stop sees a live peer
print(f"peer-stop:{fl.stop_if_enabled()}", flush=True)
"""


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if condition else 'FAIL'} {label}{' — ' + detail if detail else ''}")
    if not condition:
        failures.append(label)


def wait_for(predicate, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def wait_lines(trace: Path, count: int, timeout: float = 15.0) -> list[str]:
    wait_for(lambda: trace.exists() and len(trace.read_text().splitlines()) >= count, timeout)
    return trace.read_text(encoding="utf-8").splitlines() if trace.exists() else []


def log_text() -> str:
    path = TMP / "home" / ".machina" / "logs" / "firecrawl-lifecycle.log"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def dead_pid() -> int:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=15)
    return child.pid


print(f"temp HOME: {os.environ['HOME']}")

print("\n[1] default config (autostart off)")
config.firecrawl_autostart = False
check("start_if_enabled() is False", fl.start_if_enabled() is False)
check("stop_if_enabled() is False", fl.stop_if_enabled() is False)
check("no ~/.machina created", not (TMP / "home" / ".machina").exists())

print("\n[2] autostart on, up.sh missing -> warn once, never raise")
firecrawl = TMP / "home" / "firecrawl"
firecrawl.mkdir()
config.firecrawl_autostart = True
config.firecrawl_home = str(firecrawl)
check("first start False", fl.start_if_enabled() is False)
check("second start False", fl.start_if_enabled() is False)
check("no marker", list((TMP / "home" / ".machina" / "run").glob("*.marker")) == [])
check("warned exactly once", log_text().count("does not exist") == 1)

print("\n[3] autostart on + stub stack -> start, cross-process refcount, last-session stop")
trace = TMP / "trace.log"
bin_dir = TMP / "bin"
bin_dir.mkdir()
up_script = firecrawl / "up.sh"
up_script.write_text(
    f'#!/bin/sh\necho "up.sh $* docker_host=${{DOCKER_HOST:-unset}}" >> "{trace}"\nexit 0\n',
    encoding="utf-8",
)
up_script.chmod(0o644)  # mirrors the user's real 0644 launcher (no exec bit)
limactl = bin_dir / "limactl"
limactl.write_text(f'#!/bin/sh\necho "limactl $*" >> "{trace}"\nexit 0\n', encoding="utf-8")
limactl.chmod(0o755)
os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"

socket = f"unix://{os.environ['HOME']}/.lima/docker/sock/docker.sock"
check("start_if_enabled() is True", fl.start_if_enabled() is True)
lines = wait_lines(trace, 2)
check(
    "limactl then up.sh, in order",
    lines[:2] == ["limactl start docker", f"up.sh up docker_host={socket}"],
    repr(lines[:2]),
)
marker = fl._marker_path(os.getpid())
check(
    "marker for this PID", marker.exists() and json.loads(marker.read_text())["pid"] == os.getpid()
)

peer_env = {**os.environ, "F_FIRECRAWL_HOME": str(firecrawl)}
peer = subprocess.Popen(
    [sys.executable, "-c", PEER_SOURCE, str(REPO)],
    cwd=str(REPO),
    env=peer_env,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
)
peer_marker = fl._marker_path(peer.pid)
check("peer session started (its marker appears)", wait_for(peer_marker.exists))
wait_lines(trace, 4)
stale_marker = fl._marker_path(dead_pid())
stale_marker.write_text("{}", encoding="utf-8")

check("first session's stop keeps the live peer's stack", fl.stop_if_enabled() is False)
check("peer marker retained", peer_marker.exists())
check("our marker consumed", not marker.exists())
check("stale marker pruned", not stale_marker.exists())
check("no teardown traced while the peer lives", len(trace.read_text().splitlines()) == 4)

peer_out, _ = peer.communicate(timeout=30)
check(
    "peer stopped cleanly", peer.returncode == 0 and "peer-stop:True" in peer_out, peer_out.strip()
)
lines = wait_lines(trace, 6)
check(
    "last session's teardown: up.sh down then limactl stop docker",
    lines[4:6] == [f"up.sh down docker_host={socket}", "limactl stop docker"],
    repr(lines[4:6]),
)
check("peer marker consumed by its own stop", not peer_marker.exists())
check("repeat stop (atexit net) no-ops", fl.stop_if_enabled() is False)

print("\nlifecycle log tail:")
print("\n".join(f"    {line}" for line in log_text().splitlines()[-8:]))

print("\nRESULT:", "PASS" if not failures else f"FAIL {failures}")
raise SystemExit(0 if not failures else 1)
