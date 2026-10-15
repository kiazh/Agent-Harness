"""R5-B deterministic probe: pin the claimed interleaving exactly.

Validates the target (resolve + probe on a real directory) and then, at the
temp-file-creation seam inside the atomic writer, performs the REAL
rename-to-symlink swap a concurrent local actor could perform, keeping it
through the final replace. Checks whether attacker content lands outside.

Pre-fix the swap is fired by monkeypatching ``tempfile.mkstemp`` (the seam
that pathname-based writer used). Post-fix the temp file is created via
openat anchored to the validated dir fd, so the probe instead hooks the
module ``_os_open`` and fires on the dir_fd-anchored temp open — the
equivalent interleaving point for the fixed code. Either way the observable
is identical: does the write land outside the base?
"""

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

import machina.tools.file as fmod


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="r5b-det-"))
    base = tmp / "ws"
    sub = base / "sub"
    parking = base / ".sub_parking"
    outside = tmp / "outside"
    (base / "sub").mkdir(parents=True)
    outside.mkdir()
    (outside / "victim.txt").write_text("ORIGINAL")
    (sub / "victim.txt").write_text("inside-original")

    fmod._BASE_DIR = base.resolve()
    state = {"swapped": False}

    def _swap():
        shutil.rmtree(parking, ignore_errors=True)
        os.rename(sub, parking)
        os.symlink(outside, sub, target_is_directory=True)
        state["swapped"] = True

    real_mkstemp = tempfile.mkstemp
    real_open = fmod._os_open

    def hooked_mkstemp(*a, **k):
        if not state["swapped"]:
            _swap()
        return real_mkstemp(*a, **k)

    def hooked_open(path, flags, *a, **k):
        name = path if isinstance(path, str) else getattr(path, "name", "")
        if (
            not state["swapped"]
            and k.get("dir_fd") is not None
            and isinstance(name, str)
            and name.startswith(".victim.txt.")
            and name.endswith(".tmp")
        ):
            _swap()
        return real_open(path, flags, *a, **k)

    # Hook both seams so the same probe works pre-fix and post-fix.
    fmod.tempfile.mkstemp = hooked_mkstemp
    fmod._os_open = hooked_open
    try:
        result = asyncio.run(fmod.write_file("sub/victim.txt", "ATTACKER"))
        print("write returned:", result)
    except Exception as e:  # noqa: BLE001
        print("write raised:", type(e).__name__, e)

    outside_files = sorted(os.listdir(outside))
    leaked = outside_files != ["victim.txt"] or (outside / "victim.txt").read_text() != "ORIGINAL"
    print(f"swapped={state['swapped']} outside/victim.txt={(outside / 'victim.txt').read_text()!r}")
    print(f"outside_files={outside_files}")
    print("VERDICT: ESCAPE" if leaked else "VERDICT: CONTAINED")
    shutil.rmtree(tmp, ignore_errors=True)
    return 1 if leaked else 0


if __name__ == "__main__":
    raise SystemExit(main())
