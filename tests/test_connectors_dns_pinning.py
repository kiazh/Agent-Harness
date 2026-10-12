"""Serialize real HTTP requests while controlling only DNS and TCP sockets."""

from __future__ import annotations

import asyncio
import socket
import ssl

import httpcore
import pytest

from ah.connectors.oauth_network import OAuthNetworkClient, OAuthNetworkError


class CapturedStream(httpcore.AsyncNetworkStream):
    def __init__(self):
        self.payload = b""
        self.reads = 0
        self.tls_host = None
        self.ssl_context = None

    async def write(self, data, timeout=None):
        self.payload += data

    async def read(self, max_bytes, timeout=None):
        self.reads += 1
        if self.reads == 1:
            return b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n{}"
        return b""

    async def aclose(self):
        pass

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self.tls_host = server_hostname
        self.ssl_context = ssl_context
        return self

    def get_extra_info(self, info):
        return None


@pytest.mark.asyncio
async def test_oauth_dials_validated_numeric_ip_and_keeps_hostname_tls(monkeypatch):
    stream, dialed, lookups = CapturedStream(), [], []

    def dns(host, port, *args, **kwargs):
        lookups.append(host)
        # A third lookup of the untrusted name would rebind to loopback.
        address = "93.184.216.34" if len(lookups) <= 2 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port or 0))]

    async def connect(_backend, host, port, **kwargs):
        dialed.append(host)
        return stream

    monkeypatch.setattr(socket, "getaddrinfo", dns)
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connect)
    client = OAuthNetworkClient(local_access=False)
    try:
        await client._post_form(
            "https://rebinding.example/token",
            {"refresh_token": "synthetic-refresh"},
            auth=("client", "synthetic-secret"),
            max_bytes=65536,
            deadline=asyncio.get_running_loop().time() + 5,
        )
        assert dialed == ["93.184.216.34"]
        assert b"Host: rebinding.example\r\n" in stream.payload
        assert b"refresh_token=synthetic-refresh" in stream.payload
        assert b"Authorization: Basic " in stream.payload
        assert stream.tls_host == "rebinding.example"
        assert stream.ssl_context.verify_mode == ssl.CERT_REQUIRED
        assert stream.ssl_context.check_hostname
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("rebound", ["127.0.0.1", "10.1.2.3", "169.254.169.254"])
async def test_oauth_connect_time_private_rebinding_fails_before_secrets(monkeypatch, rebound):
    stream, dialed, lookups = CapturedStream(), [], []

    def dns(host, port, *args, **kwargs):
        lookups.append(host)
        address = "93.184.216.34" if len(lookups) == 1 else rebound
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port or 0))]

    async def connect(_backend, host, port, **kwargs):
        dialed.append(host)
        return stream

    monkeypatch.setattr(socket, "getaddrinfo", dns)
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connect)
    client = OAuthNetworkClient(local_access=False)
    try:
        with pytest.raises(OAuthNetworkError) as denied:
            await client._post_form(
                "https://rebinding.example/token",
                {"refresh_token": "synthetic-refresh"},
                auth=("client", "synthetic-secret"),
                max_bytes=65536,
                deadline=asyncio.get_running_loop().time() + 5,
            )
        assert denied.value.reason == "oauth_unsafe_destination"
        assert dialed == []
        assert stream.payload == b""
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_mcp_sdk_dials_numeric_ip_with_original_host_and_tls(monkeypatch):
    import httpcore2

    from ah.connectors.mcp_client import MCPConnection
    from ah.connectors.models import ConnectionFailed, ConnectorConfig

    stream, dialed = CapturedStream(), []

    def dns(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port or 0))]

    async def connect(_backend, host, port, **kwargs):
        dialed.append(host)
        return stream

    monkeypatch.setattr(socket, "getaddrinfo", dns)
    monkeypatch.setattr(httpcore2.AnyIOBackend, "connect_tcp", connect)
    connection = MCPConnection(
        ConnectorConfig(
            id="dns-pin",
            transport="streamable_http",
            endpoint="https://rebinding.example/mcp",
            call_timeout=1,
        )
    )
    try:
        # The deliberately minimal HTTP response is not an MCP initialize
        # result; only DNS/TCP are replaced, so the SDK rejects it normally.
        with pytest.raises(ConnectionFailed):
            await connection.connect()
        assert dialed and set(dialed) == {"93.184.216.34"}
        assert b"Host: rebinding.example\r\n" in stream.payload
        assert stream.tls_host == "rebinding.example"
        assert stream.ssl_context.verify_mode == ssl.CERT_REQUIRED
        assert stream.ssl_context.check_hostname
    finally:
        await connection.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize("address", ["127.0.0.1", "10.1.2.3"])
