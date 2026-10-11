"""Delegated approval pauses retain their continuation identity at transports."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ah.core.models import AgentResponse, StreamEvent
from ah.core.orchestrator import DelegationResult


@pytest.mark.asyncio
async def test_rpc_sequential_delegation_returns_early_pause(monkeypatch):
    from ah.gateway.features.agents import agents_run

    paused = DelegationResult("coder", "first", "paused", uuid.uuid4(), 1, 1, "waiting_approval")
    runner = AsyncMock(return_value=[paused])
    monkeypatch.setattr("ah.core.orchestrator.orchestrator.run_sequential", runner)
    gateway = SimpleNamespace(require_db=lambda: None)
    result = await agents_run(
        gateway,
        {"steps": [{"agent": "coder", "task": "first"}, {"agent": "coder", "task": "second"}]},
    )
    assert [entry["status"] for entry in result["results"]] == ["waiting_approval"]


def test_rpc_delegation_result_preserves_typed_pauses():
    from ah.gateway.features.agents import _delegation

    child = uuid.uuid4()
    pause = {
        "request_id": "request",
        "session_id": str(child),
        "agent_id": "coder",
        "turn_id": "child-turn",
        "operation": "process.exec",
        "status": "pending",
    }
    result = DelegationResult("coder", "task", "paused", child, 2, 1, "waiting_approval", [pause])
    serialized = _delegation(result)
    assert serialized["needsApproval"][0]["requestId"] == "request"
    assert serialized["needsApproval"][0]["sessionId"] == str(child)
    assert serialized["needsApproval"][0]["turnId"] == "child-turn"


def test_sse_completion_preserves_child_continuation_identity():
    from ah.api.app import _serialize_event

    child = str(uuid.uuid4())
    pause = {
        "request_id": "request",
        "session_id": child,
        "agent_id": "coder",
        "turn_id": "child-turn",
        "operation": "process.exec",
        "status": "pending",
    }
    response = AgentResponse(content="paused", needs_approval=[pause])
    serialized = _serialize_event(
        StreamEvent(type="done", response=response),
        protocol_version=2,
        session_id="parent",
        turn_id="parent-turn",
    )
    assert serialized["sessionId"] == "parent"
    assert serialized["needsApproval"][0]["sessionId"] == child
    assert serialized["needsApproval"][0]["agentId"] == "coder"
    assert serialized["needsApproval"][0]["turnId"] == "child-turn"
