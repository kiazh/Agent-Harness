"""H14 reproduction — recursive dereferencing copy bypasses containment/credential checks.

Builds an outside private file and a workspace symlink reaching it, asks the
permission policy about `cp -RL source clone`, then performs the copy the
policy authorized and reads the result through the public read_file tool.

Exit 0 = policy requires review for the dereferencing copy (safe);
exit 1 = policy allowed it (escape, the bug).
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from machina.core.config import config  # noqa: E402
from machina.permissions.policy import build_request, decide  # noqa: E402
from machina.permissions.tools import action_for_tool  # noqa: E402
from machina.tools.file import read_file  # noqa: E402

MARKER = "OUTSIDE-PRIVATE-CONTENT-9f2c"


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="h14-"))
    workspace = tmp / "workspace"
    workspace.mkdir()
    outside = tmp / "outside-private"
    outside.mkdir()
    (outside / "private.txt").write_text(MARKER + "\n", encoding="utf-8")

    source = workspace / "source"
    source.mkdir()
    (source / "notes.txt").write_text("public notes\n", encoding="utf-8")
    (source / "data").symlink_to(outside / "private.txt")

    config.workspace_root = str(workspace)
    config.execution_mode = "ask"

    command = "cp -RL source clone"
    request = build_request(tool="terminal", **action_for_tool("terminal", {"command": command}))
    decision = decide(request)
    print(f"policy: {command} -> {decision.verdict} ({decision.reason})")

    # Perform exactly what the policy just classified (the terminal tool would
    # execute this argv on the host).
    proc = subprocess.run(
        ["cp", "-RL", "source", "clone"], cwd=workspace, capture_output=True, text=True
    )
    print(f"cp exit={proc.returncode} stderr={proc.stderr.strip()!r}")
    copied = workspace / "clone" / "data"
    print(f"clone/data is_symlink={copied.is_symlink()} is_file={copied.is_file()}")

    try:
        exposed = asyncio.run(read_file("clone/data"))
    except Exception as error:  # noqa: BLE001 — reported as evidence
        exposed = f"<read_file error: {error}>"
    leaked = MARKER in exposed
    print(f"read_file('clone/data') exposes outside bytes: {leaked}")
    print(f"  content: {exposed.strip().splitlines()[0] if exposed else ''!r}")

    if decision.verdict == "pending":
        print("RESULT: SAFE — the dereferencing copy requires review")
        return 0
    print("RESULT: ESCAPE — policy allowed the dereferencing copy")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
