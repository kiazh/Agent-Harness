"""Serialize real HTTP requests while controlling only DNS and TCP sockets."""

from __future__ import annotations

import asyncio
import socket
import ssl

import httpcore
import pytest

from machina.connectors.oauth_network import OAuthNetworkClient, OAuthNetworkError


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

    from machina.connectors.mcp_client import MCPConnection
    from machina.connectors.models import ConnectionFailed, ConnectorConfig

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

    from machina.connectors.mcp_client import MCPConnection
    from machina.connectors.models import ConnectionFailed, ConnectorConfig

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

    from machina.connectors.http_transport import pinned_http_transport

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

    from machina.connectors.http_transport import pinned_http_transport

    module = importlib.import_module(library)
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "does-not-exist.pem"))
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "does-not-exist"))
    transport = pinned_http_transport(module, lambda _host: ("93.184.216.34",))
    context = transport._pool._ssl_context
    assert context.check_hostname
    assert context.verify_mode == ssl.CERT_REQUIRED


@pytest.mark.asyncio
async def test_oauth_destination_check_respects_deadline_and_keeps_loop_responsive(monkeypatch):
    import time

    def slow_dns(host, port, *args, **kwargs):
        time.sleep(0.3)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port or 0))]

    monkeypatch.setattr(socket, "getaddrinfo", slow_dns)
    client = OAuthNetworkClient(local_access=False)
    heartbeat = asyncio.create_task(asyncio.sleep(0.02))
    begin = asyncio.get_running_loop().time()
    try:
        with pytest.raises(OAuthNetworkError):
            await client._post_form(
                "https://slow-dns.example/token", {}, max_bytes=65536, deadline=begin + 0.05
            )
        elapsed = asyncio.get_running_loop().time() - begin
        assert heartbeat.done()
        assert elapsed < 0.15
    finally:
        await heartbeat
        await client.aclose()


def test_timed_out_dns_does_not_hold_asyncio_process_shutdown():
    import subprocess
    import sys
    from pathlib import Path

    script = """
import asyncio, sys, time
sys.path.insert(0, sys.argv[1])
import httpx
from machina.connectors.http_transport import pinned_http_transport

def resolve(host):
    time.sleep(0.4)
    return ("93.184.216.34",)

transport = pinned_http_transport(httpx, resolve)
async def run():
    async with httpx.AsyncClient(transport=transport, trust_env=False, timeout=0.01) as client:
        try:
            await client.get("https://slow-dns.example/")
        except httpx.ConnectTimeout:
            pass
begin = time.monotonic()
asyncio.run(run())
print(time.monotonic() - begin)
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", script, str(Path(__file__).resolve().parents[1])],
        text=True,
        capture_output=True,
        timeout=5,
        check=True,
    )
    assert float(result.stdout.strip()) < 0.15


@pytest.mark.asyncio
@pytest.mark.parametrize("abandon", ["timeout", "cancel"])
async def test_abandoned_dns_bounds_workers_and_recovers_capacity(monkeypatch, abandon):
    import threading

    from machina.connectors import http_transport

    slots = threading.BoundedSemaphore(1)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    monkeypatch.setattr(http_transport, "_DNS_SLOTS", slots)

    def blocked():
        entered.set()
        try:
            release.wait(2)
            return "approved"
        finally:
            finished.set()

    task = asyncio.create_task(
        http_transport.run_dns_check(blocked, timeout=0.05 if abandon == "timeout" else None)
    )
    try:
        async with asyncio.timeout(1):
            while not entered.is_set():
                await asyncio.sleep(0.001)
        if abandon == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(TimeoutError):
                await task
        with pytest.raises(OSError, match="capacity"):
            await http_transport.run_dns_check(lambda: pytest.fail("excess worker"), timeout=0.1)
        assert not finished.is_set()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    # Joining a woken worker thread can stall past 1s on loaded machines;
    # the product path is already proven above, this only waits out scheduling.
    async with asyncio.timeout(5):
        while not finished.is_set() or not slots.acquire(blocking=False):
            await asyncio.sleep(0.001)
    slots.release()
    assert await http_transport.run_dns_check(lambda: "recovered", timeout=0.1) == "recovered"


@pytest.mark.asyncio
async def test_mixed_public_private_answers_fail_before_secret_transmission(monkeypatch):
    dialed = []

    def dns(host, port, *args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port or 0))
            for address in ("93.184.216.34", "127.0.0.1")
        ]

    async def connect(_backend, host, port, **kwargs):
        dialed.append(host)
        return CapturedStream()

    monkeypatch.setattr(socket, "getaddrinfo", dns)
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connect)
    client = OAuthNetworkClient(local_access=False)
    try:
        with pytest.raises(OAuthNetworkError):
            await client._post_form(
                "https://mixed.example/token",
                {"refresh_token": "synthetic-secret"},
                max_bytes=65536,
                deadline=asyncio.get_running_loop().time() + 1,
            )
        assert not dialed
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_rebound_global_does_not_corrupt_slot_accounting(monkeypatch):
    """A worker that outlives a rebinding of the global semaphore must
    release the instance it acquired. Otherwise a stale worker corrupts the
    new instance's count and capacity checks pass when they must fail."""
    import threading

    from machina.connectors import http_transport

    first = threading.BoundedSemaphore(1)
    second = threading.BoundedSemaphore(1)
    monkeypatch.setattr(http_transport, "_DNS_SLOTS", first)
    entered, release = threading.Event(), threading.Event()

    def blocked():
        entered.set()
        release.wait(2)
        return "approved"

    task = asyncio.create_task(http_transport.run_dns_check(blocked, timeout=0.05))
    try:
        async with asyncio.timeout(1):
            while not entered.is_set():
                await asyncio.sleep(0.001)
        with pytest.raises(TimeoutError):
            await task
        # Rebind while the first worker is still blocked, as test
        # monkeypatching (or future dynamic pools) can.
        monkeypatch.setattr(http_transport, "_DNS_SLOTS", second)
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0.05)
        assert second.acquire(blocking=False)
        second.release()
        assert first.acquire(blocking=False)
        first.release()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
