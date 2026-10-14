#!/usr/bin/env python
"""Audit finding #3: tool-cache results cross workspace boundaries.

read_file('report.txt') is cached by (tool name, args) only. Two workspaces
hold different report.txt contents; the registry call in workspace B returns
workspace A's cached content while a direct (uncached) call returns B's.
"""
import asyncio
import sys
import tempfile
from pathlib import Path

REPO = Path("/Users/shadow/machina")
sys.path.insert(0, str(REPO))

work_a = Path(tempfile.mkdtemp(prefix="audit-ws-a-"))
work_b = Path(tempfile.mkdtemp(prefix="audit-ws-b-"))
(work_a / "report.txt").write_text("workspace-a-content\n", encoding="utf-8")
(work_b / "report.txt").write_text("workspace-b-content\n", encoding="utf-8")

from machina.core.config import config

import machina.tools.file  # noqa: F401  (registers read_file)
from machina.tools.base import registry
from machina.tools.file import read_file


async def main() -> None:
    config.workspace_root = str(work_a)
    via_registry_a = await registry.execute("read_file", path="report.txt")
    direct_a = await read_file("report.txt")

    config.workspace_root = str(work_b)
    via_registry_b = await registry.execute("read_file", path="report.txt")
    direct_b = await read_file("report.txt")

    print(f"workspace A: registry read -> {via_registry_a!r}")
    print(f"workspace A: direct   read -> {direct_a!r}")
    print(f"workspace B: registry read -> {via_registry_b!r}")
    print(f"workspace B: direct   read -> {direct_b!r}")
    print("cache keys:", list(registry._result_cache.keys()))

    stale = "workspace-a-content" in via_registry_b and "workspace-b-content" in direct_b
    print("CLAIM CONFIRMED (B registry read returned A content)"
          if stale else "CLAIM NOT REPRODUCED")
    sys.exit(0 if stale else 1)


asyncio.run(main())
