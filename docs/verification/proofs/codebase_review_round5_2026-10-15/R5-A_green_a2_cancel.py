"""R5-A A2 GREEN: cancelling an in-flight machina_chat retires the child.

Same harness as the RED, except the wrapped runner now honors the stop
handle (as the fixed _run_process does via run_bounded(cancel_event=...)).
After handler cancellation the marker must NEVER appear (child killed
promptly) and the handler must surface CancelledError after a bounded join.
"""
import asyncio
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, "/Users/shadow/machina")

from machina import mcp_server

TMP = Path(tempfile.mkdtemp(prefix="r5a-green-"))
MARKER = TMP / "child-survived.txt"


def cancellable_runner(cmd, *, timeout, env_extra=None, cancel_event=None):
    code = (
        "import pathlib, time; time.sleep(10.0); "
        f"pathlib.Path({str(MARKER)!r}).write_text('ran after cancel')"
    )
    return mcp_server._child_process().run_bounded(
        [sys.executable, "-c", code],
        env=mcp_server._sanitized_env(),
        timeout=30.0,
        cwd=TMP,
        max_stdout=mcp_server.MAX_OUTPUT_CHARS,
        max_stderr=mcp_server.MAX_STDERR_CHARS,
        cancel_event=cancel_event,
    )


async def main():
    real = mcp_server._run_process

    def fake(cmd, *, timeout, env_extra=None, cancel_event=None):
        adapted = cancellable_runner(cmd, timeout=timeout, env_extra=env_extra,
                                     cancel_event=cancel_event)
        return mcp_server.ProcessResult(
            returncode=adapted.returncode,
            output=adapted.stdout,
            stderr=adapted.stderr,
            timed_out=adapted.timed_out,
            truncated=adapted.truncated,
        )

    mcp_server._run_process = fake
    try:
        start = time.monotonic()
        task = asyncio.ensure_future(mcp_server.machina_chat("hello"))
        await asyncio.sleep(0.5)
        task.cancel()
        cancelled = False
        try:
            await task
        except asyncio.CancelledError:
            cancelled = True
        join_wait = time.monotonic() - start - 0.5
        # Budget stops too: give the (supposedly dead) child a window in
        # which a survivor would have written its marker.
        await asyncio.sleep(3.0)
        survived = MARKER.exists()
        print(f"cancelled={cancelled} join_wait={join_wait:.2f}s marker={survived}")
        assert cancelled, "handler must still report cancellation"
        assert join_wait < mcp_server.CANCEL_JOIN_SECONDS + 2.0, "join must be bounded"
        assert not survived, "child survived cancellation"
    finally:
        mcp_server._run_process = real


asyncio.run(main())
print("GREEN A2: cancelled chat retired the child tree; no marker, bounded join")
