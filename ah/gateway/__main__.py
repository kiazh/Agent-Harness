"""Entry point: ``python -m ah.gateway``.

stdin/stdout are the protocol channel. Before anything from ``ah`` is imported
they are moved to private, non-inheritable file descriptors, and fds 0/1 are
re-pointed at the null device / stderr. That guarantees:

- a stray ``print()`` or Rich console write lands in stderr, not the JSON stream;
- child processes (``git``, the terminal tool) never inherit the protocol pipes.
  On Windows a child that inherits a pipe while this process has a blocking read
  pending on it hangs at startup until the read completes.
"""

from __future__ import annotations

import os
import sys

sys.stdout.flush()
_protocol_in = os.fdopen(os.dup(0), "rb", buffering=0)
_protocol_out = os.fdopen(os.dup(1), "w", encoding="utf-8", newline="\n")
_null = os.open(os.devnull, os.O_RDONLY)
os.dup2(_null, 0)
os.close(_null)
os.dup2(2, 1)
sys.stdin = open(os.devnull, encoding="utf-8")  # noqa: SIM115 — lives for the process
sys.stdout = sys.stderr

import asyncio  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import threading  # noqa: E402
from typing import Any  # noqa: E402


def _write(frame: dict[str, Any]) -> None:
    try:
        _protocol_out.write(json.dumps(frame, ensure_ascii=False, default=str) + "\n")
        _protocol_out.flush()
    except (OSError, ValueError):
        pass  # the UI went away; the stdin reader sees EOF and we shut down


def _start_stdin_reader(loop: asyncio.AbstractEventLoop, queue: asyncio.Queue[str | None]) -> None:
    """Read protocol lines on a thread (portable across Windows pipes and consoles)."""

    def pump() -> None:
        try:
            for raw in iter(_protocol_in.readline, b""):
                line = raw.decode("utf-8", errors="replace").strip()
                if line:
                    loop.call_soon_threadsafe(queue.put_nowait, line)
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)

    threading.Thread(target=pump, name="ah-gateway-stdin", daemon=True).start()


async def _serve() -> None:
    from ah.gateway.server import Gateway

    gateway = Gateway(_write)
    queue: asyncio.Queue[str | None] = asyncio.Queue()
    _start_stdin_reader(asyncio.get_running_loop(), queue)
    try:
        while not gateway.closing:
            line = await queue.get()
            if line is None:
                break
            await gateway.handle_line(line)
    finally:
        await gateway.close()


def main() -> None:
    logging.basicConfig(
        level=logging.WARNING, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s"
    )
    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
