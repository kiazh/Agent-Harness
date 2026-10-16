#!/usr/bin/env python3
"""PTY driver for the integrated workspace (plan §24F native evidence).

Runs a real program inside a real pseudo-terminal at a controlled size,
executes a timed action script (keys/text/resize/snapshots) and writes
rendered screen snapshots through pyte (a terminal emulator), so snapshots
are the actual frame contents — not raw ANSI logs and not a mock renderer.

Usage:
  .venv/bin/python ui/test/support/pty-driver.py \
      --cols 160 --rows 48 --cwd /tmp/fixture-workspace \
      --env MACHINA_PYTHON=/path/.venv/bin/python --env K=V \
      --actions actions.json --outdir /tmp/captures \
      -- node --disable-warning=ExperimentalWarning /path/ui/src/main.ts

actions.json: [{"wait": 3.0} | {"snapshot": "name"} |
               {"send": "\\x10"} | {"text": "main"} | {"resize": [cols, rows]}]
`send` decodes common escapes (\\xNN, \\r, \\n, \\x1b); `text` is literal.

Exit code 0 when the action script completed; snapshots land in --outdir.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import pty
import select
import signal
import struct
import sys
import termios
import time

import pyte


def _decode_send(value: str) -> bytes:
    out = bytearray()
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            if nxt == "x" and i + 3 < len(value) + 1:
                out.append(int(value[i + 2 : i + 4], 16))
                i += 4
                continue
            mapping = {"r": 13, "n": 10, "e": 27, "t": 9, "\\": 92}
            if nxt in mapping:
                out.append(mapping[nxt])
                i += 2
                continue
        out.extend(ch.encode("utf-8"))
        i += 1
    return bytes(out)


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cols", type=int, default=160)
    parser.add_argument("--rows", type=int, default=48)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--env", action="append", default=[])
    parser.add_argument("--actions", required=True)
    parser.add_argument("--outdir", required=True)
    parser.add_argument("--raw-log", default=None, help="also tee the raw ANSI stream here")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("error: no command after --", file=sys.stderr)
        return 2

    with open(args.actions, encoding="utf-8") as handle:
        actions = json.load(handle)

    os.makedirs(args.outdir, exist_ok=True)
    env = dict(os.environ)
    for item in args.env:
        key, _, value = item.partition("=")
        env[key] = value

    screen = pyte.Screen(args.cols, args.rows)
    stream = pyte.Stream(screen)
    raw_file = open(args.raw_log, "wb") if args.raw_log else None

    pid, master = pty.fork()
    if pid == 0:  # child
        try:
            os.chdir(args.cwd)
            os.execvpe(command[0], command, env)
        except Exception as error:  # pragma: no cover - child side
            print(f"pty child failed: {error}", file=sys.stderr)
            os._exit(127)

    _set_winsize(master, args.rows, args.cols)
    deadline = time.monotonic() + 120.0
    pending = list(actions)

    def drain(timeout: float) -> None:
        end = time.monotonic() + timeout
        while True:
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            ready, _, _ = select.select([master], [], [], remaining)
            if not ready:
                return
            try:
                chunk = os.read(master, 65536)
            except OSError:
                return
            if not chunk:
                return
            if raw_file is not None:
                raw_file.write(chunk)
                raw_file.flush()
            stream.feed(chunk.decode("utf-8", errors="replace"))

    try:
        while pending and time.monotonic() < deadline:
            action = pending.pop(0)
            if "wait" in action:
                drain(float(action["wait"]))
            elif "snapshot" in action:
                drain(0.3)
                name = str(action["snapshot"])
                lines = [line.rstrip() for line in screen.display]
                with open(os.path.join(args.outdir, f"{name}.txt"), "w", encoding="utf-8") as handle:
                    handle.write("\n".join(lines) + "\n")
                print(f"snapshot {name}: {args.cols}x{args.rows} -> {name}.txt")
            elif "send" in action:
                try:
                    os.write(master, _decode_send(str(action["send"])))
                except OSError:
                    print("note: pty closed while sending; ending scenario", file=sys.stderr)
                    break
            elif "text" in action:
                try:
                    os.write(master, str(action["text"]).encode("utf-8"))
                except OSError:
                    print("note: pty closed while typing; ending scenario", file=sys.stderr)
                    break
            elif "resize" in action:
                cols, rows = action["resize"]
                _set_winsize(master, int(rows), int(cols))
                screen.resize(int(rows), int(cols))
                drain(0.6)
            else:
                print(f"warning: unknown action {action}", file=sys.stderr)
        # Final snapshot for post-mortem.
        drain(0.5)
        with open(os.path.join(args.outdir, "final.txt"), "w", encoding="utf-8") as handle:
            handle.write("\n".join(line.rstrip() for line in screen.display) + "\n")
    finally:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        for _ in range(20):
            done, _ = os.waitpid(pid, os.WNOHANG)
            if done:
                break
            time.sleep(0.1)
        else:
            try:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            except ProcessLookupError:
                pass
        os.close(master)
        if raw_file is not None:
            raw_file.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
