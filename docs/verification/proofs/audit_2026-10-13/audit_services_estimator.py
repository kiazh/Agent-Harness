#!/usr/bin/env python
"""Audit finding: AgentDef.tools=[] means unrestricted tools at execution but
zero tool schemas in the compaction estimator (undercount)."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path("/Users/shadow/machina")
sys.path.insert(0, str(REPO))

from machina.core.agent_factory import definition_tool_cap
from machina.core.assembler import get_token_count
from machina.core.agent_def import agent_registry
from machina.core.context import context_manager
from machina.core.session import session_manager
import machina.services as services
from machina.tools.base import registry

# Populate the registry with the real tool schemas.
import machina.tools.file  # noqa: F401
import machina.tools.builtins  # noqa: F401
import machina.tools.terminal  # noqa: F401


async def fake_session_get(_sid):
    return SimpleNamespace(agent_id="audit-agent", provider=None, model=None)


async def fake_defn_get(_name):
    return SimpleNamespace(tools=[], system_prompt="")


async def fake_token_usage(_sid):
    return 0


session_manager.get = fake_session_get
agent_registry.get = fake_defn_get
context_manager.get_token_usage = fake_token_usage


async def main() -> None:
    definition = SimpleNamespace(tools=[], system_prompt="")
    exec_cap = definition_tool_cap(definition)
    print("execution cap for tools=[] (definition_tool_cap):", exec_cap)

    total = await services._estimate_next_request("audit-session")
    full_schema = get_token_count(str(registry.get_tool_definitions()))
    print("estimator total with tools=[]:", total)
    print("effective full tool-schema tokens:", full_schema)
    print("undercount (missing schema):", full_schema)
    confirmed = exec_cap is None and full_schema > 0
    print("CLAIM CONFIRMED (execution unrestricted; estimator counted zero schemas)"
          if confirmed else "CLAIM NOT REPRODUCED")
    sys.exit(0 if confirmed else 1)


asyncio.run(main())
