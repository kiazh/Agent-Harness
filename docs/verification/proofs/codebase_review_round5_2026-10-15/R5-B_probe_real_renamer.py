"""R5-B adversarial probe: real concurrent renamer vs write_file atomic path.

Primary evidence for finding 3 (atomic-write directory escape). A real
background thread continuously renames the validated workspace subdirectory
to a symlink pointing outside the base, while the real ``write_file`` tool
completion runs against it. Records:

* whether attacker content ever lands outside the workspace,
* whether temp files leak outside,
* how many writes succeeded (availability).

Run against pre-fix code (RED) and post-fix code (GREEN); the fix must show
zero escapes and zero outside temp files. No seam is mocked — real
``os.rename`` / ``os.symlink`` / real ``write_file``.
"""

import asyncio
import os
import shutil
import tempfile
import threading
from pathlib import Path

import machina.tools.file as fmod

N = 300


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="r5b-probe-"))
    base = tmp / "ws"
    sub = base / "sub"
    parking = base / ".sub_parking"
    outside = tmp / "outside"
    (base / "sub").mkdir(parents=True)
    outside.mkdir()
    (outside / "victim.txt").write_text("ORIGINAL")
    (sub / "victim.txt").write_text("inside-original")

    fmod._BASE_DIR = base.resolve()
    stop = threading.Event()

    def flipper():
        to_link = True
        while not stop.is_set():
            try:
                if to_link:
                    if os.path.islink(sub) or not os.path.exists(sub) or parking.exists():
                        to_link = False
                        continue
                    shutil.rmtree(parking, ignore_errors=True)
                    os.rename(sub, parking)
                    os.symlink(outside, sub, target_is_directory=True)
                    # Hold the swapped state briefly so in-flight writes
                    # interleave with it (a real adversarial actor with a
                    # slower cadence than the writer).
                    stop.wait(0.002)
                else:
                    if os.path.islink(sub):
                        os.unlink(sub)
                    if os.path.isdir(parking) and not os.path.islink(parking):
                        if not os.path.exists(sub):
                            os.rename(parking, sub)
                    stop.wait(0.002)
                to_link = not to_link
            except OSError:
                pass

    thread = threading.Thread(target=flipper, daemon=True)
    thread.start()
    ok = rejected = 0
    escaped_at = None
    try:
        for i in range(N):
            try:
                asyncio.run(fmod.write_file("sub/victim.txt", f"ATTACKER-{i}"))
                ok += 1
            except Exception:
                rejected += 1
            try:
                content = (outside / "victim.txt").read_text()
            except OSError:
                content = "ORIGINAL"
            if content != "ORIGINAL" and escaped_at is None:
                escaped_at = (i, content)
    finally:
        stop.set()
        thread.join(timeout=10)

    outside_files = sorted(os.listdir(outside))
    litter = [n for n in outside_files if n.endswith(".tmp")]
    print(f"writes_ok={ok} rejected={rejected} escaped_at={escaped_at}")
    print(f"outside/victim.txt={(outside / 'victim.txt').read_text()!r}")
    print(f"outside_files={outside_files}")
    print(f"outside_temp_litter={len(litter)}")
    escape = escaped_at is not None or litter or (outside / "victim.txt").read_text() != "ORIGINAL"
    print("VERDICT: ESCAPE" if escape else "VERDICT: CONTAINED")
    shutil.rmtree(tmp, ignore_errors=True)
    return 1 if escape else 0


if __name__ == "__main__":
    raise SystemExit(main())
