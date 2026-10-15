"""H18 reproduction — search_files re-opens a validated pathname in the matcher.

The parent validates `link.txt` (an in-workspace symlink) and passes the
original pathname to the isolated matcher.  A controlled swap performed at
the spawn boundary — after validation, before the matcher's open() — retargets
the link to an outside private file.

Exit 0 = the outside bytes stay hidden (safe);
exit 1 = the search exposed the outside bytes (escape, the bug).
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from machina.core.config import config  # noqa: E402
from machina.tools import builtins  # noqa: E402

MARKER = "OUTSIDE-PRIVATE-CONTENT-9f2c"


async def run() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="h18-"))
    workspace = tmp / "workspace"
    workspace.mkdir()
    outside = tmp / "outside"
    outside.mkdir()
    (outside / "private.txt").write_text(MARKER + "\n", encoding="utf-8")
    (workspace / "notes.txt").write_text("public notes\n", encoding="utf-8")
    link = workspace / "link.txt"
    link.symlink_to(workspace / "notes.txt")

    config.workspace_root = str(workspace)

    real_create = asyncio.create_subprocess_exec
    swapped = False

    async def create_with_swap(*args, **kwargs):
        nonlocal swapped
        if not swapped:
            swapped = True
            link.unlink()
            link.symlink_to(outside / "private.txt")
        return await real_create(*args, **kwargs)

    asyncio.create_subprocess_exec = create_with_swap  # type: ignore[assignment]
    try:
        # Search a substring of the outside line so the no-match report cannot
        # be confused with the leaked content line itself.
        result = await builtins.search_files("PRIVATE-CONTENT", ".")
    finally:
        asyncio.create_subprocess_exec = real_create  # type: ignore[assignment]

    print(f"controlled swap fired: {swapped}")
    print(f"link.txt now targets: {link.resolve()}")
    print(f"search result: {result!r}")
    if MARKER in result:
        print("RESULT: ESCAPE — the matcher followed the swapped pathname")
        return 1
    print("RESULT: SAFE — the swapped pathname was not followed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
