"""Delegation through an agent tool must stay within its owned session."""

from types import SimpleNamespace
from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_delegate_refuses_a_parent_session_owned_by_another_agent(monkeypatch):
    from ah.core import orchestrator as module
    from ah.core.exceptions import ToolError
    from ah.tools.agents import current_agent_id, current_session_id, delegate

    calls = []

    async def fake_owner(*args):
        return "another-agent"

    async def fake_delegate(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(status="complete", response="leaked")

    monkeypatch.setattr("ah.tools.agents.db.fetchval", fake_owner)
    monkeypatch.setattr(module.orchestrator, "delegate", fake_delegate)
    session_token = current_session_id.set(uuid4())
    agent_token = current_agent_id.set("researcher")
    try:
        with pytest.raises(ToolError, match="does not belong"):
            await delegate("coder", "summarize parent context")
    finally:
        current_agent_id.reset(agent_token)
        current_session_id.reset(session_token)
    assert calls == []


@pytest.mark.asyncio
async def test_delegate_records_the_actual_parent_agent(monkeypatch):
    from ah.core import orchestrator as module
    from ah.tools.agents import current_agent_id, current_session_id, delegate

    calls = []

    async def fake_owner(*args):
        return "researcher"

    async def fake_delegate(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(status="complete", response="done")

    monkeypatch.setattr("ah.tools.agents.db.fetchval", fake_owner)
    monkeypatch.setattr(module.orchestrator, "delegate", fake_delegate)
    session_id = uuid4()
    session_token = current_session_id.set(session_id)
    agent_token = current_agent_id.set("researcher")
    try:
        await delegate("coder", "check code")
    finally:
        current_agent_id.reset(agent_token)
        current_session_id.reset(session_token)
    assert calls == [
        {
            "from_agent": "researcher",
            "parent_session_id": session_id,
            "_hop_count": 1,
            # LP-08: the executing parent's authority propagates (None when
            # unrestricted) so the child cannot exceed it.
            "authority": None,
        }
    ]


def test_system_prompt_teaches_parallel_fan_out():
    from ah.core.agent import SYSTEM_PROMPT

    assert "list_agents" in SYSTEM_PROMPT
    assert "no more than three" in SYSTEM_PROMPT
    assert "never delegate to evade an approval" in SYSTEM_PROMPT


def test_delegate_description_advertises_parallel_fan_out():
    import ah.tools.agents  # noqa: F401 — registers delegate on the global registry
    from ah.tools.base import registry

    desc = next(d.description for d in registry.get_tool_definitions() if d.name == "delegate")
    assert "single block" in desc
    assert "no more than three" in desc


@pytest.mark.asyncio
async def test_parallel_delegate_fan_out_runs_every_subagent(tmp_path, monkeypatch):
    """One assistant message with three delegate calls runs all three agents."""
    import copy
    import json
    import uuid
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from ah.core.agent import BaseReActAgent, ReActAgent
    from ah.core.config import config
    from ah.core.models import LLMResponse, Session, StreamEvent
    from ah.permissions.broker import set_approval_handler

    monkeypatch.setenv("AGENT_HARNESS_SKILLS_DIR", str(tmp_path / "skills"))
    for field in ("memory_enabled", "rag_enabled", "personality_growth_enabled"):
        monkeypatch.setattr(config, field, False)
    session = Session(id=uuid.uuid4(), context_budget=8000)
    monkeypatch.setattr("ah.core.agent.session_manager.get", AsyncMock(return_value=session))
    monkeypatch.setattr("ah.core.agent.session_manager.update_activity", AsyncMock())
    monkeypatch.setattr(
        "ah.core.agent.context_manager.get_recent_context", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        "ah.core.agent.context_manager.search_archive_text", AsyncMock(return_value=[])
    )
    monkeypatch.setattr("ah.core.agent.context_manager.add_chunks_batch", AsyncMock())
    monkeypatch.setattr(BaseReActAgent, "_reserve_usage", AsyncMock(return_value=None))
    monkeypatch.setattr(BaseReActAgent, "_finish_usage", AsyncMock())

    async def unexpected_approval(card):
        pytest.fail("fan-out requested approval")

    set_approval_handler(unexpected_approval)

    from ah.core import orchestrator as orchestrator_module
    from ah.tools import agents as agents_module

    ran = []

    async def fake_owner(*args):
        return "harness"

    async def fake_delegate(agent, task, **kwargs):
        ran.append(agent)
        return SimpleNamespace(status="complete", response=f"{agent} done")

    monkeypatch.setattr(agents_module.db, "fetchval", fake_owner)
    monkeypatch.setattr(orchestrator_module.orchestrator, "delegate", fake_delegate)

    wanted = [("researcher", "gather facts"), ("coder", "fix the bug"), ("coder", "add tests")]

    class Provider:
        calls = []

        async def complete(self, **kwargs):
            self.calls.append(copy.deepcopy(kwargs))
            if len(self.calls) == 1:
                return LLMResponse(
                    content="",
                    model="fake",
                    usage={"total_tokens": 1},
                    tool_calls=[
                        {
                            "id": str(i),
                            "type": "function",
                            "function": {
                                "name": "delegate",
                                "arguments": json.dumps({"agent": a, "task": t}),
                            },
                        }
                        for i, (a, t) in enumerate(wanted)
                    ],
                )
            return LLMResponse(content="synthesized", model="fake", usage={"total_tokens": 1})

        async def stream_complete(self, **kwargs):
            yield StreamEvent(type="done", response=await self.complete(**kwargs))

    provider = Provider()
    provider.calls = []
    agent = ReActAgent(provider=provider, allowed_tools=["delegate"])
    try:
        response = await agent.run(session.id, "Research, fix, and test", verbose=False)
        assert response.content == "synthesized"
        assert sorted(ran) == ["coder", "coder", "researcher"]
        results = [
            message.get("content", "")
            for message in provider.calls[1]["messages"]
            if message.get("role") == "tool"
        ]
        assert len(results) == 3
        assert any("researcher done" in r for r in results)
    finally:
        set_approval_handler(None)
