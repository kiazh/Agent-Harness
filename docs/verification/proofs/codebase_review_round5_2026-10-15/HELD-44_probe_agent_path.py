"""HELD-44 — agent tool-call cancellation path (wait_for timeout + turn cancel).

Mirrors the two cancellation entry points in ``machina/core/agent.py`` for tool
calls, using the exact primitives (``registry.execute`` inside
``asyncio.wait_for`` at :1033; task cancel + gather at :1181-1188) with a
delayed-filesystem gate in the write worker.

A background thread releases the delayed old-turn worker on a wall-clock timer
*after* the newer turn would have started under the unfixed behavior. The newer
edit begins only once the cancelled tool call has returned (i.e. once turn
ownership has been released).

  * Pre-fix: cancellation returns before the worker's replace; the newer edit
    lands, then the orphaned worker's ``os.replace`` lands over it → LATE
    OVERWRITE (RED).
  * Post-fix: cancellation joins the worker before propagating, so the old
    write completes before the newer turn starts → newer edit is the last
    writer (GREEN).

Run pre-fix → exit 1. Post-fix → exit 0.
"""

import asyncio
import shutil
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

import machina.tools.file as fmod  # noqa: E402
from machina.tools.base import registry  # noqa: E402

_RELEASE_AFTER = 0.35


def _install_gate():
    gate_entered = threading.Event()
    gate_release = threading.Event()
    state = {"n": 0}
    real_atomic = fmod._atomic_write_file_at

    def gated_atomic(dir_fd, leaf, content, **kwargs):
        state["n"] += 1
        if state["n"] == 1:
            gate_entered.set()
            gate_release.wait(timeout=10)
        return real_atomic(dir_fd, leaf, content, **kwargs)

    fmod._atomic_write_file_at = gated_atomic
    return gate_entered, gate_release, real_atomic


def _arm_release(gate_release, delay=_RELEASE_AFTER):
    timer = threading.Timer(delay, gate_release.set)
    timer.daemon = True
    timer.start()
    return timer


async def scenario_timeout(base, target):
    gate_entered, gate_release, real_atomic = _install_gate()
    fmod._BASE_DIR = base.resolve()
    try:
        async def call_old():
            return await asyncio.wait_for(
                registry.execute("write_file", path="victim.txt", content="OLD"),
                timeout=0.2,
            )

        task = asyncio.create_task(call_old())
        await asyncio.to_thread(gate_entered.wait, 5)
        timer = _arm_release(gate_release)
        outcome = "?"
        try:
            await task
            outcome = "completed"
        except asyncio.TimeoutError:
            outcome = "timeout"
        # Newer turn starts only after the cancelled tool call returned.
        await registry.execute("write_file", path="victim.txt", content="NEW")
        await asyncio.sleep(0.6)  # give any orphaned worker time to land
        timer.join(timeout=5)
        final = target.read_text()
        print(f"[A wait_for]    outcome={outcome} final={final!r}")
        return final
    finally:
        fmod._atomic_write_file_at = real_atomic


async def scenario_turn_cancel(base, target):
    gate_entered, gate_release, real_atomic = _install_gate()
    fmod._BASE_DIR = base.resolve()
    try:
        task = asyncio.create_task(
            asyncio.wait_for(
                registry.execute("write_file", path="victim.txt", content="OLD"),
                timeout=30,
            )
        )
        await asyncio.to_thread(gate_entered.wait, 5)
        # Turn cancellation: cancel + join exactly like agent.py's finally.
        timer = _arm_release(gate_release)
        task.cancel()
        cancelled = False
        try:
            await task
        except asyncio.CancelledError:
            cancelled = True
        await registry.execute("write_file", path="victim.txt", content="NEW")
        await asyncio.sleep(0.6)
        timer.join(timeout=5)
        final = target.read_text()
        print(f"[B turn-cancel] cancelled={cancelled} final={final!r}")
        return final
    finally:
        fmod._atomic_write_file_at = real_atomic


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="held44-agent-probe-"))
    base = root / "ws"
    base.mkdir()
    target = base / "victim.txt"
    target.write_text("ORIGINAL")

    a = asyncio.run(scenario_timeout(base, target))
    target.write_text("ORIGINAL")
    b = asyncio.run(scenario_turn_cancel(base, target))

    shutil.rmtree(root, ignore_errors=True)
    bad = [name for name, val in (("A", a), ("B", b)) if val != "NEW"]
    if bad:
        print(f"VERDICT: LATE OVERWRITE on scenario(s) {bad}")
        return 1
    print("VERDICT: CONTAINED (newer edit is the last writer in both scenarios)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
