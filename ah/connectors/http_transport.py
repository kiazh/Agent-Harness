"""HTTP transports that dial policy-approved IPs while preserving Host and TLS."""

from __future__ import annotations

import asyncio
import importlib
import ipaddress
from collections.abc import Callable
from typing import Any


def pinned_http_transport(http_module: Any, resolve: Callable[[str], tuple[str, ...]]):
    """Support both HTTPX and the MCP SDK's HTTPX2 transport.

    The connection backend resolves and validates a hostname once, then passes
    only numeric addresses to the socket backend. HTTPcore keeps the original
    origin hostname for HTTP Host, TLS SNI and certificate verification.
    """
    core_name = {"httpx": "httpcore", "httpx2": "httpcore2"}[http_module.__name__]
    core = importlib.import_module(core_name)

    class PinnedBackend(core.AsyncNetworkBackend):
        def __init__(self):
            self._delegate = core.AnyIOBackend()

        async def connect_tcp(
            self, host, port, timeout=None, local_address=None, socket_options=None
        ):
            deadline = None if timeout is None else asyncio.get_running_loop().time() + timeout
            # Keep the complete validated answer set immutable across retries.
            # DNS consumes the same connect deadline as the socket attempts.
            try:
                async with asyncio.timeout(timeout):
                    approved = await asyncio.to_thread(resolve, host)
            except TimeoutError as exc:
                raise core.ConnectTimeout("destination resolution timed out") from exc
            addresses = tuple(str(ipaddress.ip_address(ip)) for ip in approved)
            if not addresses:
                raise core.ConnectError("destination has no approved addresses")
            for index, address in enumerate(addresses):
                remaining = (
                    None if deadline is None else deadline - asyncio.get_running_loop().time()
                )
                if remaining is not None and remaining <= 0:
                    raise core.ConnectTimeout("connection deadline expired")
                try:
                    return await self._delegate.connect_tcp(
                        address,
                        port,
                        timeout=remaining,
                        local_address=local_address,
                        socket_options=socket_options,
                    )
                except (core.ConnectError, core.ConnectTimeout):
                    if index == len(addresses) - 1:
                        raise

        async def connect_unix_socket(self, path, timeout=None, socket_options=None):
            raise core.ConnectError("connector HTTP transport requires TCP")

        async def sleep(self, seconds):
            await asyncio.sleep(seconds)

    class PinnedTransport(http_module.AsyncHTTPTransport):
        def __init__(self):
            limits = http_module.Limits()
            ssl_context = http_module.create_ssl_context(verify=True, trust_env=False)
            super().__init__(verify=ssl_context, trust_env=False, limits=limits)
            # HTTPX exposes no network_backend constructor argument. Its
            # AsyncHTTPTransport delegates to this pool in both locked HTTPX
            # versions; use HTTPcore's public pool/backend interfaces here.
            self._pool = core.AsyncConnectionPool(
                ssl_context=ssl_context,
                network_backend=PinnedBackend(),
                max_connections=limits.max_connections,
                max_keepalive_connections=limits.max_keepalive_connections,
                keepalive_expiry=limits.keepalive_expiry,
            )

    return PinnedTransport()
