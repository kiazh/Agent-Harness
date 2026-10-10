"""Extraction enforces SSRF policy at the connection, including redirects."""

import pytest

from ah.core.exceptions import ToolError
from ah.security import fetch
from ah.tools import builtins
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
        monkeypatch, {"example.com": (["93.184.216.34"], lambda: _ok(b"x" * 20_001))}
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
        assert max_bytes == 20_000 and timeout == 30
        raise fetch.FetchError("fetch exceeded overall deadline")

    monkeypatch.setattr(fetch, "pinned_fetch", expired)
    with pytest.raises(ToolError, match="overall deadline"):
        await builtins.web_extract("https://example.com/article")
