"""Extraction enforces SSRF policy at the connection, including redirects."""

import pytest

from machina.core.exceptions import ToolError
from machina.security import fetch
from machina.tools import builtins
from tests.test_handoff_regressions import _fake_pinned_transport, _ok


@pytest.fixture(autouse=True)
def forbid_proxy_and_stage_rebinding(monkeypatch):
    # Simulate preliminary validation of a safe DNS answer. The fetch must
    # validate its own answer and use the selected IP, never a third party.
    monkeypatch.setattr(builtins, "_is_safe_url", lambda _: True)

    def forbidden_proxy(*args, **kwargs):
        raise AssertionError("extraction proxy must not receive target URLs")

    monkeypatch.setattr(builtins.httpx, "AsyncClient", forbidden_proxy)


async def test_extract_connects_directly_to_validated_address(monkeypatch):
    connected = _fake_pinned_transport(
        monkeypatch,
        {
            "example.com": (
                ["93.184.216.34"],
                lambda: _ok(
                    b"<html><style>hidden style</style><body><h1>Title</h1><p>Useful text</p>"
                    b"<script>hidden code</script></body></html>"
                ),
            )
        },
    )
    result = await builtins.web_extract("https://example.com/article")
    assert connected == ["93.184.216.34"]
    assert "Title" in result and "Useful text" in result
    assert "hidden" not in result and "<html>" not in result


@pytest.mark.parametrize(
    "address",
    [
        "10.0.0.1",
        "127.0.0.1",
        "169.254.169.254",
        "224.0.0.1",
        "240.0.0.1",
        "0.0.0.0",
        "::1",
        "fe80::1",
        "ff02::1",
        "::",
    ],
)
async def test_rebinding_to_nonpublic_target_cannot_connect(monkeypatch, address):
    connected = _fake_pinned_transport(
        monkeypatch, {"example.com": ([address], lambda: _ok(b"internal"))}
    )
    with pytest.raises(ToolError, match="not public"):
        await builtins.web_extract("https://example.com/article")
    assert connected == []


async def test_redirect_to_private_destination_is_revalidated(monkeypatch):
    connected = _fake_pinned_transport(
        monkeypatch,
        {
            "example.com": (
                ["93.184.216.34"],
                lambda: (302, {"Location": "http://internal.test/credentials"}, []),
            ),
            "internal.test": (["10.0.0.1"], lambda: _ok(b"credentials")),
        },
    )
    with pytest.raises(ToolError, match="not public"):
        await builtins.web_extract("https://example.com/article")
    assert connected == ["93.184.216.34"]


async def test_extract_body_limit_is_enforced_during_transfer(monkeypatch):
    _fake_pinned_transport(
        monkeypatch,
        {"example.com": (["93.184.216.34"], lambda: _ok(b"x" * (builtins._EXTRACT_MAX_BYTES + 1)))},
    )
    with pytest.raises(ToolError, match="byte cap"):
        await builtins.web_extract("https://example.com/article")


async def test_extract_preserves_plain_text(monkeypatch):
    _fake_pinned_transport(
        monkeypatch, {"example.com": (["93.184.216.34"], lambda: _ok(b"a < b and c > d"))}
    )
    assert await builtins.web_extract("https://example.com/article") == "a < b and c > d"


async def test_extract_propagates_fetch_deadline_without_proxy(monkeypatch):
    def expired(url, *, max_bytes, timeout):
        assert max_bytes == builtins._EXTRACT_MAX_BYTES and timeout == 30
        raise fetch.FetchError("fetch exceeded overall deadline")

    monkeypatch.setattr(fetch, "pinned_fetch", expired)
    with pytest.raises(ToolError, match="overall deadline"):
        await builtins.web_extract("https://example.com/article")


async def test_large_real_page_is_extracted_not_rejected(monkeypatch):
    """Pages far beyond the old 20 KB raw cap extract successfully now."""
    body = b"<p>Sentence about widgets.</p>" * 2_000  # ~56 KB of HTML
    _fake_pinned_transport(monkeypatch, {"example.com": (["93.184.216.34"], lambda: _ok(body))})
    result = await builtins.web_extract("https://example.com/article")
    assert "Sentence about widgets." in result
    assert len(result) > 5_000  # beyond the old 5 KB text truncation
    assert len(result) == builtins._EXTRACT_TEXT_LIMIT


async def test_extracted_text_honors_custom_limit(monkeypatch):
    def big_text(url, *, max_bytes, timeout):
        assert max_bytes == builtins._EXTRACT_MAX_BYTES
        return "plain words " * 30_000

    monkeypatch.setattr(fetch, "pinned_fetch", big_text)
    defaulted = await builtins.extract_url_text("https://example.com/article")
    assert len(defaulted) == builtins._EXTRACT_TEXT_LIMIT
    custom = await builtins.extract_url_text("https://example.com/article", text_limit=60_000)
    assert len(custom) == 60_000
