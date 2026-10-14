import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "/Users/shadow/machina")

work = Path(tempfile.mkdtemp(prefix="audit-redos-"))
(work / "evil.txt").write_text("a" * 36 + "!\n", encoding="utf-8")

from machina.core.config import config
config.workspace_root = str(work)

from machina.tools.base import registry
import machina.tools.builtins  # noqa: F401


async def main():
    result = await registry.execute("search_files", pattern="(a+)+$", path=str(work))
    print("completed:", result[:80])


asyncio.run(main())
