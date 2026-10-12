"""Limits on batch and recursive agent delegation."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from machina.core.agent_def import AgentDef
from machina.core.orchestrator import Orchestrator
from machina.gateway.errors import INVALID_PARAMS, RpcError
from machina.gateway.features.agents import _steps


def test_agents_run_rejects_excessive_steps():
    with pytest.raises(RpcError) as error:
        _steps({"steps": [{"agent": "coder", "task": "work"}] * 17})
    assert error.value.code == INVALID_PARAMS


async def test_delegation_deadline_marks_message_error(monkeypatch):
    from machina.core import orchestrator as module

    class HangingAgent:
        async def run(self, *_args, **_kwargs):
            await asyncio.Event().wait()

    monkeypatch.setattr(
        module.agent_registry, "get", AsyncMock(return_value=AgentDef(name="coder"))
    )
    monkeypatch.setattr(
        module.session_manager, "create", AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4()))
    )
    orch = Orchestrator(agent_factory=lambda _definition: HangingAgent())
    orch._record_start = AsyncMock(return_value=uuid.uuid4())
    orch._record_end = AsyncMock()
    monkeypatch.setattr(orch, "DELEGATION_TIMEOUT_SECONDS", 0.01, raising=False)
    result = await orch.delegate("coder", "wait")
    assert result.status == "error"
    assert "timed out" in result.response.lower()
    assert orch._record_end.await_args.kwargs["status"] == "error"


async def test_nested_delegation_inherits_the_root_deadline(monkeypatch):
    from machina.core import orchestrator as module

    class HangingAgent:
        async def run(self, *_args, **_kwargs):
            await asyncio.Event().wait()

    monkeypatch.setattr(
        module.agent_registry, "get", AsyncMock(return_value=AgentDef(name="coder"))
    )
    monkeypatch.setattr(
        module.session_manager, "create", AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4()))
    )
    orch = Orchestrator(agent_factory=lambda _definition: HangingAgent())
    orch._record_start = AsyncMock(return_value=uuid.uuid4())
    orch._record_end = AsyncMock()
    token = module.delegation_deadline.set(asyncio.get_running_loop().time() + 0.01)
    try:
        result = await asyncio.wait_for(orch.delegate("coder", "nested", _hop_count=2), 0.5)
    finally:
        module.delegation_deadline.reset(token)
    assert result.status == "error"


async def test_delegate_tool_passes_nested_depth(monkeypatch):
    from machina.core import orchestrator as module
    from machina.tools.agents import delegate

    fake_result = SimpleNamespace(status="complete", response="ok")
    call = AsyncMock(return_value=fake_result)
    monkeypatch.setattr(module.orchestrator, "delegate", call)
    token = module.delegation_depth.set(3)
    try:
        assert "ok" in await delegate("coder", "next")
    finally:
        module.delegation_depth.reset(token)
    assert call.await_args.kwargs["_hop_count"] == 4
