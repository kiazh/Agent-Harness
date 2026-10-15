"""R5-A A2 RED: cancelling an in-flight machina_chat leaves the child alive.

Spawns the REAL machina_chat path with _run_process wrapped around the REAL
_run_process (same timeout/kill semantics) but running a slow marker command
instead of `machina chat` (no DB/provider needed — the seam under test is the
await-without-stop-handle at mcp_server:474, not the CLI). Cancels the handler
task mid-flight, then watches: if the child survives cancellation and writes
its marker, finding 15 is reproduced.
"""
import asyncio
import contextlib
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, "/Users/shadow/machina")

from machina import mcp_server

TMP = Path(tempfile.mkdtemp(prefix="r5a-red-"))
MARKER = TMP / "child-survived.txt"


def slow_real_runner(cmd, *, timeout, env_extra=None):
    code = (
        "import pathlib, time; time.sleep(2.0); "
        f"pathlib.Path({str(MARKER)!r}).write_text('ran after cancel')"
    )
    return mcp_server._child_process().run_bounded(
        [sys.executable, "-c", code],
        env=mcp_server._sanitized_env(),
        timeout=30.0,
        cwd=TMP,
        max_stdout=mcp_server.MAX_OUTPUT_CHARS,
        max_stderr=mcp_server.MAX_STDERR_CHARS,
    )


async def main() -> bool:
    real = mcp_server._run_process

    def fake(cmd, *, timeout, env_extra=None):
        adapted = slow_real_runner(cmd, timeout=timeout, env_extra=env_extra)
        return mcp_server.ProcessResult(
            returncode=adapted.returncode,
            output=adapted.stdout,
            stderr=adapted.stderr,
            timed_out=adapted.timed_out,
            truncated=adapted.truncated,
        )

    mcp_server._run_process = fake
    try:
        task = asyncio.ensure_future(mcp_server.machina_chat("hello"))
        await asyncio.sleep(0.5)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        # The handler is gone; give the (supposedly retired) child time to
        # prove it is still alive by writing its marker.
        deadline = time.monotonic() + 6.0
        while time.monotonic() < deadline:
            if MARKER.exists():
                break
            await asyncio.sleep(0.1)
        survived = MARKER.exists()
        print("handler cancelled; child marker exists:", survived)
        return survived
    finally:
        mcp_server._run_process = real


survived = asyncio.run(main())
assert survived, "expected the child to survive handler cancellation pre-fix"
print("RED A2 REPRODUCED: cancelled machina_chat left the child running (marker written)")
