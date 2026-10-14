#!/usr/bin/env python
"""Audit finding #1: recursive terminal read bypasses credential approval.

Builds the exact broker request the agent loop builds for
`terminal(command="grep -R PRIVATE_TOKEN .")`, asks policy, then executes
the allowed action through the real terminal tool.
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

REPO = Path("/Users/shadow/machina")
sys.path.insert(0, str(REPO))

work = Path(tempfile.mkdtemp(prefix="audit-recursive-"))
(work / ".env").write_text("PRIVATE_TOKEN=review-fixture-secret\n", encoding="utf-8")

from machina.core.config import config

config.workspace_root = str(work)

from machina.permissions.policy import build_request, decide
from machina.permissions.shell import parse_terminal_command

SESSION = "00000000-0000-0000-0000-0000000000aa"


def req_for(cmd: str):
    parsed = parse_terminal_command(cmd)
    return build_request(
        operation="process.exec",
        argv=parsed.argv,
        shell_payload=parsed.payload,
        cwd=".",
        mode="ask",
        backend="host",
        tool="terminal",
        session_id=SESSION,
        timeout=60,
    )


r_cat = req_for("cat .env")
r_grep = req_for("grep -R PRIVATE_TOKEN .")
d_cat = decide(r_cat, [])
d_grep = decide(r_grep, [])
print(f"decision: cat .env          -> {d_cat.verdict} ({d_cat.reason})")
print(f"decision: grep -R TOKEN .   -> {d_grep.verdict} ({d_grep.reason})")
assert d_cat.verdict == "pending", "expected explicit .env read to ask"
assert d_grep.verdict == "allowed", "policy layer did not allow; can't continue"

# Execute through the real terminal tool with the broker-stamped snapshot,
# exactly as the agent loop does after guard() returns allowed.
async def run():
    from machina.permissions.broker import _stamp_execution
    from machina.tools.base import registry

    import machina.tools.terminal  # noqa: F401  (registers the tool)

    _stamp_execution(r_grep)
    return await registry.execute(
        "terminal", command="grep -R PRIVATE_TOKEN .", workdir="."
    )


out = asyncio.run(run())
print("actual terminal tool output:")
print(out)
leaked = "review-fixture-secret" in str(out)
print(f"secret present in tool result: {leaked}")
need_fail = not leaked
sys.exit(1 if need_fail else 0)
