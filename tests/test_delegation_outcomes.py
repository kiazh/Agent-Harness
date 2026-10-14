"""Stream C: typed delegation outcomes, single deadline, name validator.

Mocked providers/DB, AsyncMock patterns like tests/test_failed_delegation_usage.py.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from machina.core.agent_def import AgentDef
from machina.core.models import AgentResponse, Session
from machina.core.orchestrator import Orchestrator


@pytest.fixture
def delegation(monkeypatch):
    from machina.core import orchestrator as module
    from machina.core.usage import usage_store

    child = Session(id=uuid4(), agent_id="coder")
    charges = {}
    recorded = []

    async def summary(session_id, agent_id):
        return {"session": {"chargedTokens": charges.get(session_id, 0)}}

    async def record_end(message_id, **kwargs):
        recorded.append(kwargs)

    monkeypatch.setattr(
        module.agent_registry, "get", AsyncMock(return_value=AgentDef(name="coder"))
    )
    monkeypatch.setattr(module.session_manager, "create", AsyncMock(return_value=child))
    monkeypatch.setattr(
        "machina.core.session_mode.resolve_effective_mode", AsyncMock(return_value="ask")
    )
    monkeypatch.setattr(module.context_manager, "add_chunk", AsyncMock())
    monkeypatch.setattr(usage_store, "summary", summary)

    def build(agent):
        orch = Orchestrator(agent_factory=lambda _: agent)
        monkeypatch.setattr(orch, "_record_start", AsyncMock(return_value=uuid4()))
        monkeypatch.setattr(orch, "_record_end", record_end)
        monkeypatch.setattr(orch, "_parent_context", AsyncMock(return_value=""))
        return orch

    return SimpleNamespace(child=child, charges=charges, recorded=recorded, build=build)


def _failed_child(delegation, content, tokens_known=7, charged=13):
    class Child:
        async def run(self, session_id, *_args, **_kwargs):
            delegation.charges[session_id] = charged
            return AgentResponse(content=content, tokens_used=tokens_known, outcome="failed")

    return Child()


@pytest.mark.parametrize(
    "content",
    [
        "LLM provider error. Check server logs.",  # provider-failure
        "budget exceeded for session",  # budget-failure
    ],
)
async def test_failed_outcome_maps_to_error_status(delegation, content):
    orch = delegation.build(_failed_child(delegation, content))
    result = await orch.delegate("coder", "work")
    assert result.status == "error"
    assert result.tokens == 13
    assert delegation.recorded[-1]["status"] == "error"
    assert delegation.recorded[-1]["tokens"] == 13
    # Never detected via prose: even a success-sounding body with failed outcome errors.
    assert result.response == content


async def test_failed_outcome_success_sounding_prose_still_errors(delegation):
    orch = delegation.build(_failed_child(delegation, "all done, responded ok"))
    result = await orch.delegate("coder", "work")
    assert result.status == "error"
    assert "responded" not in result.status


async def test_waiting_approval_semantics_intact(delegation):
    pause = {
        "request_id": "r1",
        "session_id": "s",
        "agent_id": "coder",
        "operation": "tool.terminal",
        "target": "ls",
        "status": "pending",
    }

    class Child:
        async def run(self, session_id, *_args, **_kwargs):
            delegation.charges[session_id] = 5
            return AgentResponse(content="Waiting for approval.", needs_approval=[pause])

    result = await delegation.build(Child()).delegate("coder", "work")
    assert result.status == "waiting_approval"


async def test_failed_wrapper_label_never_responded(delegation, monkeypatch):
    from machina.core import orchestrator as module
    from machina.core.agent_def import agent_registry
    from machina.tools.agents import delegate

    orch = delegation.build(_failed_child(delegation, "LLM provider error. Check server logs."))
    monkeypatch.setattr(module, "orchestrator", orch)
    monkeypatch.setattr(
        agent_registry, "list", AsyncMock(return_value=[AgentDef(name="coder")])
    )
    receipt = await delegate("coder", "work")
    assert "failed" in receipt
    assert "responded" not in receipt
    assert receipt.tokens_used == 13


async def test_effective_deadline_is_120_through_real_tool_path(monkeypatch):
    import asyncio as _asyncio

    from machina.core import orchestrator as module
    from machina.core.agent_def import agent_registry
    from machina.tools.agents import delegate

    captured = {}

    async def fake_delegate(*_args, **kwargs):
        captured["deadline_in"] = module.delegation_deadline.get()
        captured["now"] = _asyncio.get_running_loop().time()
        return SimpleNamespace(status="complete", response="ok", tokens=0, needs_approval=[])

    monkeypatch.setattr(module.orchestrator, "delegate", fake_delegate)
    monkeypatch.setattr(
        agent_registry, "list", AsyncMock(return_value=[AgentDef(name="coder")])
    )
    assert module.delegation_deadline.get() is None
    await delegate("coder", "work")
    delta = captured["deadline_in"] - captured["now"]
    assert 115 <= delta <= 125, f"expected ~120, got {delta}"
    assert module.delegation_deadline.get() is None


async def test_inherited_shorter_deadline_clips_through_real_tool_path(monkeypatch):
    import asyncio as _asyncio

    from machina.core import orchestrator as module
    from machina.core.agent_def import agent_registry
    from machina.tools.agents import delegate

    captured = {}

    async def fake_delegate(*_args, **kwargs):
        captured["deadline_in"] = module.delegation_deadline.get()
        return SimpleNamespace(status="complete", response="ok", tokens=0, needs_approval=[])

    monkeypatch.setattr(module.orchestrator, "delegate", fake_delegate)
    monkeypatch.setattr(
        agent_registry, "list", AsyncMock(return_value=[AgentDef(name="coder")])
    )
    loop = _asyncio.get_running_loop()
    inherited = loop.time() + 10
    token = module.delegation_deadline.set(inherited)
    try:
        await delegate("coder", "work")
    finally:
        module.delegation_deadline.reset(token)
    assert abs(captured["deadline_in"] - inherited) < 1.0
    assert module.delegation_deadline.get() is None


async def test_unknown_delegate_name_raises_with_suggestions_zero_child_cost(monkeypatch):
    from machina.core import orchestrator as module
    from machina.core.agent_def import agent_registry
    from machina.core.exceptions import ToolError
    from machina.tools.agents import delegate

    monkeypatch.setattr(
        agent_registry,
        "list",
        AsyncMock(
            return_value=[
                AgentDef(name="coder"),
                AgentDef(name="researcher"),
                AgentDef(name="code-reviewer"),
            ]
        ),
    )
    created = AsyncMock()
    monkeypatch.setattr(module.session_manager, "create", created)
    orch_delegate = AsyncMock()
    monkeypatch.setattr(module.orchestrator, "delegate", orch_delegate)
    with pytest.raises(ToolError) as exc:
        await delegate("ecc-code-reviewer", "work")
    assert "code-reviewer" in str(exc.value)
    created.assert_not_called()
    orch_delegate.assert_not_called()


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize(
    "content",
    [
        "LLM provider error. Check server logs.",
        "budget exceeded for session",
    ],
)
async def test_typed_failure_parent_charges_once_run_and_stream(
    delegation, monkeypatch, streaming, content
):
    """Typed failed child via real delegate tool: usage charged exactly once, parent sees failure."""

    from pathlib import Path
    from types import SimpleNamespace

    from machina.core import orchestrator as module
    from machina.core.agent import ReActAgent, registry
    from machina.core.agent_def import agent_registry
    from machina.core.execution_context import ExecutionContext
    from machina.core.models import LLMResponse, Session, StreamEvent
    from machina.permissions.broker import permission_broker
    from machina.tools.agents import delegate

    parent = Session(id=uuid4())
    agent = ReActAgent(provider=SimpleNamespace(model="fake"), allowed_tools=["delegate"])
    orch = delegation.build(_failed_child(delegation, content))
    monkeypatch.setattr(module, "orchestrator", orch)
    monkeypatch.setattr(
        agent_registry, "list", AsyncMock(return_value=[AgentDef(name="coder")])
    )
    monkeypatch.setattr("machina.tools.agents.db.fetchval", AsyncMock(return_value="harness"))
    monkeypatch.setattr(agent, "_prepare_context", AsyncMock(return_value=(parent, [])))
    monkeypatch.setattr(agent, "_flush_pending_chunks", AsyncMock())
    monkeypatch.setattr("machina.core.agent.session_manager.update_activity", AsyncMock())
    monkeypatch.setattr(agent, "_schedule_memory_consolidation", lambda *_: None)
    monkeypatch.setattr(agent, "_schedule_learning_review", lambda *_: None)
    monkeypatch.setattr(permission_broker, "guard", AsyncMock())
    monkeypatch.setattr(
        "machina.core.execution_context.resolve_execution_context",
        AsyncMock(return_value=ExecutionContext("ask", "host", Path("/tmp"))),
    )

    async def execute(name, **kwargs):
        assert name == "delegate"
        return await delegate(**kwargs)

    monkeypatch.setattr(registry, "execute", execute)
    calls = []

    async def complete(**kwargs):
        calls.append(kwargs)
        if len(calls) > 1:
            return LLMResponse(content="continued", model="fake", usage={"total_tokens": 1})
        return LLMResponse(
            content="",
            model="fake",
            usage={"total_tokens": 1},
            tool_calls=[
                {
                    "id": "call",
                    "type": "function",
                    "function": {
                        "name": "delegate",
                        "arguments": '{"agent":"coder","task":"work"}',
                    },
                }
            ],
        )

    async def stream(**kwargs):
        yield StreamEvent(type="done", response=await complete(**kwargs))

    monkeypatch.setattr(agent, "_call_llm_with_retry", complete)
    monkeypatch.setattr(agent, "_stream_llm_with_retry", stream)
    if streaming:
        events = [
            event
            async for event in ReActAgent.run_stream.__wrapped__(agent, parent.id, "work", False)
        ]
        response = events[-1].response
    else:
        response = await ReActAgent.run.__wrapped__(agent, parent.id, "work", False)
    assert len(calls) == 2
    # 1 (first LLM) + 13 (failed child, exact-once) + 1 (second LLM)
    assert response.tokens_used == 15
    assert response.tool_calls[0]["outcome"] == "failed"
