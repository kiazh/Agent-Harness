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
