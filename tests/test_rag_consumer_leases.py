"""Production consumers retain their RAG generation across configuration rotation."""

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.parametrize("consumer", ["index", "search", "research", "crawl"])
async def test_consumer_keeps_pipeline_open_until_finished(monkeypatch, consumer):
    from machina.api.routes import documents
    from machina.api.routes.models import CreateDocumentRequest
    from machina.tools import rag, web_crawl, web_research

    entered, release = asyncio.Event(), asyncio.Event()
    embedder = SimpleNamespace(close=AsyncMock())
    reranker = SimpleNamespace(close=AsyncMock())
    sid = uuid.uuid4()

    async def pause():
        entered.set()
        await release.wait()
        embedder.close.assert_not_awaited()
        reranker.close.assert_not_awaited()

    async def index(*args, **kwargs):
        if consumer == "index":
            await pause()
        embedder.close.assert_not_awaited()
        return [object()]

    async def search(**kwargs):
        await pause()
        return []

    old = SimpleNamespace(
        _embedder=embedder, _reranker=reranker, index_document=index, search=search
    )
    newer = SimpleNamespace(_embedder=None, _reranker=None)
    identity = [(0, 0, 0)]
    monkeypatch.setattr(rag, "_rag_pipeline", old)
    monkeypatch.setattr(rag, "_rag_pipeline_identity", identity[0])
    monkeypatch.setattr(rag, "_pipeline_users", {})
    monkeypatch.setattr(rag, "_retired_pipelines", {})
    monkeypatch.setattr(rag, "_current_pipeline_identity", lambda: identity[0])
    monkeypatch.setattr(rag, "_built_reranker_identity", lambda pipeline: None)
    monkeypatch.setattr(rag, "_build_shared_pipeline", lambda: newer)
    monkeypatch.setattr(
        documents.session_manager,
        "get",
        AsyncMock(return_value=SimpleNamespace(agent_id="harness")),
    )

    async def fetch(*args, **kwargs):
        await pause()
        return "<html><body>Useful reference text.</body></html>"

    for module in (web_research, web_crawl):
        monkeypatch.setattr(module, "active_agent_scope", AsyncMock(return_value=("harness", sid)))
        monkeypatch.setattr(module.db, "fetchval", AsyncMock(return_value=False))
    monkeypatch.setattr(
        web_research,
        "_search_web_results",
        AsyncMock(return_value=[("Reference", "https://example.com", "summary")]),
    )
    monkeypatch.setattr(web_research, "extract_url_text", fetch)
    monkeypatch.setattr(web_crawl, "fetch_page_html", fetch)
    if consumer == "index":
        operation = documents.index_document(
            CreateDocumentRequest(sessionId=sid, content="reference", source="test")
        )
    elif consumer == "search":
        operation = documents.search_documents(sessionId=sid, query="reference", topK=5)
    elif consumer == "research":
        operation = web_research.web_research("reference")
    else:
        operation = web_crawl.web_crawl("https://example.com", max_depth=0)
    task = asyncio.create_task(operation)
    try:
        await asyncio.wait_for(entered.wait(), 5)
        identity[0] = (1, 0, 0)
        assert await rag.get_rag_pipeline() is newer
        embedder.close.assert_not_awaited()
        reranker.close.assert_not_awaited()
        release.set()
        await task
        embedder.close.assert_awaited_once()
        reranker.close.assert_awaited_once()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
