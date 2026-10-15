"""F13 RED probe: resolve_path / read_file against a writer-less FIFO.
Runs the (blocking) call in a daemon thread so a hang is observable."""
import asyncio, os, tempfile, threading, time
from pathlib import Path

import machina.tools.file as fmod

root = Path(tempfile.mkdtemp(prefix="f13-"))
base = root / "ws"
base.mkdir()
fifo = base / "pipe"
os.mkfifo(fifo)
fmod._BASE_DIR = base.resolve()

result = {}


def run_resolve():
    t0 = time.monotonic()
    try:
        fmod.resolve_path("pipe")
        result["resolve"] = ("ok", time.monotonic() - t0)
    except Exception as e:  # noqa: BLE001
        result["resolve"] = (type(e).__name__, time.monotonic() - t0)


def run_read():
    t0 = time.monotonic()
    try:
        asyncio.run(fmod.read_file("pipe", max_size=1024))
        result["read"] = ("ok", time.monotonic() - t0)
    except Exception as e:  # noqa: BLE001
        result["read"] = (type(e).__name__, time.monotonic() - t0)


for name, fn in (("resolve", run_resolve), ("read", run_read)):
    th = threading.Thread(target=fn, daemon=True)
    th.start()
    th.join(timeout=3.0)
    if th.is_alive():
        print(f"{name}: HANG (>3s, blocked in open) — event loop would freeze")
    else:
        print(f"{name}: {result.get(name)}")
