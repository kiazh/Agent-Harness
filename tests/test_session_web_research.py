"""Online research is indexed only in the active agent session."""

import uuid
from unittest.mock import AsyncMock

import msgpack
import pytest

from machina.core.exceptions import ToolError
from machina.memory.redaction import redact_secrets
from machina.tools import web_research as research_module


async def test_web_research_fetches_and_indexes_with_session_and_source(monkeypatch):
    sid = uuid.uuid4()
    pipeline = type("Pipeline", (), {"index_document": AsyncMock(return_value=[object()])})()
    monkeypatch.setattr(
        research_module,
        "active_agent_scope",
        AsyncMock(return_value=("researcher", sid)),
    )
    monkeypatch.setattr(
        research_module,
        "_search_web_results",
        AsyncMock(return_value=[("Reference", "https://example.com/guide", "snippet")]),
    )
    monkeypatch.setattr(
        research_module,
        "extract_url_text",
        AsyncMock(return_value="Fetched reference text."),
    )
    monkeypatch.setattr(research_module.db, "fetchval", AsyncMock(return_value=False))
    monkeypatch.setattr(research_module, "acquire_rag_pipeline", AsyncMock(return_value=pipeline))

    response = await research_module.web_research("reference guide")

    assert "https://example.com/guide" in response
    assert "saved to this session" in response
    indexed = pipeline.index_document.await_args.kwargs
    assert indexed["session_id"] == sid
    assert indexed["agent_id"] == "researcher"
    assert indexed["source"].source == "https://example.com/guide"
    assert indexed["source"].content == "Fetched reference text."


async def test_web_research_does_not_duplicate_an_indexed_url(monkeypatch):
    sid = uuid.uuid4()
    pipeline = type("Pipeline", (), {"index_document": AsyncMock(return_value=[])})()
    monkeypatch.setattr(
        research_module,
        "active_agent_scope",
        AsyncMock(return_value=("harness", sid)),
    )
    monkeypatch.setattr(
        research_module,
        "_search_web_results",
        AsyncMock(return_value=[("Reference", "https://example.com/guide", "snippet")]),
    )
    monkeypatch.setattr(
        research_module,
        "extract_url_text",
        AsyncMock(return_value="Fetched reference text."),
    )
    monkeypatch.setattr(research_module.db, "fetchval", AsyncMock(return_value=True))
    monkeypatch.setattr(
        research_module.db,
        "fetch",
        AsyncMock(
            return_value=[
                {
                    "payload_msgpack": msgpack.packb(
                        {"text": "Saved reference text.", "metadata": {"chunk_index": 0}}
                    )
                }
            ]
        ),
    )
    monkeypatch.setattr(research_module, "acquire_rag_pipeline", AsyncMock(return_value=pipeline))

    response = await research_module.web_research("reference guide")

    assert "already saved in this session" in response
    assert "Saved reference text." in response
    research_module.extract_url_text.assert_not_awaited()
    pipeline.index_document.assert_not_awaited()


@pytest.fixture
def research_stubs(monkeypatch):
    sid = uuid.uuid4()
    pipeline = type("Pipeline", (), {"index_document": AsyncMock(return_value=[object()])})()
    monkeypatch.setattr(
        research_module, "active_agent_scope", AsyncMock(return_value=("harness", sid))
    )
    monkeypatch.setattr(research_module, "acquire_rag_pipeline", AsyncMock(return_value=pipeline))
    monkeypatch.setattr(research_module.db, "fetchval", AsyncMock(return_value=False))
    monkeypatch.setattr(
        research_module.db,
        "fetch",
        AsyncMock(
            return_value=[
                {
                    "payload_msgpack": msgpack.packb(
                        {"text": "Saved page text.", "metadata": {"chunk_index": 0}}
                    )
                }
            ]
        ),
    )
    return pipeline


async def test_research_page_limit_bounds_failed_fetch_attempts(monkeypatch, research_stubs):
    results = [(f"Page {i}", f"https://example.com/{i}", "") for i in range(6)]
    monkeypatch.setattr(research_module, "_search_web_results", AsyncMock(return_value=results))
    extract = AsyncMock(side_effect=ToolError("Page unavailable"))
    monkeypatch.setattr(research_module, "extract_url_text", extract)

    with pytest.raises(ToolError, match="no pages"):
        await research_module.web_research("question", limit=3)

    assert extract.await_count == 3


async def test_research_does_not_claim_empty_index_was_saved(monkeypatch, research_stubs):
    research_stubs.index_document.return_value = []
    monkeypatch.setattr(
        research_module,
        "_search_web_results",
        AsyncMock(return_value=[("Page", "https://example.com/page", "")]),
    )
    monkeypatch.setattr(research_module, "extract_url_text", AsyncMock(return_value="Page text"))

    with pytest.raises(ToolError, match="no pages"):
        await research_module.web_research("question")


async def test_research_duplicate_check_matches_stored_redacted_source(monkeypatch, research_stubs):
    url = "https://example.com/guide?api_key=abcdefghijklmnopqrstuvw123456"
    monkeypatch.setattr(
        research_module, "_search_web_results", AsyncMock(return_value=[("Page", url, "")])
    )
    monkeypatch.setattr(research_module, "extract_url_text", AsyncMock(return_value="Page text"))
    exists = AsyncMock(return_value=True)
    monkeypatch.setattr(research_module.db, "fetchval", exists)

    await research_module.web_research("question")

    assert exists.await_args.args[-1] == redact_secrets(url).text
    research_stubs.index_document.assert_not_awaited()


async def test_research_duplicate_results_do_not_consume_page_limit(monkeypatch, research_stubs):
    first = ("First", "https://example.com/first", "")
    second = ("Second", "https://example.com/second", "")
    monkeypatch.setattr(
        research_module,
        "_search_web_results",
        AsyncMock(return_value=[first, first, first, second]),
    )
    extract = AsyncMock(return_value="Page text")
    monkeypatch.setattr(research_module, "extract_url_text", extract)

    response = await research_module.web_research("question", limit=2)

    assert extract.await_count == 2
    assert "https://example.com/second" in response
