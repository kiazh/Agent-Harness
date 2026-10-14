#!/usr/bin/env python
"""Audit finding: a custom tool declaring effects=('exec',) is cached.

Cache eligibility is derived from tool-name prefixes/sets, ignoring the
tool's declared effects, so the second identical call never executes.
"""
import asyncio
import sys
from pathlib import Path

REPO = Path("/Users/shadow/machina")
sys.path.insert(0, str(REPO))

from machina.tools.base import registry

executions: list[str] = []


@registry.register(
    name="audit_effects_tool",
    description="audit-only tool declaring exec effects",
    parameters={
        "type": "object",
        "properties": {"argument": {"type": "string"}},
        "required": ["argument"],
    },
    effects=("exec",),
)
def audit_effects_tool(argument: str) -> str:
    executions.append(argument)
    return f"executed {argument!r} (run #{len(executions)})"


async def main() -> None:
    first = await registry.execute("audit_effects_tool", argument="same")
    second = await registry.execute("audit_effects_tool", argument="same")
    print("first :", first)
    print("second:", second)
    print("declared effects:", registry._tools["audit_effects_tool"].effects)
    print("actual executions:", len(executions), executions)
    cached = len(executions) == 1 and "run #2" not in str(second)
    print("CLAIM CONFIRMED (second call served from cache)" if cached else "CLAIM NOT REPRODUCED")
    sys.exit(0 if cached else 1)


asyncio.run(main())
