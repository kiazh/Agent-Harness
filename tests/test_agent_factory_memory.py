"""Factory-built agents carry the session provider into the consolidator.

Regression for the 2026-10-13 bug: ``MemoryConsolidator()`` was constructed
with no provider, so extraction raised "no LLM provider available for memory
extraction" and every consolidation failed — masked by a shutdown race that
killed the task before it could even reach extraction.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

from machina.core import agent_factory


class _StubProvider:
    model = "stub-model"


async def test_factory_wires_provider_into_memory_consolidator(monkeypatch):
    stub = _StubProvider()
    monkeypatch.setattr("machina.core.provider.get_provider", lambda **kwargs: stub)
    monkeypatch.setattr(agent_factory, "_memory_enabled", lambda: True)
    monkeypatch.setattr(agent_factory, "_shared_rag_pipeline", AsyncMock(return_value=None))
    session = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id="harness",
        provider="deepseek",
        model="deepseek-chat",
        delegation_authority={},
    )

    agent = await agent_factory.build_agent_for_session(session)

    assert agent.memory_consolidator is not None
    assert agent.memory_consolidator.llm is stub
    assert agent.memory_retriever is not None


async def test_cancelled_rag_acquisition_closes_created_provider(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock
    from machina.core import agent_factory
    import pytest
    from machina.core.agent_def import agent_registry

    llm = SimpleNamespace(close=AsyncMock())
    monkeypatch.setattr("machina.core.provider.get_provider", lambda **kwargs: llm)
    monkeypatch.setattr(agent_registry, "get", AsyncMock(return_value=None))
    monkeypatch.setattr(agent_factory, "_memory_enabled", lambda: False)

    async def cancelled():
        raise asyncio.CancelledError

    monkeypatch.setattr(agent_factory, "_shared_rag_pipeline", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await agent_factory.build_agent_for_session(
            SimpleNamespace(
                agent_id="harness", provider="openai", model="test", delegation_authority={}
            )
        )
    llm.close.assert_awaited_once()
