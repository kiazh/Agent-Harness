"""Regressions for session-scoped retrieval and private file ingestion."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from machina.core.exceptions import ToolError, ValidationError
from machina.rag.loaders import FileLoader
from machina.tools import rag
from machina.tools.agents import current_agent_id, current_session_id


@pytest.mark.parametrize("operation", ["index_document", "search_documents"])
async def test_rag_tool_rejects_another_session(monkeypatch, operation):
    own_session = uuid.uuid4()
    other_session = uuid.uuid4()
    owner_token = current_agent_id.set("owner")
    session_token = current_session_id.set(own_session)
    monkeypatch.setattr("machina.tools.agents.db.fetchval", AsyncMock(return_value="owner"))
    pipeline = AsyncMock()
    monkeypatch.setattr(rag, "get_rag_pipeline", AsyncMock(return_value=pipeline))
    try:
        with pytest.raises(ToolError, match="session"):
            if operation == "index_document":
                await rag.index_document("notes.md", str(other_session))
            else:
                await rag.search_documents("secret", str(other_session))
        pipeline.index_document.assert_not_called()
        pipeline.search.assert_not_called()
    finally:
        current_session_id.reset(session_token)
        current_agent_id.reset(owner_token)


async def test_rag_tool_allows_its_own_session(monkeypatch):
    session = uuid.uuid4()
    owner_token = current_agent_id.set("owner")
    session_token = current_session_id.set(session)
    monkeypatch.setattr("machina.tools.agents.db.fetchval", AsyncMock(return_value="owner"))
    pipeline = AsyncMock()
    pipeline.search.return_value = []
    monkeypatch.setattr(rag, "get_rag_pipeline", AsyncMock(return_value=pipeline))
    try:
        assert "No results" in await rag.search_documents("query", str(session))
        pipeline.search.assert_awaited_once()
    finally:
        current_session_id.reset(session_token)
        current_agent_id.reset(owner_token)


async def test_rag_tool_defaults_to_the_active_session(monkeypatch):
    """Omitted / 'current' session IDs resolve to the active session, not an error."""
    session = uuid.uuid4()
    owner_token = current_agent_id.set("owner")
    session_token = current_session_id.set(session)
    monkeypatch.setattr("machina.tools.agents.db.fetchval", AsyncMock(return_value="owner"))
    pipeline = AsyncMock()
    pipeline.search.return_value = []
    monkeypatch.setattr(rag, "get_rag_pipeline", AsyncMock(return_value=pipeline))
    try:
        assert "No results" in await rag.search_documents("query")
        assert "No results" in await rag.search_documents("query", "current")
        assert "No results" in await rag.search_documents("query", "ACTIVE")
        assert pipeline.search.await_count == 3
        for call in pipeline.search.await_args_list:
            assert call.kwargs["session_id"] == session
    finally:
        current_session_id.reset(session_token)
        current_agent_id.reset(owner_token)


@pytest.mark.parametrize("name", [".env", ".env.local", ".npmrc", "id_rsa", ".git/config"])
def test_rag_loader_rejects_private_files(tmp_path, name):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("fake-secret", encoding="utf-8")
    loader = FileLoader(tmp_path)
    with pytest.raises(ValidationError, match="private"):
        loader.load(name)
    assert loader.load_directory(".") == []
