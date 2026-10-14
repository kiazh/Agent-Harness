"""Growth-progress listeners: fan-out isolation and background notifications."""

import uuid
from unittest.mock import AsyncMock

import pytest

from machina.core.agent import (
    _notify_growth,
    add_growth_listener,
    remove_growth_listener,
)


def test_listeners_fan_out_and_isolate_failures():
    seen = []

    def _record(sid, stage, detail):
        seen.append((sid, stage, detail))

    def _boom(sid, stage, detail):
        raise RuntimeError("listener blew up")

    add_growth_listener(_record)
    add_growth_listener(_boom)
    try:
        _notify_growth(uuid.uuid4(), "consolidation_done", "2 new memories")
        assert seen != []
        assert seen[0][1] == "consolidation_done"
    finally:
        remove_growth_listener(_record)
        remove_growth_listener(_boom)

    # Removing twice is safe; after removal nothing fires.
    remove_growth_listener(_boom)
    seen.clear()
    _notify_growth(uuid.uuid4(), "consolidation_done", "x")
    assert seen == []


@pytest.mark.asyncio
async def test_consolidation_notifies_only_on_new_memories(monkeypatch):
    from types import SimpleNamespace

    from machina.core.agent import BaseReActAgent

    agent = BaseReActAgent.__new__(BaseReActAgent)
    agent.agent_id = "harness"
    agent.memory_consolidator = AsyncMock()
    monkeypatch.setattr(
        "machina.core.agent.session_manager.get",
        AsyncMock(return_value=SimpleNamespace(state={}, id=uuid.uuid4())),
    )
    monkeypatch.setattr("machina.core.agent.context_manager.get_chunks", AsyncMock(return_value=[]))
    monkeypatch.setattr("machina.core.agent.session_manager.update_state_fields", AsyncMock())
    seen = []

    def _record(sid, stage, detail):
        seen.append((stage, detail))

    add_growth_listener(_record)
    try:
        agent.memory_consolidator.consolidate_session = AsyncMock(
            return_value=SimpleNamespace(entries=[], checkpoint=None)
        )
        await agent._consolidate_memories(uuid.uuid4())
        assert seen == []
        agent.memory_consolidator.consolidate_session = AsyncMock(
            return_value=SimpleNamespace(entries=[{"a": 1}, {"b": 2}], checkpoint=None)
        )
        await agent._consolidate_memories(uuid.uuid4())
        assert seen == [("consolidation_done", "2 new memories")]
    finally:
        remove_growth_listener(_record)


@pytest.mark.asyncio
async def test_review_notifies_only_when_staged(monkeypatch):
    from machina.core.agent import BaseReActAgent
    from machina.core.models import AgentResponse

    agent = BaseReActAgent.__new__(BaseReActAgent)
    agent.agent_id = "harness"
    agent.provider = AsyncMock()
    seen = []

    def _record(sid, stage, detail):
        seen.append((stage, detail))

    add_growth_listener(_record)
    try:
        import machina.skills.learning as learning_module

        monkeypatch.setattr(
            learning_module.learning_reviewer,
            "review_turn",
            AsyncMock(return_value=None),
        )
        await agent._review_learning(
            uuid.uuid4(),
            "did something reusable",
            AgentResponse(content="done " * 10, iterations=1),
        )
        assert seen == []
        monkeypatch.setattr(
            learning_module.learning_reviewer,
            "review_turn",
            AsyncMock(return_value={"id": "r1", "status": "pending", "autoApplied": False}),
        )
        await agent._review_learning(
            uuid.uuid4(),
            "did something reusable",
            AgentResponse(content="done " * 10, iterations=1),
        )
        assert seen == [("review_done", "suggested a learning skill")]
        # Auto-applied rows report the published skill as applied, with its name.
        seen.clear()
        monkeypatch.setattr(
            learning_module.learning_reviewer,
            "review_turn",
            AsyncMock(
                return_value={
                    "id": "r2",
                    "status": "approved",
                    "autoApplied": True,
                    "name": "deploy-checks",
                }
            ),
        )
        await agent._review_learning(
            uuid.uuid4(),
            "did something reusable",
            AgentResponse(content="done " * 10, iterations=1),
        )
        assert seen == [("review_done", "applied learned skill deploy-checks")]
    finally:
        remove_growth_listener(_record)


def test_profile_slug_rules():
    import re

    from machina.memory.user_profile import PROFILE_ID_RE, profile_slug

    assert re.fullmatch(PROFILE_ID_RE, "work")
    assert re.fullmatch(PROFILE_ID_RE, "my-work-2")
    assert not re.fullmatch(PROFILE_ID_RE, "My Work!")
    assert not re.fullmatch(PROFILE_ID_RE, "")
    assert profile_slug("My Work!") == "my-work"
    assert profile_slug("") == ""
