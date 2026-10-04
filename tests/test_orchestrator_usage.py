"""Delegation leaves provider usage accounting to the agent."""
from __future__ import annotations

import os
import uuid
from unittest.mock import AsyncMock

import pytest

from ah.core.agent_def import AgentDef
from ah.core.models import AgentResponse

TEST_DSN = os.environ.get("AGENT_HARNESS_TEST_DATABASE_URL", "")
needs_db = pytest.mark.skipif(not TEST_DSN, reason="AGENT_HARNESS_TEST_DATABASE_URL not set")


@pytest.fixture
async def connected_db():
    from ah.db.connection import db

    await db.connect()
    yield
    await db.close()


class FakeAgent:
    """Deterministic stand-in that records calls and returns a fixed response."""

    def __init__(self, definition: AgentDef) -> None:
        self.definition = definition

    async def run(self, session_id, user_message, verbose=True) -> AgentResponse:
        return AgentResponse(
            content=f"[{self.definition.name}] handled: {user_message[:60]}",
            tool_calls=[],
            tokens_used=11,
            iterations=1,
        )


@needs_db
@pytest.mark.usefixtures("connected_db")
@pytest.mark.asyncio
async def test_orchestrator_delegate_does_not_double_count(monkeypatch):
    """Delegation does not add a second reservation around the agent."""
    from ah.core.orchestrator import Orchestrator

    orch = Orchestrator(agent_factory=FakeAgent)

    reserve = AsyncMock(return_value=uuid.uuid4())
    finish = AsyncMock()
    monkeypatch.setattr("ah.core.usage.usage_store.reserve", reserve)
    monkeypatch.setattr("ah.core.usage.usage_store.finish", finish)

    parent = await _parent_session()
    result = await orch.delegate(
        "researcher", "find the latest release", parent_session_id=parent
    )

    assert result.status == "complete"
    reserve.assert_not_awaited()
    finish.assert_not_awaited()


@needs_db
@pytest.mark.usefixtures("connected_db")
@pytest.mark.asyncio
async def test_orchestrator_error_does_not_make_phantom_reservation(monkeypatch):
    """A failed delegate does not charge an extra wrapper request."""
    from ah.core.orchestrator import Orchestrator

    class ExplodingAgent:
        def __init__(self, definition: AgentDef) -> None:
            pass

        async def run(self, session_id, user_message, verbose=True):
            raise RuntimeError("agent exploded")

    orch = Orchestrator(agent_factory=ExplodingAgent)

    reserve = AsyncMock(return_value=uuid.uuid4())
    finish = AsyncMock()
    monkeypatch.setattr("ah.core.usage.usage_store.reserve", reserve)
    monkeypatch.setattr("ah.core.usage.usage_store.finish", finish)

    parent = await _parent_session()
    result = await orch.delegate(
        "researcher", "find the latest release", parent_session_id=parent
    )

    assert result.status == "error"
    reserve.assert_not_awaited()
    finish.assert_not_awaited()


@needs_db
@pytest.mark.usefixtures("connected_db")
@pytest.mark.asyncio
async def test_orchestrator_delegate_skips_usage_store_without_parent_session(monkeypatch):
    """Orchestrator.delegate() should NOT call usage_store when parent_session_id is None."""
    from ah.core.orchestrator import Orchestrator

    orch = Orchestrator(agent_factory=FakeAgent)

    reserve = AsyncMock(return_value=uuid.uuid4())
    finish = AsyncMock()
    monkeypatch.setattr("ah.core.usage.usage_store.reserve", reserve)
    monkeypatch.setattr("ah.core.usage.usage_store.finish", finish)

    result = await orch.delegate("researcher", "find the latest release")

    assert result.status == "complete"
    reserve.assert_not_awaited()
    finish.assert_not_awaited()


async def _parent_session() -> uuid.UUID:
    from ah.core.session import session_manager

    return (await session_manager.create(title="parent")).id