async def test_scoped_local_access_pins_approved_private_addresses(monkeypatch, address):
    stream, dialed = CapturedStream(), []

    def dns(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port or 0))]

    async def connect(_backend, host, port, **kwargs):
        dialed.append(host)
        return stream

    monkeypatch.setattr(socket, "getaddrinfo", dns)
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connect)
    client = OAuthNetworkClient(local_access=True)
    try:
        await client._post_form(
            "https://local.example/token",
            {},
            max_bytes=65536,
            deadline=asyncio.get_running_loop().time() + 5,
        )
        assert dialed == [address]
        assert stream.tls_host == "local.example"
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("library", ["oauth", "mcp"])
async def test_localhost_alias_cannot_bypass_metadata_denial_with_consent(monkeypatch, library):
    import httpcore2

    from ah.connectors.mcp_client import MCPConnection
    from ah.connectors.models import ConnectionFailed, ConnectorConfig

    dialed = []

    def dns(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", port or 0))]

    async def connect(_backend, host, port, **kwargs):
        dialed.append(host)
        return CapturedStream()

    monkeypatch.setattr(socket, "getaddrinfo", dns)
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connect)
    monkeypatch.setattr(httpcore2.AnyIOBackend, "connect_tcp", connect)
    if library == "oauth":
        client = OAuthNetworkClient(local_access=True)
        try:
            with pytest.raises(OAuthNetworkError):
                await client._post_form(
                    "https://metadata.localhost/token",
                    {},
                    max_bytes=65536,
                    deadline=asyncio.get_running_loop().time() + 5,
                )
        finally:
            await client.aclose()
    else:
        connection = MCPConnection(
            ConnectorConfig(
                id="metadata-alias",
                transport="streamable_http",
                endpoint="https://metadata.localhost/mcp",
                local_access=True,
            )
        )
        try:
            with pytest.raises(ConnectionFailed):
                await connection.connect()
        finally:
            await connection.disconnect()
    assert not dialed


@pytest.mark.asyncio
async def test_socket_fallback_uses_only_the_original_approved_ip_set(monkeypatch):
    stream, dialed = CapturedStream(), []

    def dns(host, port, *args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port or 0))
            for address in ("93.184.216.34", "93.184.216.35")
        ]

    async def connect(_backend, host, port, **kwargs):
        dialed.append(host)
        if len(dialed) == 1:
            raise httpcore.ConnectError("synthetic first address failure")
        return stream

    monkeypatch.setattr(socket, "getaddrinfo", dns)
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connect)
    client = OAuthNetworkClient(local_access=False)
    try:
        await client._post_form(
            "https://fallback.example/token",
            {},
            max_bytes=65536,
            deadline=asyncio.get_running_loop().time() + 5,
        )
        assert dialed == ["93.184.216.34", "93.184.216.35"]
        assert stream.tls_host == "fallback.example"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_pinned_backend_connect_timeout_includes_dns_resolution(monkeypatch):
    import time

    import httpx

    from ah.connectors.http_transport import pinned_http_transport

    dialed = []

    def slow_resolve(host):
        time.sleep(0.05)
        return ("93.184.216.34",)

    async def connect(_backend, host, port, **kwargs):
        dialed.append(host)
        return CapturedStream()

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connect)
    async with httpx.AsyncClient(
        transport=pinned_http_transport(httpx, slow_resolve), trust_env=False, timeout=0.01
    ) as client:
        with pytest.raises(httpx.ConnectTimeout):
            await client.get("https://slow-dns.example/")
    assert not dialed


@pytest.mark.asyncio
async def test_literal_https_loopback_requires_local_consent(monkeypatch):
    dialed = []

    async def connect(_backend, host, port, **kwargs):
        dialed.append(host)
        return CapturedStream()

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connect)
    client = OAuthNetworkClient(local_access=False)
    try:
        with pytest.raises(OAuthNetworkError) as denied:
            await client._post_form(
                "https://127.0.0.1/token",
                {},
                max_bytes=65536,
                deadline=asyncio.get_running_loop().time() + 5,
            )
        assert denied.value.reason == "oauth_unsafe_destination"
        assert not dialed
    finally:
        await client.aclose()


@pytest.mark.parametrize("library", ["httpx", "httpx2"])
def test_pinned_transport_ignores_environment_ca_paths(monkeypatch, tmp_path, library):
    import importlib

    from ah.connectors.http_transport import pinned_http_transport

    module = importlib.import_module(library)
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "does-not-exist.pem"))
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "does-not-exist"))
    transport = pinned_http_transport(module, lambda _host: ("93.184.216.34",))
    context = transport._pool._ssl_context
    assert context.check_hostname
    assert context.verify_mode == ssl.CERT_REQUIRED
