#!/usr/bin/env python
"""Audit finding: headless `chat --provider X --model Y` passes no overrides
to the shared factory; the default model runs while the CLI prints requested
provider name."""
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

REPO = Path("/Users/shadow/machina")
sys.path.insert(0, str(REPO))

from typer.testing import CliRunner

import machina.cli as cli
import machina.core.agent_factory as agent_factory
from machina.db.connection import db

calls: dict = {}


async def fake_build(session, **kwargs):
    calls["session_provider"] = session.provider
    calls["session_model"] = session.model
    calls["factory_kwargs"] = kwargs
    agent = SimpleNamespace(provider=SimpleNamespace(model="provider-default-model"))

    async def run_stream(session_id, message, verbose=False):
        yield SimpleNamespace(type="done", response=None)

    agent.run_stream = run_stream
    return agent


agent_factory.build_agent_for_session = fake_build
agent_factory.close_agent_provider = AsyncMock()
db.connect = AsyncMock()
db.initialize_runtime_schema = AsyncMock()
db.close = AsyncMock()


async def fake_create(title=None, goal=None):
    return SimpleNamespace(id=uuid.uuid4(), provider=None, model=None, agent_id="harness")


cli.session_manager.create = fake_create

runner = CliRunner()
result = runner.invoke(
    cli.app,
    ["chat", "--provider", "ollama", "--model", "my-model", "hello"],
)
print("exit code:", result.exit_code)
for line in result.output.splitlines():
    if "Model:" in line or "Session ID" in line:
        print("cli output:", line.strip())
print("factory received kwargs:", calls.get("factory_kwargs"))
print("session provider/model:", calls.get("session_provider"), calls.get("session_model"))
overrides_sent = bool(calls.get("factory_kwargs"))
confirmed = result.exit_code == 0 and not overrides_sent
print("CLAIM CONFIRMED (factory got no overrides; default model ran)"
      if confirmed else "CLAIM NOT REPRODUCED")
sys.exit(0 if confirmed else 1)
