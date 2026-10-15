"""Stdlib-shape probe: SIGINT vs. _run's worker-thread wait (R4-017).

Usage: python sigint_probe.py threadpool|mainloop
Prints when the coroutine's finally runs and when KeyboardInterrupt surfaces.
"""

import asyncio
import concurrent.futures
import os
import signal
import sys
import threading
import time

mode = sys.argv[1]


def run_old(coro):
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def run_new(coro):
    return asyncio.run(coro)


async def main():
    t0 = time.monotonic()
    try:
        await asyncio.sleep(3.0)
    finally:
        print(f"cleanup ran after {time.monotonic() - t0:.1f}s", flush=True)


def alarm():
    time.sleep(0.5)
    os.kill(os.getpid(), signal.SIGINT)


threading.Thread(target=alarm, daemon=True).start()
t0 = time.monotonic()
try:
    run_old(main()) if mode == "threadpool" else run_new(main())
except KeyboardInterrupt:
    print(f"raised KeyboardInterrupt after {time.monotonic() - t0:.1f}s", flush=True)
