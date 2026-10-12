"""Failed delegations still charge the parent for child and descendant work."""

import asyncio
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from machina.core.agent import ReActAgent
from machina.core.agent_def import AgentDef
from machina.core.execution_context import ExecutionContext
from machina.core.models import AgentResponse, LLMResponse, Session, StreamEvent
from machina.core.orchestrator import Orchestrator


@pytest.fixture
def delegation(monkeypatch):
    from machina.core import orchestrator as module
    from machina.core.usage import usage_store

    child = Session(id=uuid4(), agent_id="coder")
    charges = {}
    recorded = []

    async def summary(session_id, agent_id):
        assert session_id == child.id
        assert agent_id == "coder"
        return {
            "session": {"chargedTokens": charges.get(session_id, 0)},
            # Other sessions for this agent must never be charged to this turn.
            "agent": {"chargedTokens": 999},
        }

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


def spending_child(delegation, outcome):
    class Child:
        async def run(self, session_id, *_args, **_kwargs):
            # This total includes a completed nested delegation and a failed
            # provider reservation, as returned by the durable usage summary.
            delegation.charges[session_id] = 13
            if outcome == "timeout":
                await asyncio.Event().wait()
            raise RuntimeError("failed after provider work")

    return Child()


@pytest.mark.parametrize("outcome", ["raise", "timeout"])
async def test_failed_child_returns_its_charged_usage(delegation, monkeypatch, outcome):
    orch = delegation.build(spending_child(delegation, outcome))
    monkeypatch.setattr(orch, "DELEGATION_TIMEOUT_SECONDS", 0.01)

    result = await orch.delegate("coder", "work")

    assert result.status == "error"
    assert result.tokens == 13
    assert delegation.recorded[-1]["tokens"] == 13


@pytest.mark.parametrize("charged, known, expected", [(13, 7, 13), (7, 13, 13)])
async def test_success_uses_maximum_of_durable_and_response_usage(
    delegation, charged, known, expected
):
    class Child:
        async def run(self, session_id, *_args, **_kwargs):
            delegation.charges[session_id] = charged
            return AgentResponse(content="done", tokens_used=known)

    result = await delegation.build(Child()).delegate("coder", "work")

    assert result.tokens == expected
    assert delegation.recorded[-1]["tokens"] == expected


async def test_success_keeps_known_usage_without_database(delegation, monkeypatch):
    from machina.core.exceptions import DatabaseError
    from machina.core.usage import usage_store

    monkeypatch.setattr(
        usage_store, "summary", AsyncMock(side_effect=DatabaseError("not connected"))
    )

    class Child:
        async def run(self, *_args, **_kwargs):
            return AgentResponse(content="done", tokens_used=13)

    result = await delegation.build(Child()).delegate("coder", "work")
    assert result.tokens == 13


@pytest.mark.parametrize("outcome", ["raise", "success"])
async def test_unavailable_production_accounting_never_reports_zero(
    delegation, monkeypatch, outcome
):
    from machina.core.exceptions import DatabaseError
    from machina.core.usage import db, usage_store

    monkeypatch.setattr(db, "_pool", object())
    monkeypatch.setattr(
        usage_store, "summary", AsyncMock(side_effect=DatabaseError("ledger read failed"))
    )

    class Child:
        async def run(self, session_id, *_args, **_kwargs):
            delegation.charges[session_id] = 13
            if outcome == "raise":
                raise RuntimeError("failed after provider work")
            return AgentResponse(content="done", tokens_used=7)

    with pytest.raises(DatabaseError, match="delegation usage accounting unavailable"):
        await delegation.build(Child()).delegate("coder", "work")
    assert delegation.recorded == []


async def test_tool_timeout_returns_accounting_receipt(delegation, monkeypatch):
    from machina.core import orchestrator as module
    from machina.tools.agents import DelegationToolResult, delegate

    orch = delegation.build(spending_child(delegation, "timeout"))
    monkeypatch.setattr(module, "orchestrator", orch)
    original_wait_for = asyncio.wait_for

    async def accelerated_outer_timeout(awaitable, timeout):
        # Compress the tool's old independent 60s timeout. Orchestrator uses
        # the inherited short deadline, so its accounting still runs normally.
        return await original_wait_for(awaitable, 0.001 if timeout == 60 else timeout)

    monkeypatch.setattr(asyncio, "wait_for", accelerated_outer_timeout)
    token = module.delegation_deadline.set(asyncio.get_running_loop().time() + 0.02)
    try:
        receipt = await delegate("coder", "work")
    finally:
        module.delegation_deadline.reset(token)

    assert isinstance(receipt, DelegationToolResult)
    assert receipt.tokens_used == 13
    assert "timed out" in receipt


async def test_parent_cancellation_drains_child_and_records_spend(delegation):
    entered, cleaning, release, cleaned = (asyncio.Event() for _ in range(4))

    class Child:
        async def run(self, session_id, *_args, **_kwargs):
            delegation.charges[session_id] = 13
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
                cleaned.set()

    task = asyncio.create_task(delegation.build(Child()).delegate("coder", "work"))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    await asyncio.wait_for(cleaning.wait(), 1)
    try:
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned.is_set()
    assert delegation.recorded[-1]["status"] == "cancelled"
    assert delegation.recorded[-1]["tokens"] == 13


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("outcome", ["raise", "timeout", "ledger_unavailable"])
async def test_failed_child_spend_is_accounted_without_implicit_turn_cutoff(
    delegation, monkeypatch, streaming, outcome
):
    from machina.core import orchestrator as module
    from machina.core.agent import registry
    from machina.permissions.broker import permission_broker
    from machina.tools.agents import delegate

    parent = Session(id=uuid4())
    agent = ReActAgent(provider=SimpleNamespace(model="fake"), allowed_tools=["delegate"])
    orch = delegation.build(spending_child(delegation, outcome))
    monkeypatch.setattr(orch, "DELEGATION_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(module, "orchestrator", orch)
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
    if outcome == "ledger_unavailable":
        from machina.core.exceptions import DatabaseError
        from machina.core.usage import db, usage_store

        monkeypatch.setattr(db, "_pool", object())
        monkeypatch.setattr(
            usage_store, "summary", AsyncMock(side_effect=DatabaseError("ledger read failed"))
        )
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
    token = module.delegation_deadline.set(asyncio.get_running_loop().time() + 0.02)
    try:
        expectation = (
            pytest.raises(DatabaseError, match="delegation usage accounting unavailable")
            if outcome == "ledger_unavailable"
            else nullcontext()
        )
        with expectation:
            if streaming:
                events = [
                    event
                    async for event in ReActAgent.run_stream.__wrapped__(
                        agent, parent.id, "work", False
                    )
                ]
                response = events[-1].response
            else:
                response = await ReActAgent.run.__wrapped__(agent, parent.id, "work", False)
    finally:
        module.delegation_deadline.reset(token)

    if outcome == "ledger_unavailable":
        assert len(calls) == 1
        assert delegation.charges[delegation.child.id] == 13
        return
    assert len(calls) == 2
    assert response.tokens_used == 15
    assert response.content == "continued"
