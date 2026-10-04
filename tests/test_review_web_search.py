"""DuckDuckGo fallback parses equivalent anchor markup."""

from ah.tools.builtins import _parse_duckduckgo_results


def test_duckduckgo_links_accept_attribute_order_and_extra_classes():
    html = (
        '<a href="https://example.test/one" class="result__a title">First <b>result</b></a>'
        '<a class="title result__a" data-id="2" href="https://example.test/two">Second &amp; last</a>'
    )
    assert _parse_duckduckgo_results(html) == [
        ("https://example.test/one", "First result"),
        ("https://example.test/two", "Second & last"),
    ]
