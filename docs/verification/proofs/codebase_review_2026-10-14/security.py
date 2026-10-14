"""Scratch-only reproductions for the 2026-10-14 security review."""
import asyncio
import os
from pathlib import Path
import subprocess
import tempfile
import time

scratch = Path(tempfile.mkdtemp(prefix="machina-security-review-")).resolve()
os.environ["HOME"] = str(scratch / "home")
os.environ["MACHINA_ENV_FILE"] = str(scratch / "unused.env")

from machina.core.config import config
config.workspace_root = str(scratch)
config.execution_mode = "ask"
config.terminal_sandbox = "disabled"
from machina.permissions import broker, store
from machina.permissions.policy import build_request, decide
from machina.permissions.tools import action_for_tool
from machina.tools.builtins import search_files
from machina.tools.terminal import terminal


async def inspect_action(name, args, execute=False):
    req = build_request(tool=name, **action_for_tool(name, args))
    decision = decide(req)
    print(f"{name} {args!r}: {decision.verdict}; {decision.reason}", flush=True)
    if execute:
        assert decision.verdict == "allowed"
        await broker.permission_broker.guard(req)
        try:
            output = await (search_files(**args) if name == "search_files" else terminal(**args))
            print(f"  OUTPUT {output!r}", flush=True)
        finally:
            broker.clear_execution_context()


async def main():
    print(f"SCRATCH {scratch}", flush=True)
    (scratch / "credentials").write_text("REVIEW_TOKEN=synthetic-credential\n")
    (scratch / "production.env").write_text("REVIEW_TOKEN=synthetic-dotenv\n")
    (scratch / ".env").write_text("REVIEW_TOKEN=synthetic-hidden-dotenv\n")
    await inspect_action("read_file", {"path": "credentials"})
    await inspect_action("terminal", {"command": "cat .env"})
    await inspect_action("search_files", {"pattern": "REVIEW_TOKEN", "path": "."}, True)
    await inspect_action("terminal", {"command": "rg REVIEW_TOKEN .", "timeout": 2}, True)
    await inspect_action("terminal", {"command": "grep -d recurse REVIEW_TOKEN .", "timeout": 2}, True)

    (scratch / "source" / ".git").mkdir(parents=True)
    (scratch / "target" / ".git").mkdir(parents=True)
    (scratch / "source" / ".git" / "config").write_text("attacker-controlled-config\n")
    target_config = scratch / "target" / ".git" / "config"
    target_config.write_text("original-config\n")
    await inspect_action("terminal", {"command": "cp source/.git/config target/.git/config"})
    await inspect_action("terminal", {"command": "cp -R source/. target", "timeout": 2}, True)
    print(f"  TARGET GIT CONFIG {target_config.read_text()!r}", flush=True)

    async def approve(card):
        await store.resolve_decision(card["request_id"], "approved", "review-fixture-human")
        return "approved"

    broker.set_approval_handler(approve, principal="review-fixture-human")
    args = {
        "command": "(sleep 2; exec >/dev/null 2>&1; sleep 2; echo survived > late.txt) &",
        "timeout": 1,
    }
    req = build_request(tool="terminal", **action_for_tool("terminal", args))
    await broker.permission_broker.guard(req)
    marker = scratch / "late.txt"
    started = time.monotonic()
    try:
        output = await terminal(**args)
    finally:
        broker.clear_execution_context()
    print(f"terminal timeout=1 returned after {time.monotonic() - started:.2f}s: {output!r}; is_error={output.is_error}; marker_at_return={marker.exists()}", flush=True)
    await asyncio.sleep(2.5)
    print(f"marker_after_return={marker.exists()}; content={marker.read_text()!r}", flush=True)


asyncio.run(main())
