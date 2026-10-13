"""FM3-F4: full-value digests, truncated display only.

Two URLs differing only past char 300 and two write payloads differing only
past 100k must never share a digest (or an approval card); the display card
truncates with an explicit ``…(+N chars)`` marker instead.
"""

from __future__ import annotations

import hashlib

from machina.permissions.broker import _approval_view
from machina.permissions.policy import build_request
from machina.permissions.tools import action_for_tool


def _build(tool: str, args: dict):
    return build_request(tool=tool, **action_for_tool(tool, args))


def _view(req):
    return _approval_view(req, {"request_id": req.request_id, "status": "pending"})


def test_long_urls_differing_past_300_chars_have_distinct_digests():
    prefix = "https://example.com/" + "a" * 280  # exactly 300 chars
    url_a = prefix + "payload-A"
    url_b = prefix + "payload-B"
    assert len(prefix) == 300
    req_a = _build("web_extract", {"url": url_a})
    req_b = _build("web_extract", {"url": url_b})
    # The full value is bound and digested, never truncated before auth.
    assert req_a.network == [url_a]
    assert req_a.digest != req_b.digest
    # The display discloses hidden content explicitly; identity (digest) is
    # what distinguishes the two cards, never a silently truncated prefix.
    card_a = _view(req_a)
    assert card_a["network"][0].endswith(f"…(+{len(url_a) - 300} chars)")


def test_long_url_card_truncates_with_marker_only_for_display():
    url = "https://example.com/" + "a" * 320
    req = _build("web_extract", {"url": url})
    card = _view(req)
    shown = card["network"][0]
    assert shown != url
    assert shown.startswith(url[:300])
    assert shown.endswith(f"…(+{len(url) - 300} chars)")


def test_long_write_content_digest_binds_full_payload_and_reports_length():
    base = "x" * 100_000
    content_a = base + "TAIL-A"
    content_b = base + "TAIL-B"
    spec_a = action_for_tool("write_file", {"path": "notes.txt", "content": content_a})
    # The mapped action never truncates before authorization.
    assert spec_a["content"] == content_a
    req_a = _build("write_file", {"path": "notes.txt", "content": content_a})
    req_b = _build("write_file", {"path": "notes.txt", "content": content_b})
    assert req_a.digest != req_b.digest
    assert req_a.content_digest == hashlib.sha256(content_a.encode()).hexdigest()
    # Misreporting fixed: length reflects the full payload.
    assert req_a.content_length == len(content_a)
    assert len(req_a.content_preview) < len(content_a)
    assert req_a.content_preview.endswith(f"…(+{len(content_a) - 4000} chars)")


def test_short_values_are_not_marked_or_altered():
    url = "https://example.com/short"
    req = _build("web_extract", {"url": url})
    assert _view(req)["network"] == [url]
    content = "hello"
    req_w = _build("write_file", {"path": "n.txt", "content": content})
    assert req_w.content_length == len(content)
    assert req_w.content_preview == content
