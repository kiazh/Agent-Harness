"""DuckDuckGo results can flow into safe page extraction."""

import httpx

from machina.security import fetch
from machina.tools import builtins
from machina.tools.builtins import _parse_duckduckgo_results


def test_duckduckgo_links_accept_attribute_order_and_extra_classes():
    html = (
        '<a href="https://example.test/one" class="result__a title">First <b>result</b></a>'
        '<a class="title result__a" data-id="2" href="https://example.test/two">Second &amp; last</a>'
    )
    assert _parse_duckduckgo_results(html) == [
        ("https://example.test/one", "First result"),
        ("https://example.test/two", "Second & last"),
    ]


async def test_duckduckgo_redirect_result_can_be_extracted(monkeypatch):
    html = (
        '<a class="result__a" '
        'href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.python.org%2F3%2F">'
        "Python documentation</a>"
    )
    monkeypatch.setattr(builtins.config, "get", lambda key: "" if key == "searxng_url" else None)
    monkeypatch.setattr(
        builtins.httpx,
        "get",
        lambda *args, **kwargs: httpx.Response(200, text=html),
    )
    result = await builtins.web_search("python documentation")
    assert "https://docs.python.org/3/" in result
    assert "duckduckgo.com/l/" not in result
    source_url = next(
        line.strip() for line in result.splitlines() if line.strip().startswith("https://")
    )

    seen = []

    def pinned(url, *, max_bytes, timeout):
        seen.append(url)
        return "Python language reference"

    monkeypatch.setattr(fetch, "pinned_fetch", pinned)
    content = await builtins.web_extract(source_url)
    assert content == "Python language reference"
    assert seen == ["https://docs.python.org/3/"]


def test_duckduckgo_redirects_do_not_publish_unsafe_urls():
    html = (
        '<a class="result__a" href="//duckduckgo.com/l/?uddg=file%3A%2F%2F%2Fetc%2Fpasswd">file</a>'
        '<a class="result__a" href="//duckduckgo.com/l/?uddg=http%3A%2F%2Flocalhost%2Fadmin">local</a>'
        '<a class="result__a" href="javascript:alert(1)">script</a>'
        '<a class="result__a" href="https://example.com/article">article</a>'
    )
    assert _parse_duckduckgo_results(html) == [("https://example.com/article", "article")]
