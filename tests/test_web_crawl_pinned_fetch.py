"""Crawl reaches only public hosts through the pinned transport, fail-closed."""

import uuid
from unittest.mock import AsyncMock

import pytest

from machina.tools import builtins
from machina.tools import web_crawl as crawl_module
from tests.test_handoff_regressions import _fake_pinned_transport, _ok

ROOT = (
    b"<html><body><p>Root page.</p>"
    b'<a href="/second">next</a>'
    b'<a href="http://internal.test/secret">private</a></body></html>'
)
SECOND = b"<html><body><p>Second page.</p></body></html>"


@pytest.fixture(autouse=True)
def crawl_session_stubs(monkeypatch):
    """Transport coverage only: stub the session scope, RAG pipeline, and DB."""
    sid = uuid.uuid4()
    pipeline = type("Pipeline", (), {"index_document": AsyncMock(return_value=[object()])})()
    monkeypatch.setattr(
        crawl_module, "active_agent_scope", AsyncMock(return_value=("harness", sid))
    )
    monkeypatch.setattr(crawl_module, "get_rag_pipeline", AsyncMock(return_value=pipeline))
    monkeypatch.setattr(crawl_module.db, "fetchval", AsyncMock(return_value=False))
    return pipeline


@pytest.fixture(autouse=True)
def forbid_proxy_and_stage_rebinding(monkeypatch):
    monkeypatch.setattr(builtins, "_is_safe_url", lambda _: True)

    def forbidden_proxy(*args, **kwargs):
        raise AssertionError("crawl must not use the HTTP proxy")

    monkeypatch.setattr(builtins.httpx, "AsyncClient", forbidden_proxy)


async def test_crawl_connects_only_to_public_same_host_pages(monkeypatch):
    pages = [ROOT, SECOND]

    def responder():
        return _ok(pages.pop(0))

    connected = _fake_pinned_transport(monkeypatch, {"example.com": (["93.184.216.34"], responder)})
    outcome = await crawl_module.web_crawl("https://example.com/", max_pages=5, max_depth=1)

    assert connected == ["93.184.216.34", "93.184.216.34"]
    assert "Root page." in outcome and "Second page." in outcome
    assert "internal.test" not in outcome, "cross-host links are not followed"


async def test_crawl_fails_closed_when_followed_host_is_private(monkeypatch):
    def responder():
        return _ok(ROOT)

    _fake_pinned_transport(
        monkeypatch,
        {
            "example.com": (["93.184.216.34"], responder),
            "internal.test": (["10.0.0.1"], lambda: _ok(b"secrets")),
        },
    )
    outcome = await crawl_module.web_crawl(
        "https://example.com/", max_pages=5, max_depth=1, same_host=False
    )

    assert "could not be fetched" in outcome
    assert "secrets" not in outcome, "a non-public page never contributes text"
