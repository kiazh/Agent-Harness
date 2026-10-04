"""Gateway memory_search passes session_id to retriever."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_memory_search_passes_session_id_to_retriever(monkeypatch):
    """memory_search should pass session_id to retriever.retrieve()."""
    from ah.gateway.features import memory_search

    mock_retriever = MagicMock()
    mock_retriever.retrieve = AsyncMock(return_value=[])
    monkeypatch.setattr("ah.memory.retriever.MemoryRetriever", MagicMock(return_value=mock_retriever))

    mock_gw = MagicMock()
    mock_gw.require_db = MagicMock()
    mock_gw.get_session = AsyncMock()

    session_id = uuid.uuid4()
    mock_session = MagicMock()
    mock_session.id = session_id
    mock_gw.get_session.return_value = mock_session

    await memory_search(mock_gw, {"query": "test query", "sessionId": str(session_id)})

    mock_retriever.retrieve.assert_awaited_once()
    call_kwargs = mock_retriever.retrieve.call_args.kwargs
    assert call_kwargs.get("session_id") == session_id


@pytest.mark.asyncio
async def test_memory_search_without_session_id_passes_none(monkeypatch):
    """memory_search should pass session_id=None when no sessionId param is provided."""
    from ah.gateway.features import memory_search

    mock_retriever = MagicMock()
    mock_retriever.retrieve = AsyncMock(return_value=[])
    monkeypatch.setattr("ah.memory.retriever.MemoryRetriever", MagicMock(return_value=mock_retriever))

    mock_gw = MagicMock()
    mock_gw.require_db = MagicMock()
    mock_gw.get_session = AsyncMock()

    await memory_search(mock_gw, {"query": "test query"})

    mock_retriever.retrieve.assert_awaited_once()
    call_kwargs = mock_retriever.retrieve.call_args.kwargs
    assert call_kwargs.get("session_id") is None
