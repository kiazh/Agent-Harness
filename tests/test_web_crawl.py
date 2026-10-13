"""Bounded multi-page crawl (web_crawl) indexes pages into the session RAG.

Policy under test: same-host by default, hard bounds on pages/depth/time,
redacted-URL dedup against already-indexed session sources, per-page failure
accounting, and the research-session scoping the RAG pipeline already enforces.
"""

import uuid
from unittest.mock import AsyncMock

import pytest

from machina.core.exceptions import ToolError, ValidationError
from machina.tools import web_crawl as crawl_module


def _page(links=(), body="Useful page text."):
    anchors = "".join(f'<a href="{href}">next</a>' for href in links)
    return f"<html><body><p>{body}</p>{anchors}</body></html>"


@pytest.fixture
def stubs(monkeypatch):
    sid = uuid.uuid4()
    pipeline = type("Pipeline", (), {"index_document": AsyncMock(return_value=[object()])})()
    monkeypatch.setattr(
        crawl_module, "active_agent_scope", AsyncMock(return_value=("harness", sid))
    )
    monkeypatch.setattr(crawl_module, "get_rag_pipeline", AsyncMock(return_value=pipeline))
    monkeypatch.setattr(crawl_module.db, "fetchval", AsyncMock(return_value=False))
    return sid, pipeline


async def test_crawl_bfs_same_host_and_depth_bounds(monkeypatch, stubs):
    sid, pipeline = stubs
    pages = {
        "https://example.com/": _page(
            links=("/second", "https://other.test/away"), body="Root page."
        ),
        "https://example.com/second": _page(links=("/third",), body="Second page."),
        "https://example.com/third": _page(body="Third page."),
    }
    fetch = AsyncMock(side_effect=lambda url, **kw: pages[url])
    monkeypatch.setattr(crawl_module, "fetch_page_html", fetch)

    response = await crawl_module.web_crawl("https://example.com/", max_pages=5, max_depth=1)

    fetched = [call.args[0] for call in fetch.await_args_list]
    assert fetched == ["https://example.com/", "https://example.com/second"]
    assert "https://other.test/away" not in fetched, "cross-host links are not followed"
    assert "https://example.com/third" not in fetched, "depth bound stops at links+1"
    indexed_sources = [c.kwargs["source"].source for c in pipeline.index_document.await_args_list]
    assert indexed_sources == ["https://example.com/", "https://example.com/second"]
    indexed = pipeline.index_document.await_args_list[0].kwargs
    assert indexed["session_id"] == sid and indexed["agent_id"] == "harness"
    assert "https://example.com/" in response and "Root page." in response


async def test_crawl_max_pages_bounds_fetches(monkeypatch, stubs):
    _, _ = stubs
    root = _page(links=tuple(f"/p{i}" for i in range(1, 8)))
    fetch = AsyncMock(side_effect=lambda url, **kw: root if url.endswith("/") else _page())
    monkeypatch.setattr(crawl_module, "fetch_page_html", fetch)

    await crawl_module.web_crawl("https://example.com/", max_pages=3, max_depth=2)

    assert fetch.await_count == 3


async def test_crawl_reuses_already_indexed_pages_without_reindex(monkeypatch, stubs):
    _, pipeline = stubs
    monkeypatch.setattr(crawl_module.db, "fetchval", AsyncMock(return_value=True))
    fetch = AsyncMock(return_value=_page())
    monkeypatch.setattr(crawl_module, "fetch_page_html", fetch)

    response = await crawl_module.web_crawl("https://example.com/", max_depth=0)

    assert fetch.await_count == 0, "an indexed seed at max depth needs no fetch"
    pipeline.index_document.assert_not_awaited()
    assert "already saved" in response


async def test_crawl_dedup_key_is_the_redacted_url(monkeypatch, stubs):
    _, _ = stubs
    raw = "https://example.com/guide?api_key=abcdefghijklmnopqrstuvw123456"
    fetchval = AsyncMock(return_value=False)
    monkeypatch.setattr(crawl_module.db, "fetchval", fetchval)
    monkeypatch.setattr(crawl_module, "fetch_page_html", AsyncMock(return_value=_page()))

    await crawl_module.web_crawl(raw, max_depth=0)

    from machina.memory.redaction import redact_secrets

    assert fetchval.await_args.args[-1] == redact_secrets(raw).text


@pytest.mark.parametrize(
    "kwargs",
    [
        {"url": ""},
        {"url": "ftp://example.com"},
        {"url": "https://example.com", "max_pages": "many"},
        {"url": "https://example.com", "max_pages": 0},
        {"url": "https://example.com", "max_depth": 99},
    ],
)
async def test_crawl_input_validation(kwargs):
    with pytest.raises(ValidationError):
        await crawl_module.web_crawl(**kwargs)


async def test_crawl_counts_partial_failures_and_raises_when_nothing_saved(monkeypatch, stubs):
    _, pipeline = stubs
    fetch = AsyncMock(side_effect=ToolError("boom"))
    monkeypatch.setattr(crawl_module, "fetch_page_html", fetch)

    with pytest.raises(ToolError, match="no pages"):
        await crawl_module.web_crawl("https://example.com/")
    pipeline.index_document.assert_not_awaited()


async def test_crawl_reports_failures_but_returns_saved_pages(monkeypatch, stubs):
    _, pipeline = stubs
    pages = {"https://example.com/": _page(links=("/dead",))}
    dead = ToolError("fetch failed")

    async def fetch(url, **kw):
        if url in pages:
            return pages[url]
        raise dead

    monkeypatch.setattr(crawl_module, "fetch_page_html", fetch)

    response = await crawl_module.web_crawl("https://example.com/", max_depth=1)

    assert pipeline.index_document.await_count == 1
    assert "could not be fetched" in response
    assert "untrusted" in response
