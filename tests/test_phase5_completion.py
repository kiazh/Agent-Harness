"""Phase 5 file definitions and parent/child context handoff."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ah.core.agent_def import AgentDef, AgentRegistry
from ah.core.models import AgentResponse, LLMResponse
from ah.core.orchestrator import Orchestrator


@pytest.mark.asyncio
async def test_yaml_agents_load_and_database_takes_precedence(tmp_path, monkeypatch):
    (tmp_path / "reviewer.yaml").write_text(
        "name: reviewer\ndescription: Review code\ntools: [read_file]\nmax_iterations: 4\n",
        encoding="utf-8",
    )
    (tmp_path / "invalid.yml").write_text("tools: wrong\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_HARNESS_AGENTS_DIR", str(tmp_path))
    from ah.core import agent_def

    monkeypatch.setattr(agent_def.db, "fetch", AsyncMock(return_value=[]))
    monkeypatch.setattr(agent_def.db, "fetchrow", AsyncMock(return_value=None))
    registry = AgentRegistry()
    reviewer = await registry.get("reviewer")
    assert reviewer.source == "yaml"
    assert reviewer.tools == ["read_file"]
    assert reviewer.max_iterations == 4
    assert await registry.get("invalid") is None
    assert "reviewer" in {agent.name for agent in await registry.list()}

    monkeypatch.setattr(
        agent_def.db,
        "fetchrow",
        AsyncMock(
            return_value={
                "name": "reviewer",
                "description": "DB review",
                "system_prompt": "",
                "tools": "[]",
                "model": None,
                "provider": None,
                "max_iterations": 2,
                "source": "db",
            }
        ),
    )
    assert (await registry.get("reviewer")).description == "DB review"


@pytest.mark.asyncio
async def test_delegation_passes_parent_context_and_records_result(monkeypatch):
    from ah.core import orchestrator as module

    parent_id = uuid.uuid4()
    child_id = uuid.uuid4()
    seen = []

    class Agent:
        async def run(self, session_id, task, verbose=False):
            seen.append((session_id, task))
            return AgentResponse(content="Reviewed", tool_calls=[], tokens_used=7, iterations=1)

    monkeypatch.setattr(module.agent_registry, "get", AsyncMock(return_value=AgentDef("reviewer")))
    monkeypatch.setattr(
        module.session_manager, "create", AsyncMock(return_value=SimpleNamespace(id=child_id))
    )
    monkeypatch.setattr(
        module.session_manager,
        "get_fresh",
        AsyncMock(return_value=SimpleNamespace(goal="Ship phase 5")),
    )
    monkeypatch.setattr(
        module.context_manager,
        "get_recent_context",
        AsyncMock(
            return_value=[
                {
                    "type": "user_message",
                    "payload": {"content": "Check module A"},
                    "tokens": 4,
                }
            ]
        ),
    )
    add_chunk = AsyncMock()
    monkeypatch.setattr(module.context_manager, "add_chunk", add_chunk)
    orch = Orchestrator(agent_factory=lambda _definition: Agent())
    monkeypatch.setattr(orch, "_record_start", AsyncMock(return_value=uuid.uuid4()))
    monkeypatch.setattr(orch, "_record_end", AsyncMock())

    result = await orch.delegate("reviewer", "Review code", parent_session_id=parent_id)
    assert result.status == "complete"
    assert seen[0][0] == child_id
    assert "Review code" in seen[0][1]
    assert "Goal: Ship phase 5" in seen[0][1]
    assert "Check module A" in seen[0][1]
    assert add_chunk.await_args.kwargs["session_id"] == parent_id
    assert add_chunk.await_args.kwargs["chunk_type"] == "result"
    assert add_chunk.await_args.kwargs["payload"]["content"] == "[reviewer] Reviewed"


@pytest.mark.asyncio
async def test_delegate_tool_uses_isolated_session_context(monkeypatch):
    from ah.core import orchestrator as module
    from ah.tools.agents import current_agent_id, current_session_id, delegate

    seen = []

    async def fake_delegate(
        agent, task, *, from_agent, parent_session_id, _hop_count, authority=None
    ):
        await asyncio.sleep(0)
        seen.append(parent_session_id)
        return SimpleNamespace(status="complete", response="done")

    monkeypatch.setattr(module.orchestrator, "delegate", fake_delegate)

    async def fake_owner(*args):
        return "harness"

    monkeypatch.setattr("ah.tools.agents.db.fetchval", fake_owner)

    async def call(session_id):
        token = current_session_id.set(session_id)
        agent_token = current_agent_id.set("harness")
        try:
            await delegate("reviewer", "Review")
        finally:
            current_agent_id.reset(agent_token)
            current_session_id.reset(token)

    ids = [uuid.uuid4(), uuid.uuid4()]
    await asyncio.gather(*(call(session_id) for session_id in ids))
    assert set(seen) == set(ids)
    assert current_session_id.get() is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name",
    ["delegate", "remember", "recall", "share_memory", "session_recall", "session_recall_window"],
)
async def test_agent_tool_execution_sets_and_resets_session_context(monkeypatch, tool_name):
    from ah.core import agent as agent_module
    from ah.core.agent import ReActAgent
    from ah.tools.agents import current_agent_id, current_session_id

    session_id = uuid.uuid4()
    seen = []

    async def execute(name, **kwargs):
        seen.append((current_session_id.get(), current_agent_id.get()))
        return "done"

    monkeypatch.setattr(agent_module.registry, "execute", execute)
    monkeypatch.setattr(agent_module.context_manager, "add_chunk", AsyncMock())
    agent = ReActAgent.__new__(ReActAgent)
    agent.allowed_tools = None
    agent.agent_id = "harness"
    response = LLMResponse(
        content="",
        model="test",
        tool_calls=[
            {
                "id": "call-1",
                "function": {"name": tool_name, "arguments": "{}"},
            }
        ],
    )
    events = [
        event async for event in agent._execute_tool_calls_stream(response, [], [], session_id)
    ]
    assert [event.type for event in events] == ["tool_call", "tool_result"]
    assert seen == [(session_id, "harness")]
    assert current_session_id.get() is None
    assert current_agent_id.get() is None
