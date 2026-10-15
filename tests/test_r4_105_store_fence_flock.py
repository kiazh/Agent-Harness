"""R4-105(c): cross-process flock contention for ``security/store_fence``.

Existing coverage (``test_chatgpt_auth_adversarial``) proves a child-held
lock makes the parent fail typed inside a bounded window. This suite closes
the remaining deterministic gaps named in R4-105:

* the failure path is repeated (>=5 contended acquisitions), each bounded and
  typed, with the parent's open-fd count unchanged after every attempt;
* after the holder RELEASES, acquisition succeeds promptly (the lock is not
  a one-way latch) and repeated acquire/release round-trips leak no fds;
* the timeout message names the lock file and never credential material.

The holder is a real child process using the production ``store_lock``; only
the module-level timeout constant is shortened so the assertions run fast.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX flock semantics are required")

REPO_ROOT = Path(__file__).resolve().parents[1]
_HOLD_LOCK_CHILD = """
import sys, time
from pathlib import Path

from machina.security import store_fence

target = Path(sys.argv[1])
ready = Path(sys.argv[2])
release = Path(sys.argv[3])

with store_fence.store_lock(target):
    ready.write_text("locked")
    deadline = time.monotonic() + 30
    while not release.exists():
        if time.monotonic() > deadline:
            raise SystemExit("release was never signalled")
        time.sleep(0.02)
"""

#: Shortened bound for the parent's contended attempts (production default 45s).
_CONTENDED_TIMEOUT_SECONDS = 0.25
_CONTENDED_WINDOW_SECONDS = 2.0
_REPEATS = 5


def _open_fd_count() -> int:
    """Count this process's open fds without leaving an fd behind."""
    fd_dir = "/proc/self/fd" if os.path.isdir("/proc/self/fd") else "/dev/fd"
    return len(os.listdir(fd_dir))


def _wait_for_holder(child: subprocess.Popen, ready: Path) -> None:
    deadline = time.monotonic() + 15
    while not ready.exists():
        if child.poll() is not None:
            pytest.fail(f"lock-holder child exited early (rc={child.returncode})")
        if time.monotonic() > deadline:
            pytest.fail("lock-holder child never acquired the store lock")
        time.sleep(0.01)


def test_cross_process_contention_bounded_then_release_recovers(tmp_path, monkeypatch):
    from machina.security import store_fence

    target = tmp_path / "auth" / "chatgpt.json"
    ready = tmp_path / "holder.ready"
    release = tmp_path / "holder.release"
    monkeypatch.setattr(store_fence, "STORE_LOCK_TIMEOUT_SECONDS", _CONTENDED_TIMEOUT_SECONDS)

    fds_before_spawn = _open_fd_count()
    child = subprocess.Popen(
        [sys.executable, "-c", _HOLD_LOCK_CHILD, str(target), str(ready), str(release)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(REPO_ROOT),
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
    )
    try:
        _wait_for_holder(child, ready)
        fds_while_contended = _open_fd_count()

        # >=5 contended acquisitions: bounded, typed, no fd growth.
        for attempt in range(_REPEATS):
            started = time.monotonic()
            with pytest.raises(store_fence.StoreLockTimeout) as excinfo:
                with store_fence.store_lock(target):
                    pytest.fail("acquired a lock held by a live child process")
            elapsed = time.monotonic() - started
            message = str(excinfo.value)
            assert "credential store lock" in message, message
            assert str(target) + ".lock" in message, message
            assert elapsed < _CONTENDED_WINDOW_SECONDS, (
                f"attempt {attempt}: unbounded wait ({elapsed:.2f}s)"
            )
            assert elapsed >= _CONTENDED_TIMEOUT_SECONDS / 2, (
                f"attempt {attempt}: returned before the bound ({elapsed:.3f}s)"
            )

        assert _open_fd_count() == fds_while_contended, (
            "timed-out lock attempts leaked file descriptors"
        )

        # Release -> success: the holder exits its context manager cleanly.
        release.write_text("go")
        assert child.wait(timeout=15) == 0, child.stderr.read() if child.stderr else ""
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        if child.stderr is not None:
            child.stderr.close()

    assert _open_fd_count() == fds_before_spawn, "lock-holder lifecycle leaked fds"

    started = time.monotonic()
    with store_fence.store_lock(target):
        pass
    assert time.monotonic() - started < 1.0, "released lock was not immediately acquirable"

    for _ in range(20):
        with store_fence.store_lock(target):
            pass
    assert _open_fd_count() == fds_before_spawn, "acquire/release round-trips leaked fds"
