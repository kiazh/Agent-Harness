"""F01 RED probe: ancestor swapped to a symlink between containment check and
lstat/open in the dir-fd write chain. Runs against the pre-fix code."""
import asyncio, os, tempfile
from pathlib import Path

import machina.tools.file as fmod

root = Path(tempfile.mkdtemp(prefix="f01-"))
base = root / "ws"
(base / "a" / "b").mkdir(parents=True)
outside = root / "outside"
(outside / "b").mkdir(parents=True)
(outside / "b" / "file.txt").write_text("ORIGINAL")
fmod._BASE_DIR = base.resolve()

real = fmod._realpath_inside_base
state = {"swapped": False}


def hooked(path):
    result = real(path)
    if not state["swapped"] and Path(path).name == "b":
        state["swapped"] = True
        os.rename(base / "a", base / ".parking")
        os.symlink(outside, base / "a", target_is_directory=True)
    return result


fmod._realpath_inside_base = hooked
try:
    out = asyncio.run(fmod.write_file("a/b/file.txt", "ATTACKER"))
    print("write result:", out)
except Exception as e:  # noqa: BLE001
    print("REJECTED:", type(e).__name__, e)
print("swap fired:", state["swapped"])
print("outside content:", (outside / "b" / "file.txt").read_text())
print("ESCAPE" if (outside / "b" / "file.txt").read_text() == "ATTACKER" else "no-escape")
