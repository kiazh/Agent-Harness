"""Final-review fixes for the secured fetch path (2026-10-15).

Covers: `_check_ip_public` now rejects every non-global address via
`ipaddress.is_global` (closing the CGNAT / cloud-metadata gap that the
individual `is_private`/`is_reserved`/... checks missed), and malformed
ports surface as typed `FetchError` instead of a bare `ValueError`.
"""

from __future__ import annotations

import pytest

from machina.security.fetch import FetchError, _check_ip_public, pinned_fetch


@pytest.mark.parametrize(
    "ip",
    [
        "100.100.100.200",  # Alibaba Cloud ECS metadata (CGNAT 100.64.0.0/10)
        "100.64.0.1",  # CGNAT range start
        "192.0.2.7",  # TEST-NET-1 (documentation)
        "198.18.0.9",  # benchmarking range
        "127.0.0.1",  # loopback
        "10.0.0.8",  # RFC1918
        "169.254.169.254",  # link-local metadata
        "0.0.0.0",  # unspecified
        "224.0.0.1",  # multicast
        "2001:db8::1",  # IPv6 documentation
        "::1",  # IPv6 loopback
    ],
)
def test_check_ip_public_rejects_non_global(ip):
    with pytest.raises(FetchError, match="not public"):
        _check_ip_public(ip)


def test_check_ip_public_allows_global_addresses():
    _check_ip_public("8.8.8.8")
    _check_ip_public("2606:4700:4700::1111")


def test_invalid_port_is_typed_fetch_error():
    with pytest.raises(FetchError, match="invalid port"):
        pinned_fetch("http://example.invalid:99999/")


def test_non_numeric_port_is_typed_fetch_error():
    with pytest.raises(FetchError, match="invalid port"):
        pinned_fetch("http://example.invalid:abc/")
