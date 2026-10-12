"""Delegation preserves authority, pauses, cancellation, and budget attribution."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from machina.core.agent import ReActAgent
from machina.core.agent_def import AgentDef
from machina.core.agent_factory import parent_authority_var
from machina.core.config import config
from machina.core.execution_context import ExecutionContext
from machina.core.models import AgentResponse, LLMResponse, Session, StreamEvent
from machina.core.orchestrator import Orchestrator


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["ask", "sandbox"])
@pytest.mark.parametrize("terminal_sandbox", ["disabled", "docker"])
async def test_child_uses_parent_mode_even_when_global_default_is_full(
    monkeypatch, mode, terminal_sandbox
):
    from machina.core import orchestrator as module

    monkeypatch.setattr(config, "execution_mode", "full")
    monkeypatch.setattr(config, "terminal_sandbox", terminal_sandbox)
    parent_id, child_id = uuid4(), uuid4()
    created = {}

    async def create(**kwargs):
        created.update(kwargs)
        return Session(id=child_id, execution_mode=kwargs.get("execution_mode", "full"))

    class Child:
        async def run(self, sid, prompt, verbose):
            assert created.get("execution_mode") == mode
            assert parent_authority_var.get()["max_mode"] == mode
            assert parent_authority_var.get()["tools"] == ["read_file"]
            if mode == "sandbox" or terminal_sandbox == "docker":
                assert parent_authority_var.get().get("terminal_backend") == "sandbox"
            return AgentResponse(content="done")

    monkeypatch.setattr(
        module.agent_registry,
        "get",
        AsyncMock(return_value=AgentDef(name="coder", tools=["read_file", "terminal"])),
    )
    monkeypatch.setattr(module.session_manager, "create", create)
    monkeypatch.setattr("machina.core.session_mode.resolve_effective_mode", AsyncMock(return_value=mode))
    orch = Orchestrator(agent_factory=lambda _: Child())
    monkeypatch.setattr(orch, "_parent_context", AsyncMock(return_value=""))
    monkeypatch.setattr(orch, "_record_start", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(orch, "_record_end", AsyncMock())
    monkeypatch.setattr(module.context_manager, "add_chunk", AsyncMock())
    result = await orch.delegate(
        "coder", "inspect", parent_session_id=parent_id, authority={"tools": ["read_file"]}
    )
    assert result.status == "complete"
    assert created["parent_session_id"] == parent_id


@pytest.mark.asyncio
async def test_orchestrator_returns_child_pause_and_stops_sequential_work(monkeypatch):
    from machina.core import orchestrator as module

    card = {
        "request_id": "approval-child",
        "agent_id": "coder",
        "session_id": str(uuid4()),
        "operation": "file.write",
        "target": "a.txt",
        "status": "pending",
    }

    class Child:
        async def run(self, *args, **kwargs):
            return AgentResponse(content="waiting", needs_approval=[card])

    monkeypatch.setattr(
        module.agent_registry, "get", AsyncMock(return_value=AgentDef(name="coder"))
    )
    monkeypatch.setattr(
        module.session_manager, "create", AsyncMock(return_value=Session(id=uuid4()))
    )
    orch = Orchestrator(agent_factory=lambda _: Child())
    monkeypatch.setattr(orch, "_record_start", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(orch, "_record_end", AsyncMock())
    results = await orch.run_sequential([("coder", "write"), ("coder", "publish")])
    assert len(results) == 1
    assert results[0].status == "waiting_approval"
    assert results[0].needs_approval == [card]
    assert orch._record_end.await_args.kwargs["status"] == "waiting_approval"


def _tool_response():
    return LLMResponse(
        content="",
        model="fake",
        usage={"total_tokens": 1},
        tool_calls=[
            {
                "id": "call",
                "type": "function",
                "function": {"name": "delegate", "arguments": '{"agent":"coder","task":"write"}'},
            }
        ],
    )


@pytest.fixture
async def bare_agent(monkeypatch):
    from machina.core.agent import registry
    from machina.permissions.broker import permission_broker

    session = Session(id=uuid4())
    agent = ReActAgent(provider=SimpleNamespace(model="fake"), allowed_tools=["delegate"])
    monkeypatch.setattr(agent, "_prepare_context", AsyncMock(return_value=(session, [])))
    monkeypatch.setattr(agent, "_flush_pending_chunks", AsyncMock())
    monkeypatch.setattr(agent, "_schedule_memory_consolidation", lambda *_: None)
    monkeypatch.setattr(agent, "_schedule_learning_review", lambda *_: None)
    monkeypatch.setattr("machina.core.agent.session_manager.update_activity", AsyncMock())
    monkeypatch.setattr(permission_broker, "guard", AsyncMock())
    monkeypatch.setattr(
        "machina.core.execution_context.resolve_execution_context",
        AsyncMock(return_value=ExecutionContext("ask", "host", Path("/tmp"))),
    )
    monkeypatch.setattr(registry, "execute", AsyncMock(return_value="done"))
    return agent, session


@pytest.mark.asyncio
async def test_cancelling_parent_waits_until_child_cleanup_finishes(bare_agent, monkeypatch):
    from machina.core.agent import registry

    agent, session = bare_agent
    started, cleaning, release, cleaned = (asyncio.Event() for _ in range(4))

    async def execute(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            cleaned.set()

    monkeypatch.setattr(registry, "execute", execute)

    async def consume():
        async for _ in agent._execute_tool_calls_stream(_tool_response(), [], [], session.id, []):
            pass

    task = asyncio.create_task(consume())
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    await asyncio.wait_for(cleaning.wait(), 1)
    await asyncio.sleep(0)
    try:
        assert not task.done(), "parent returned before cancelled child cleanup finished"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert cleaned.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_child_pause_reaches_parent_without_another_provider_call(
    bare_agent, monkeypatch, streaming
):
    from machina.core import orchestrator as module
    from machina.core.agent import registry
    from machina.tools.agents import delegate

    agent, session = bare_agent
    card = {
        "request_id": "approval-child",
        "agent_id": "coder",
        "session_id": str(uuid4()),
        "operation": "file.write",
        "target": "a.txt",
        "status": "pending",
    }
    monkeypatch.setattr("machina.tools.agents.db.fetchval", AsyncMock(return_value="harness"))
    monkeypatch.setattr(
        module.orchestrator,
        "delegate",
        AsyncMock(
            return_value=SimpleNamespace(
                status="waiting_approval", response="waiting", tokens=7, needs_approval=[card]
            )
        ),
    )

    async def execute(name, **kwargs):
        return await delegate(**kwargs)

    monkeypatch.setattr(registry, "execute", execute)
    calls = []

    async def complete(**kwargs):
        calls.append(kwargs)
        return (
            _tool_response()
            if len(calls) == 1
            else LLMResponse(content="continued", model="fake", usage={"total_tokens": 1})
        )

    async def stream(**kwargs):
        yield StreamEvent(type="done", response=await complete(**kwargs))

    monkeypatch.setattr(agent, "_call_llm_with_retry", complete)
    monkeypatch.setattr(agent, "_stream_llm_with_retry", stream)
    if streaming:
        events = [
            event
            async for event in ReActAgent.run_stream.__wrapped__(agent, session.id, "write", False)
        ]
        result = events[-1].response
        assert any(event.type == "needs_approval" for event in events)
    else:
        result = await ReActAgent.run.__wrapped__(agent, session.id, "write", False)
    assert result.needs_approval == [card]
    assert result.tokens_used == 8
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_parent_accounts_child_spend_without_implicit_turn_cutoff(
    bare_agent, monkeypatch, streaming
):
    from machina.core import orchestrator as module
    from machina.core.agent import registry
    from machina.tools.agents import delegate

    agent, session = bare_agent
    monkeypatch.setattr("machina.tools.agents.db.fetchval", AsyncMock(return_value="harness"))
    monkeypatch.setattr(
        module.orchestrator,
        "delegate",
        AsyncMock(
            return_value=SimpleNamespace(
                status="complete", response="done", tokens=7, needs_approval=[]
            )
        ),
    )

    async def execute(name, **kwargs):
        return await delegate(**kwargs)

    monkeypatch.setattr(registry, "execute", execute)
    calls = []

    async def complete(**kwargs):
        calls.append(kwargs)
        return (
            _tool_response()
            if len(calls) == 1
            else LLMResponse(content="continued", model="fake", usage={"total_tokens": 1})
        )

    async def stream(**kwargs):
        yield StreamEvent(type="done", response=await complete(**kwargs))

    monkeypatch.setattr(agent, "_call_llm_with_retry", complete)
    monkeypatch.setattr(agent, "_stream_llm_with_retry", stream)
    if streaming:
        events = [
            event
            async for event in ReActAgent.run_stream.__wrapped__(agent, session.id, "write", False)
        ]
        result = events[-1].response
    else:
        result = await ReActAgent.run.__wrapped__(agent, session.id, "write", False)
    assert len(calls) == 2
    assert result.tokens_used == 9
    assert result.content == "continued"


@pytest.mark.asyncio
async def test_estimated_provider_usage_keeps_reservation(monkeypatch):
    from machina.core.usage import UsageStore

    execute = AsyncMock(return_value="UPDATE 1")
    monkeypatch.setattr("machina.core.usage.db.execute", execute)
    await UsageStore().finish(
        uuid4(), {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3, "estimated": True}
    )
    assert execute.await_args.args[4] is None
    assert execute.await_args.args[5] is None


@pytest.mark.db
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "limit_key",
    [
        "usage_session_request_limit",
        "usage_agent_request_limit",
        "usage_session_token_limit",
        "usage_agent_token_limit",
    ],
)
async def test_child_calls_cannot_bypass_ancestor_budget(db_pool, monkeypatch, limit_key):
    from machina.core.exceptions import UsageBudgetExceededError
    from machina.core.session import session_manager
    from machina.core.usage import UsageStore

    monkeypatch.setattr("machina.core.session.db", db_pool)
    monkeypatch.setattr("machina.core.usage.db", db_pool)
    for key in (
        "usage_session_request_limit",
        "usage_agent_request_limit",
        "usage_session_token_limit",
        "usage_agent_token_limit",
    ):
        monkeypatch.setattr(config, key, 0)
    parent_agent, child_agent = f"parent-{uuid4()}", f"child-{uuid4()}"
    parent = await session_manager.create(agent_id=parent_agent)
    child = await session_manager.create(agent_id=child_agent, parent_session_id=parent.id)
    store = UsageStore()
    monkeypatch.setattr(config, limit_key, 1 if "request" in limit_key else 100000)
    reservation = await store.reserve(child.id, child_agent, "fake", "fake", [], [], 1)
    await store.finish(reservation, {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3})
    if "token" in limit_key:
        monkeypatch.setattr(config, limit_key, 3)
    sibling = await session_manager.create(
        agent_id=f"sibling-{uuid4()}", parent_session_id=parent.id
    )
    with pytest.raises(UsageBudgetExceededError):
        await store.reserve(sibling.id, sibling.agent_id, "fake", "fake", [], [], 1)
    row = await db_pool.fetchrow(
        "SELECT agent_id, session_id FROM llm_usage WHERE id = $1", reservation
    )
    assert row["agent_id"] == child_agent
    assert row["session_id"] == child.id
    summary = await store.summary(parent.id, parent_agent)
    assert summary["session"]["chargedRequests"] == 1
    assert summary["agent"]["chargedTokens"] == 3
    await session_manager.delete(child.id)
    summary = await store.summary(parent.id, parent_agent)
    assert summary["session"]["chargedTokens"] == 3


@pytest.mark.db
@pytest.mark.asyncio
async def test_parallel_child_reservations_share_ancestor_request_budget(db_pool, monkeypatch):
    from machina.core.exceptions import UsageBudgetExceededError
    from machina.core.session import session_manager
    from machina.core.usage import UsageStore

    monkeypatch.setattr("machina.core.session.db", db_pool)
    monkeypatch.setattr("machina.core.usage.db", db_pool)
    for key in (
        "usage_session_request_limit",
        "usage_agent_request_limit",
        "usage_session_token_limit",
        "usage_agent_token_limit",
    ):
        monkeypatch.setattr(config, key, 0)
    parent = await session_manager.create(agent_id=f"parent-{uuid4()}")
    children = [
        await session_manager.create(agent_id=f"child-{uuid4()}", parent_session_id=parent.id)
        for _ in range(3)
    ]
    monkeypatch.setattr(config, "usage_session_request_limit", 1)
    results = await asyncio.gather(
        *(
            UsageStore().reserve(child.id, child.agent_id, "fake", "fake", [], [], 1)
            for child in children
        ),
        return_exceptions=True,
    )
    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert sum(isinstance(result, UsageBudgetExceededError) for result in results) == 2


@pytest.mark.asyncio
async def test_rebuilt_child_retains_durable_delegation_restrictions(monkeypatch):
    from machina.core.agent_factory import build_agent_for_session

    monkeypatch.setattr(config, "memory_enabled", False)
    monkeypatch.setattr(config, "rag_enabled", False)
    monkeypatch.setattr(
        "machina.core.agent_def.agent_registry.get",
        AsyncMock(return_value=AgentDef(name="coder", tools=["read_file", "terminal"])),
    )
    monkeypatch.setattr("machina.core.provider.get_provider", lambda **_: SimpleNamespace(model="fake"))
    session = SimpleNamespace(
        agent_id="coder",
        model="fake",
        provider="fake",
        delegation_authority={
            "tools": ["read_file"],
            "max_mode": "ask",
            "terminal_backend": "sandbox",
        },
    )
    agent = await build_agent_for_session(
        session, authority={"tools": ["read_file", "terminal"], "max_mode": "full"}
    )
    assert agent.allowed_tools == ["read_file"]
    assert agent._authority["max_mode"] == "ask"
    assert agent._authority["terminal_backend"] == "sandbox"


@pytest.mark.asyncio
@pytest.mark.parametrize("activated", [False, True])
async def test_full_child_inherits_only_live_parent_full_grant(monkeypatch, activated):
    from machina.core import orchestrator as module
    from machina.permissions import store
    from machina.permissions.policy import build_request, decide

    parent, child = (
        Session(id=uuid4(), execution_mode="full"),
        Session(id=uuid4(), agent_id="coder", execution_mode="full"),
    )
    monkeypatch.setattr(module.session_manager, "get_fresh", AsyncMock(return_value=parent))
    monkeypatch.setattr(module.session_manager, "create", AsyncMock(return_value=child))
    monkeypatch.setattr(
        module.agent_registry, "get", AsyncMock(return_value=AgentDef(name="coder"))
    )
    monkeypatch.setattr(
        "machina.core.session_mode.resolve_effective_mode", AsyncMock(return_value="full")
    )
    monkeypatch.setattr(module.context_manager, "add_chunk", AsyncMock())
    if activated:
        await store.save_grant(
            {
                "session_id": str(parent.id),
                "agent_id": parent.agent_id,
                "mode": "full",
                "capability": "session",
                "scope_type": "file",
                "grant_kind": "session",
                "digest": "full-mode-session",
            }
        )

    class Child:
        async def run(self, sid, *args, **kwargs):
            req = build_request(
                operation="file.read",
                targets=["/tmp/delegation-read.txt"],
                mode="full",
                agent_id="coder",
                session_id=str(sid),
                tool="read_file",
            )
            decision = decide(req, await store.list_grants(str(sid)))
            assert (decision.verdict == "allowed") is activated
            return AgentResponse(content="done")

    orch = Orchestrator(agent_factory=lambda _: Child())
    monkeypatch.setattr(orch, "_parent_context", AsyncMock(return_value=""))
    monkeypatch.setattr(orch, "_record_start", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(orch, "_record_end", AsyncMock())
    result = await orch.delegate(
        "coder", "read", parent_session_id=parent.id, from_agent=parent.agent_id
    )
    assert result.status == "complete"


@pytest.mark.db
@pytest.mark.asyncio
@pytest.mark.parametrize("deleted", ["middle", "root"])
async def test_resumed_descendant_keeps_budgets_after_ancestor_deletion(
    db_pool, monkeypatch, deleted
):
    from machina.core.exceptions import UsageBudgetExceededError
    from machina.core.session import session_manager
    from machina.core.usage import UsageStore

    monkeypatch.setattr("machina.core.session.db", db_pool)
    monkeypatch.setattr("machina.core.usage.db", db_pool)
    for key in (
        "usage_session_request_limit",
        "usage_agent_request_limit",
        "usage_session_token_limit",
        "usage_agent_token_limit",
    ):
        monkeypatch.setattr(config, key, 0)
    root = await session_manager.create(agent_id=f"root-{uuid4()}")
    middle = await session_manager.create(agent_id=f"middle-{uuid4()}", parent_session_id=root.id)
    grandchild = await session_manager.create(
        agent_id=f"grandchild-{uuid4()}", parent_session_id=middle.id
    )
    await UsageStore().reserve(root.id, root.agent_id, "fake", "fake", [], [], 1)
    await session_manager.delete(middle.id if deleted == "middle" else root.id)
    monkeypatch.setattr(
        config,
        "usage_session_request_limit" if deleted == "middle" else "usage_agent_request_limit",
        1,
    )
    with pytest.raises(UsageBudgetExceededError):
        await UsageStore().reserve(grandchild.id, grandchild.agent_id, "fake", "fake", [], [], 1)


@pytest.mark.asyncio
async def test_child_full_authority_stops_when_parent_grant_is_revoked(monkeypatch):
    from machina.core import orchestrator as module
    from machina.permissions import store
    from machina.permissions.broker import ApprovalDenied, permission_broker
    from machina.permissions.policy import build_request

    parent = Session(id=uuid4(), execution_mode="full")
    child = Session(id=uuid4(), agent_id="coder", execution_mode="full")
    saved_authority = {}

    async def create(**kwargs):
        saved_authority.update(kwargs.get("delegation_authority", {}))
        return child

    monkeypatch.setattr(module.session_manager, "get_fresh", AsyncMock(return_value=parent))
    monkeypatch.setattr(module.session_manager, "create", create)
    monkeypatch.setattr(
        module.agent_registry, "get", AsyncMock(return_value=AgentDef(name="coder"))
    )
    monkeypatch.setattr(
        "machina.core.session_mode.resolve_effective_mode", AsyncMock(return_value="full")
    )
    monkeypatch.setattr(module.context_manager, "add_chunk", AsyncMock())
    await store.save_grant(
        {
            "session_id": str(parent.id),
            "agent_id": parent.agent_id,
            "mode": "full",
            "capability": "session",
            "scope_type": "file",
            "grant_kind": "session",
            "digest": "full-mode-session",
        }
    )

    class Child:
        async def run(self, sid, *args, **kwargs):
            req = build_request(
                operation="file.read",
                targets=["/tmp/delegation-read.txt"],
                mode="full",
                agent_id="coder",
                session_id=str(sid),
                tool="read_file",
            )
            await permission_broker.guard(req)
            await store.revoke_grants(str(parent.id))
            with pytest.raises(ApprovalDenied, match="inherited full"):
                await permission_broker.guard(req)
            return AgentResponse(content="done")

    orch = Orchestrator(agent_factory=lambda _: Child())
    monkeypatch.setattr(orch, "_parent_context", AsyncMock(return_value=""))
    monkeypatch.setattr(orch, "_record_start", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(orch, "_record_end", AsyncMock())
    result = await orch.delegate(
        "coder", "read", parent_session_id=parent.id, from_agent=parent.agent_id
    )
    assert result.status == "complete"
    assert saved_authority["full_grant_sources"][0]["session_id"] == str(parent.id)


@pytest.mark.db
@pytest.mark.asyncio
async def test_runtime_migration_backfills_historical_delegation_charges(db_pool, monkeypatch):
    from machina.core.session import session_manager
    from machina.core.usage import UsageStore

    monkeypatch.setattr("machina.core.session.db", db_pool)
    monkeypatch.setattr("machina.core.usage.db", db_pool)
    root = await session_manager.create(agent_id=f"root-{uuid4()}")
    child = await session_manager.create(agent_id=f"child-{uuid4()}", parent_session_id=root.id)
    reservation = await UsageStore().reserve(child.id, child.agent_id, "fake", "fake", [], [], 1)
    await db_pool.execute(
        "UPDATE llm_usage SET ancestor_session_ids = '{}', ancestor_agent_ids = '{}' WHERE id = $1",
        reservation,
    )
    await db_pool.execute(
        "UPDATE sessions SET ancestor_session_ids = '{}', ancestor_agent_ids = '{}' WHERE id = $1",
        child.id,
    )
    await db_pool.initialize_runtime_schema()
    summary = await UsageStore().summary(root.id, root.agent_id)
    assert summary["session"]["chargedRequests"] == 1
    assert summary["agent"]["chargedRequests"] == 1
    await db_pool.initialize_runtime_schema()
    summary = await UsageStore().summary(root.id, root.agent_id)
    assert summary["session"]["chargedRequests"] == 1


@pytest.mark.db
@pytest.mark.asyncio
async def test_fork_keeps_ancestral_budgets_after_parent_deletion(db_pool, monkeypatch):
    from machina.core.exceptions import UsageBudgetExceededError
    from machina.core.session import session_manager
    from machina.core.usage import UsageStore

    monkeypatch.setattr("machina.core.session.db", db_pool)
    monkeypatch.setattr("machina.core.usage.db", db_pool)
    root = await session_manager.create(agent_id=f"root-{uuid4()}")
    child = await session_manager.create(agent_id=f"child-{uuid4()}", parent_session_id=root.id)
    await UsageStore().reserve(root.id, root.agent_id, "fake", "fake", [], [], 1)
    await session_manager.get(child.id)
    await session_manager.delete(root.id)
    fork = await session_manager.fork(child.id)
    monkeypatch.setattr(config, "usage_agent_request_limit", 1)
    with pytest.raises(UsageBudgetExceededError):
        await UsageStore().reserve(fork.id, fork.agent_id, "fake", "fake", [], [], 1)
