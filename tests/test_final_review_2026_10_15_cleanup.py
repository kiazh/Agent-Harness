"""Evidence tests for F12 (2026-10-15 review): failed delegation bookkeeping
must not leak the freshly built child provider or orphan the child session.

Finding (docs/verification/CODEBASE_REVIEW_FOLLOWUP_2026-10-15.md, F12):
``Orchestrator.delegate`` acquired the child session + owned provider, then
awaited ``_record_start`` *outside* the execution cleanup scope -- a DB error
or cancellation during that insert skipped cleanup entirely. The related
``agent_factory.py:137-188`` F4 finding: ``get_provider`` ran before an
unguarded ``MachinaAdapter.to_soulspec(...)`` step, so a raise there leaked the
provider.

These tests deliberately inject a failure and assert the cleanup contract.
They fail on the pre-fix code (RED) and pass once the ownership scope wraps
acquisition + start recording (GREEN).
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from machina.core.agent_def import AgentDef, agent_registry


class _RecordingProvider:
    """Raw provider stand-in: records that ``close`` was awaited."""

    def __init__(self) -> None:
        self.closed = 0

    async def close(self) -> None:
        self.closed += 1


class _OwnedAgent:
    """Owned-provider agent stand-in for the orchestrator default path."""

    def __init__(self) -> None:
        self.provider = _RecordingProvider()
        self._owns_provider = True


@pytest.fixture
def _stub_registry_and_session(monkeypatch):
    from machina.core import orchestrator as module

    child_id = uuid.uuid4()
    archived: list[uuid.UUID] = []

    async def fake_archive(session_id: uuid.UUID) -> None:
        archived.append(session_id)

    monkeypatch.setattr(
        module.agent_registry, "get", AsyncMock(return_value=AgentDef(name="coder"))
    )
    monkeypatch.setattr(
        module.session_manager,
        "create",
        AsyncMock(return_value=SimpleNamespace(id=child_id)),
    )
    monkeypatch.setattr(module.session_manager, "archive", AsyncMock(side_effect=fake_archive))
    return module, child_id, archived


async def test_record_start_db_error_closes_provider_and_archives_child(
    monkeypatch, _stub_registry_and_session
):
    """A DB failure recording the start row must not orphan the child/provider."""
    module, child_id, archived = _stub_registry_and_session

    orch = module.Orchestrator()  # default factory => owns the provider
    agent = _OwnedAgent()

    async def fake_default_agent(*_args, **_kwargs):
        return agent

    monkeypatch.setattr(orch, "_default_agent", fake_default_agent)
    orch._record_start = AsyncMock(side_effect=RuntimeError("db down"))

    with pytest.raises(RuntimeError, match="db down"):
        await orch.delegate("coder", "work")

    # F12: the owned provider is closed...
    assert agent.provider.closed == 1
    # ...and the fresh child session is archived, not left active.
    assert archived == [child_id]


async def test_record_start_cancellation_closes_provider_and_archives_child(
    monkeypatch, _stub_registry_and_session
):
    """Cancellation during the start-row insert must run the same cleanup."""
    module, child_id, archived = _stub_registry_and_session

    orch = module.Orchestrator()
    agent = _OwnedAgent()

    async def fake_default_agent(*_args, **_kwargs):
        return agent

    monkeypatch.setattr(orch, "_default_agent", fake_default_agent)
    orch._record_start = AsyncMock(side_effect=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await orch.delegate("coder", "work")

    assert agent.provider.closed == 1
    assert archived == [child_id]


async def test_factory_closes_provider_when_soulspec_step_raises(monkeypatch):
    """agent_factory F4: a raise after ``get_provider`` must close the provider."""
    from machina.core import agent_factory
    from machina.soulspec.adapters import MachinaAdapter

    provider = _RecordingProvider()
    monkeypatch.setattr("machina.core.provider.get_provider", lambda **_kwargs: provider)
    monkeypatch.setattr(agent_factory, "_memory_enabled", lambda: False)
    monkeypatch.setattr(agent_factory, "_rag_enabled", lambda: False)
    monkeypatch.setattr(agent_factory, "_shared_rag_pipeline", AsyncMock(return_value=None))
    monkeypatch.setattr(agent_registry, "get", AsyncMock(return_value=AgentDef(name="coder")))

    def boom(_definition):
        raise RuntimeError("soulspec boom")

    monkeypatch.setattr(MachinaAdapter, "to_soulspec", staticmethod(boom))

    session = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id="coder",
        provider="deepseek",
        model="deepseek-chat",
        delegation_authority={},
    )

    with pytest.raises(RuntimeError, match="soulspec boom"):
        await agent_factory.build_agent_for_session(session)

    assert provider.closed == 1
