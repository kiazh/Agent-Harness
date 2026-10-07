"""Pinned outbound fetch for skill URLs (5.8).

Destination policy is enforced AT the actual connection: the hostname is
resolved once with a bounded timeout, every returned address is checked
against private/loopback/multicast/reserved/link-local/unspecified ranges
(any match fails closed), and the TCP connection opens to the validated IP
directly — no second resolution that DNS rebinding could steer. TLS keeps
SNI + hostname verification for the original host. Redirects are followed
manually (max 3), each target re-validated. Bodies stream with a byte cap
enforced DURING transfer, never after buffering. Full-host mode does not
relax any of this; network policy is separate from filesystem authority.
"""

from __future__ import annotations

import ipaddress
import socket
import ssl
from urllib.parse import urlparse

MAX_BYTES = 200_000
MAX_REDIRECTS = 3
CONNECT_TIMEOUT = 10.0


class FetchError(Exception):
    """Validation or transport failure (never returns partial unsafe data)."""


def _bounded_resolve(hostname: str, timeout: float = 5.0) -> list[str]:
    import concurrent.futures

    from ah.tools.builtins import _DNS_EXECUTOR

    fut = _DNS_EXECUTOR.submit(socket.getaddrinfo, hostname, None)
    try:
        infos = fut.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        raise FetchError("DNS resolution timed out") from None
    except socket.gaierror as e:
        raise FetchError(f"DNS resolution failed: {e}") from None
    ips: list[str] = []
    for _fam, _typ, _proto, _canon, sockaddr in infos:
        ips.append(sockaddr[0])
    if not ips:
        raise FetchError("DNS returned no addresses")
    return ips


def _check_ip_public(ip: str) -> None:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        raise FetchError(f"unparseable IP {ip!r}") from None
    if (
        addr.is_private
        or addr.is_loopback
        or addr.is_reserved
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_unspecified
    ):
        raise FetchError(f"resolved address {ip} is not public")


def _read_response(sock: socket.socket, max_bytes: int) -> tuple[int, dict[str, str], bytes]:
    f = sock.makefile("rb")
    try:
        status_line = f.readline(8192).decode("latin-1")
    except Exception as e:
        raise FetchError(f"short response: {e}") from None
    parts = status_line.strip().split(" ", 2)
    if len(parts) < 2 or not parts[1].isdigit():
        raise FetchError(f"bad status line: {status_line.strip()!r}")
    status = int(parts[1])
    headers: dict[str, str] = {}
    while True:
        line = f.readline(8192).decode("latin-1")
        if line in ("", "\r\n", "\n"):
            break
        if ":" in line:
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
    body = bytearray()

    def _push(chunk: bytes) -> None:
        body.extend(chunk)
        if len(body) > max_bytes:
            raise FetchError(f"response exceeds {max_bytes} byte cap")

    if headers.get("transfer-encoding", "").lower() == "chunked":
        while True:
            size_line = f.readline(8192).decode("latin-1").strip().split(";")[0]
            try:
                size = int(size_line, 16)
            except ValueError:
                raise FetchError("bad chunk size") from None
            if size == 0:
                f.readline(8192)
                break
            remaining = size
            while remaining > 0:
                data = f.read(min(remaining, 65536))
                if not data:
                    raise FetchError("truncated chunk")
                _push(data)
                remaining -= len(data)
            f.readline(8192)
    elif headers.get("content-length", "").strip().isdigit():
        remaining = int(headers["content-length"])
        if remaining > max_bytes:
            raise FetchError(f"response exceeds {max_bytes} byte cap")
        while remaining > 0:
            data = f.read(min(remaining, 65536))
            if not data:
                raise FetchError("truncated body")
            _push(data)
            remaining -= len(data)
    else:
        while True:
            data = f.read(65536)
            if not data:
                break
            _push(data)
    return status, headers, bytes(body)


def pinned_fetch(url: str, *, max_bytes: int = MAX_BYTES, timeout: float = CONNECT_TIMEOUT) -> str:
    """GET *url* over a pinned connection. Returns decoded text."""
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        parsed = urlparse(current)
        if parsed.scheme not in ("http", "https"):
            raise FetchError(f"unsupported scheme in {current!r}")
        hostname = parsed.hostname or ""
        if not hostname or hostname.lower() in ("localhost", "localhost.localdomain"):
            raise FetchError(f"rejected host {hostname!r}")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        ips = _bounded_resolve(hostname)
        for ip in ips:
            _check_ip_public(ip)
        target_ip = ips[0]
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        raw = socket.socket(
            socket.AF_INET6 if ":" in target_ip else socket.AF_INET, socket.SOCK_STREAM
        )
        raw.settimeout(timeout)
        try:
            raw.connect((target_ip, port))
            if parsed.scheme == "https":
                ctx = ssl.create_default_context()
                sock: socket.socket = ctx.wrap_socket(raw, server_hostname=hostname)
            else:
                sock = raw
            try:
                req = (
                    f"GET {path} HTTP/1.1\r\nHost: {hostname}\r\n"
                    f"Connection: close\r\nUser-Agent: AgentHarness/1.0\r\n"
                    f"Accept: text/markdown,text/plain\r\n\r\n"
                )
                sock.sendall(req.encode("ascii"))
                status, headers, body = _read_response(sock, max_bytes)
            finally:
                try:
                    sock.close()
                except OSError:
                    pass
        except FetchError:
            raise
        except (OSError, ssl.SSLError) as e:
            raise FetchError(f"connection to {hostname} failed: {e}") from None
        if status in (301, 302, 303, 307, 308):
            location = headers.get("location", "")
            if not location:
                raise FetchError("redirect without location")
            # Resolve relative redirects against the current URL, then loop
            # (next iteration re-validates scheme/host/IP).
            from urllib.parse import urljoin

            current = urljoin(current, location)
            continue
        if status != 200:
            raise FetchError(f"HTTP {status} for {current}")
        try:
            return body.decode("utf-8")
        except UnicodeDecodeError:
            raise FetchError("non-UTF-8 response") from None
    raise FetchError("too many redirects")
