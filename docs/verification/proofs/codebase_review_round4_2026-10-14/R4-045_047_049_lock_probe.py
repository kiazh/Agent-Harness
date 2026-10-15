"""R4-049 probe: contended provider-store lock across processes.

Holds the real ``flock`` on the providers store lock file in a child
process, then runs ``store.add_provider`` in another child twice: once with
a compressed bound and once with the production bound. Prints the typed
error, elapsed time and leaked descriptors as JSON lines.

Usage: .venv/bin/python R4-045_047_049_lock_probe.py <providers_file>
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]

HOLDER = textwrap.dedent(
    """
    import fcntl, os, sys, time
    lock_path, ready_path = sys.argv[1], sys.argv[2]
    os.makedirs(os.path.dirname(lock_path), mode=0o700, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    open(ready_path, "w").write("held")
    time.sleep(120)
    """
)

ADD = textwrap.dedent(
    """
    import json, os, sys, time
    providers_file = sys.argv[1]
    timeout_override = float(sys.argv[2])
    os.environ["MACHINA_PROVIDERS_FILE"] = providers_file
    def open_fds():
        try:
            return {int(n) for n in os.listdir("/dev/fd") if n.isdigit()}
        except OSError:
            return None
    from machina.security import providers as store
    if timeout_override:
        store._STORE_LOCK_TIMEOUT_SECONDS = timeout_override
    before = open_fds()
    started = time.monotonic()
    error = None
    try:
        store.add_provider("contended", "http://127.0.0.1:9000/v1")
    except BaseException as exc:
        error = {"type": type(exc).__name__, "message": str(exc)}
    elapsed = time.monotonic() - started
    after = open_fds()
    leaked = sorted(after - before) if before is not None and after is not None else None
    print(json.dumps({"error": error, "elapsed": round(elapsed, 3), "leaked_fds": leaked}))
    """
)


def main(providers_file: Path) -> int:
    lock_path = Path(str(providers_file) + ".lock")
    ready = providers_file.with_suffix(".ready")
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(lock_path), str(ready)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 10
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ready.exists(), "holder never acquired the lock"
    env = {**os.environ, "MACHINA_PROVIDERS_FILE": str(providers_file), "PYTHONPATH": str(ROOT)}
    try:
        for override in ("0.4", "0"):
            try:
                completed = subprocess.run(
                    [sys.executable, "-c", ADD, str(providers_file), override],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    env=env,
                    cwd=str(ROOT),
                )
            except subprocess.TimeoutExpired:
                print(json.dumps({"override": override, "result": "BLOCKED_PAST_15s"}))
                return 1
            print(f"override={override or 'default'} {completed.stdout.strip()}")
        print(json.dumps({"store_file_created": providers_file.exists()}))
        return 0
    finally:
        holder.kill()
        holder.wait(timeout=10)


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
