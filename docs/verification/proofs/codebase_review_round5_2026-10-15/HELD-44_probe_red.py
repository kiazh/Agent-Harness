"""HELD-44 (finding 4) — cancelled file writes can overwrite later edits.

Reproduction harness. A delayed-filesystem gate pins the claimed interleave:

  1. Turn 1 starts write_file(content="OLD"); its worker thread reaches the
     filesystem gate and blocks there (delay).
  2. Turn 1 is cancelled (timeout / turn cancellation) and the awaiting
     coroutine returns (ownership released).
  3. Turn 2 (newer edit) writes content="NEW" and it lands.
  4. A wall-clock timer releases the delayed turn-1 worker; it performs the
     real os.replace with "OLD".

Pre-fix: the old turn's replace lands *after* the newer edit → final content is
"OLD" (late overwrite). Post-fix: cancellation joins the worker before
propagating, so the old write completes before turn 2 starts and the newer edit
is the last writer.

No writer logic is mocked: the gate only delays the real atomic-write call in
the worker thread; the actual syscalls (temp create, rename) are real.
"""

import asyncio
import shutil
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

import machina.tools.file as fmod  # noqa: E402


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="held44-probe-"))
    base = root / "ws"
    base.mkdir()
    target = base / "victim.txt"
    target.write_text("ORIGINAL")

    fmod._BASE_DIR = base.resolve()

    gate_entered = threading.Event()
    gate_release = threading.Event()
    call_count = {"n": 0}
    real_atomic = fmod._atomic_write_file_at

    def gated_atomic(dir_fd, leaf, content, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # First (old-turn) write: hold inside the worker thread.
            gate_entered.set()
            gate_release.wait(timeout=5)
        return real_atomic(dir_fd, leaf, content, **kwargs)

    fmod._atomic_write_file_at = gated_atomic

    async def scenario():
        # Turn 1: old write, cancelled while its worker is delayed.
        t1 = asyncio.create_task(fmod.write_file("victim.txt", "OLD"))
        entered = await asyncio.to_thread(gate_entered.wait, 5)
        assert entered, "old worker never reached the filesystem gate"
        timer = threading.Timer(0.35, gate_release.set)
        timer.daemon = True
        timer.start()
        t1.cancel()
        cancelled = False
        try:
            await t1
        except asyncio.CancelledError:
            cancelled = True

        # Turn 2: newer edit (ownership released) lands.
        await fmod.write_file("victim.txt", "NEW")
        after_new = target.read_text()
        # Let any orphaned worker land, then let the timer clean up.
        await asyncio.sleep(0.6)
        timer.join(timeout=5)
        final = target.read_text()

        print(f"turn-1 cancelled={cancelled} after_new={after_new!r} final={final!r}")
        if final == "OLD":
            print("VERDICT: LATE OVERWRITE (old cancelled write clobbered newer edit)")
            return 1
        if final == "NEW":
            print("VERDICT: CONTAINED (newer edit is the last writer)")
            return 0
        print(f"VERDICT: UNEXPECTED ({final!r})")
        return 2

    try:
        return asyncio.run(scenario())
    finally:
        fmod._atomic_write_file_at = real_atomic
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
