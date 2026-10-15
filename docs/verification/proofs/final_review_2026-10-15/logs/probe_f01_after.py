"""F01 GREEN probe: ancestor swap to an outside symlink against the fixed
descriptor-walk write chain (two timings)."""
import asyncio, os, shutil, tempfile
from pathlib import Path

import machina.tools.file as fmod


def setup():
    root = Path(tempfile.mkdtemp(prefix="f01g-"))
    base = root / "ws"
    (base / "a" / "b").mkdir(parents=True)
    (base / "a" / "b" / "file.txt").write_text("INSIDE")
    outside = root / "outside"
    (outside / "b").mkdir(parents=True)
    (outside / "b" / "file.txt").write_text("ORIGINAL")
    fmod._BASE_DIR = base.resolve()
    return root, base, outside


def swap_a(base, outside):
    if os.path.islink(base / "a"):
        return
    os.rename(base / "a", base / ".parking")
    os.symlink(outside, base / "a", target_is_directory=True)


def restore_a(base):
    if os.path.islink(base / "a"):
        os.unlink(base / "a")
    if (base / ".parking").is_dir() and not os.path.islink(base / ".parking"):
        os.rename(base / ".parking", base / "a")


def attack_a(real_open, base, outside, trigger):
    state = {"fired": False}

    def hook(path, flags, *args, **kwargs):
        name = path if isinstance(path, str) else getattr(path, "name", "")
        if not state["fired"] and kwargs.get("dir_fd") is not None:
            if trigger == "component" and name == "a":
                state["fired"] = True
                swap_a(base, outside)
            elif trigger == "temp" and isinstance(name, str) and name.startswith(".file.txt."):
                state["fired"] = True
                swap_a(base, outside)
        return real_open(path, flags, *args, **kwargs)

    hook.state = state  # type: ignore[attr-defined]
    return hook


for trigger in ("component", "temp"):
    root, base, outside = setup()
    real_open = fmod._os_open
    hook = attack_a(real_open, base, outside, trigger)
    fmod._os_open = hook
    try:
        try:
            out = asyncio.run(fmod.write_file("a/b/file.txt", "ATTACKER"))
            result = f"wrote: {out}"
        except Exception as e:  # noqa: BLE001
            result = f"REJECTED: {type(e).__name__}"
    finally:
        fmod._os_open = real_open
        restore_a(base)
    content = (outside / "b" / "file.txt").read_text()
    print(f"[{trigger}] fired={hook.state['fired']} {result} outside={content!r} "
          f"-> {'ESCAPE' if content == 'ATTACKER' else 'no-escape'}")
    shutil.rmtree(root, ignore_errors=True)
