"""Regressions for session-scoped retrieval and private file ingestion."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from ah.core.exceptions import ToolError, ValidationError
from ah.rag.loaders import FileLoader
from ah.tools import rag
from ah.tools.agents import current_agent_id, current_session_id


@pytest.mark.parametrize("operation", ["index_document", "search_documents"])
async def test_rag_tool_rejects_another_session(monkeypatch, operation):
    own_session = uuid.uuid4()
    other_session = uuid.uuid4()
    owner_token = current_agent_id.set("owner")
    session_token = current_session_id.set(own_session)
    monkeypatch.setattr("ah.tools.agents.db.fetchval", AsyncMock(return_value="owner"))
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
    monkeypatch.setattr("ah.tools.agents.db.fetchval", AsyncMock(return_value="owner"))
    pipeline = AsyncMock()
    pipeline.search.return_value = []
    monkeypatch.setattr(rag, "get_rag_pipeline", AsyncMock(return_value=pipeline))
    try:
        assert "No results" in await rag.search_documents("query", str(session))
        pipeline.search.assert_awaited_once()
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
