"""Built-in tools — web search, web extract, and file search."""

from __future__ import annotations

import asyncio
import concurrent.futures
import ipaddress
import logging
import re
import socket
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

import httpx

from ah.core.config import config
from ah.core.exceptions import ToolError, ValidationError
from ah.tools.base import registry

logger = logging.getLogger(__name__)


class _DuckDuckGoLinks(HTMLParser):
    """Collect result anchors regardless of attribute order or extra CSS classes."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[tuple[str, str]] = []
        self._url: str | None = None
        self._title: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a" or self._url is not None:
            return
        values = dict(attrs)
        if "result__a" in (values.get("class") or "").split() and values.get("href"):
            self._url = values["href"]
            self._title = []

    def handle_data(self, data: str) -> None:
        if self._url is not None:
            self._title.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._url is not None:
            self.results.append((self._url, "".join(self._title).strip()))
            self._url = None


def _parse_duckduckgo_results(html: str) -> list[tuple[str, str]]:
    parser = _DuckDuckGoLinks()
    parser.feed(html)
    return parser.results


def _getaddrinfo_timeout(hostname: str, timeout: float = 3.0):
    """Resolve hostname with a bounded timeout without blocking on shutdown.

    AH-025: the old per-call ``ThreadPoolExecutor`` context waited for the
    resolver thread on exit, so a timeout still blocked for the full DNS
    duration. Use a shared executor and return on timeout without waiting
    for the abandoned worker. The pool is bounded (4 workers) so slow DNS
    cannot spawn unbounded threads; abandoned lookups finish in the
    background and their sockets are closed by the OS.
    """
    future = _DNS_EXECUTOR.submit(socket.getaddrinfo, hostname, None)
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        raise socket.gaierror("DNS resolution timed out") from None


# Shared bounded executor for DNS (never shut down per call — see above).
_DNS_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="ah-dns")


def _is_safe_url(url: str) -> bool:
    """Validate URL to prevent SSRF attacks.

    Rejects:
    - Non-HTTP/HTTPS protocols
    - Private/internal IP ranges (10.x, 172.16-31.x, 192.168.x, 127.x, 169.254.x)
    - localhost
    - IPv6 private ranges
    - Multicast, unspecified (0.0.0.0/::), reserved and link-local addresses
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return False

    # Only allow HTTP/HTTPS
    if parsed.scheme not in ("http", "https"):
        return False

    hostname = parsed.hostname
    if not hostname:
        return False

    # Reject localhost
    if hostname.lower() in ("localhost", "localhost.localdomain"):
        return False

    # Resolve hostname to IP and check against private ranges
    try:
        # Get all addresses (IPv4 and IPv6) with bounded timeout.
        addr_infos = _getaddrinfo_timeout(hostname, timeout=3.0)
        for _, _, _, _, sockaddr in addr_infos:
            ip = ipaddress.ip_address(sockaddr[0])
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_reserved
                or ip.is_link_local
                or ip.is_multicast
                or ip.is_unspecified
            ):
                return False
    except (socket.gaierror, ValueError):
        # If we can't resolve, reject to be safe
        return False

    return True


def _resolve_safe_ip(hostname: str) -> str | None:
    """Resolve hostname to a safe (non-private) IP address.

    Returns the first safe IP address, or None if no safe address is found.
    This is used to pin the resolved IP for the actual connection, preventing
    TOCTOU races where DNS resolution changes between validation and connection.
    TODO(pinned-ip): actually connect to the pinned IP with Host header / TLS
    SNI instead of only validating, so DNS rebinding between check and fetch
    cannot bypass the SSRF policy.
    """
    try:
        addr_infos = _getaddrinfo_timeout(hostname, timeout=3.0)
        for _, _, _, _, sockaddr in addr_infos:
            ip = ipaddress.ip_address(sockaddr[0])
            if not (
                ip.is_private
                or ip.is_loopback
                or ip.is_reserved
                or ip.is_link_local
                or ip.is_multicast
                or ip.is_unspecified
            ):
                return str(ip)
    except (socket.gaierror, ValueError):
        pass
    return None


@registry.register(description="Search the web for information")
async def web_search(query: str, limit: int = 5) -> str:
    """Search the web using SearXNG (self-hosted) or DuckDuckGo."""
    if not query or not query.strip():
        raise ValidationError("Empty search query")

    # Try SearXNG first (self-hosted)
    searxng_url = config.get("searxng_url")
    if searxng_url:
        try:
            probe = f"{str(searxng_url).rstrip('/')}/search"
            if not _is_safe_url(probe):
                raise ValidationError(f"SearXNG URL rejected by security policy: {searxng_url}")
        except ValidationError:
            searxng_url = None
    if searxng_url:
        try:
            resp = await asyncio.to_thread(
                httpx.get,
                f"{str(searxng_url).rstrip('/')}/search",
                params={"q": query, "format": "json"},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                results = data.get("results", [])[:limit]
                if results:
                    lines = [f"Search results for '{query}':"]
                    for r in results:
                        lines.append(f"\n  {r.get('title', 'No title')}")
                        lines.append(f"  {r.get('url', '')}")
                        lines.append(f"  {r.get('content', '')[:200]}")
                    return "\n".join(lines)
        except httpx.TimeoutException:
            pass
        except httpx.HTTPError:
            pass
        except Exception:
            pass

    # Fallback: DuckDuckGo HTML
    try:
        resp = await asyncio.to_thread(
            httpx.get,
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            timeout=10,
            headers={"User-Agent": "AgentHarness/0.1"},
        )
        if resp.status_code == 200:
            results = _parse_duckduckgo_results(resp.text)
            if results:
                lines = [f"Search results for '{query}':"]
                for url, title in results[:limit]:
                    lines.append(f"\n  {title}")
                    lines.append(f"  {url}")
                return "\n".join(lines)
    except httpx.TimeoutException:
        pass
    except httpx.HTTPError:
        pass
    except Exception:
        pass

    raise ToolError(f"Search failed for '{query}'. No results.")


@registry.register(description="Extract content from a URL")
async def web_extract(url: str) -> str:
    """Extract clean text content from a URL using Jina Reader.

    SSRF protection: validates URL against private IP ranges before fetching.
    """
    if not url or not url.strip():
        raise ValidationError("Empty URL")

    url = url.strip()

    # SSRF validation
    if not _is_safe_url(url):
        raise ValidationError(
            f"URL rejected by security policy (private/internal address or invalid protocol): {url}"
        )

    # Pin the resolved IP to prevent TOCTOU race
    parsed = urlparse(url)
    hostname = parsed.hostname
    pinned_ip = _resolve_safe_ip(hostname) if hostname else None
    if pinned_ip is None:
        raise ValidationError(f"Could not resolve safe IP for: {hostname}")

    try:
        # The fetch is performed by the Jina Reader proxy, so the URL is passed
        # through unchanged. Rewriting it to a pinned IP (and overriding Host)
        # sent the *target's* hostname to r.jina.ai and broke TLS/SNI there.
        # Stream with byte limit to avoid loading full response into memory
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            async with client.stream(
                "GET",
                f"https://r.jina.ai/{url}",
                headers={"Accept": "text/markdown"},
            ) as resp:
                if resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("location", "")
                    if not location or not _is_safe_url(location):
                        raise ValidationError(
                            f"Redirect target rejected by security policy: {location!r}"
                        )
                    raise ToolError(f"HTTP {resp.status_code} redirect for {url}")
                if resp.status_code != 200:
                    raise ToolError(f"HTTP {resp.status_code} for {url}")
                # Read at most 20000 bytes (enough for ~5000 chars of UTF-8)
                chunks = []
                total_bytes = 0
                max_bytes = 20000
                async for chunk in resp.aiter_bytes():
                    total_bytes += len(chunk)
                    if total_bytes > max_bytes:
                        break
                    chunks.append(chunk)
                content = b"".join(chunks)[:max_bytes].decode("utf-8", errors="replace")
                return content[:5000]
    except httpx.TimeoutException:
        raise ToolError(f"Request timed out for {url}") from None
    except httpx.HTTPError as e:
        raise ToolError(f"HTTP error for {url}: {e}") from e
    except Exception as e:
        raise ToolError(f"Error extracting URL: {e}") from e


@registry.register(description="Search file contents with regex")
async def search_files(pattern: str, path: str = ".", file_glob: str | None = None) -> str:
    """Search file contents using regex pattern."""
    from ah.tools.file import resolve_path

    if not isinstance(pattern, str) or not pattern:
        raise ToolError("Invalid search pattern")
    if len(pattern) > 200:
        raise ToolError("Search pattern exceeds 200 characters (ReDoS guard)")
    try:
        dir_path = resolve_path(path)
    except ValueError as e:
        raise ToolError(f"{e}") from e
    if not dir_path.exists():
        raise ToolError(f"Path not found: {path}")

    try:
        regex = re.compile(pattern)
    except re.error as e:
        raise ToolError(f"Invalid regex pattern '{pattern}': {e}") from e

    glob_pattern = file_glob or "*"
    # Sanitize glob pattern to prevent path traversal
    if (
        ".." in glob_pattern
        or ":" in glob_pattern
        or "\\" in glob_pattern
        or Path(glob_pattern).is_absolute()
        or glob_pattern.startswith(("/", "\\"))
    ):
        raise ToolError(f"Invalid glob pattern: {glob_pattern}")
    matches = []
    try:

        def _search():
            count = 0
            for f in dir_path.glob(glob_pattern):
                count += 1
                if count > 1000:
                    break
                try:
                    resolve_path(str(f))
                except ValueError:
                    continue
                if not f.is_file():
                    continue
                try:
                    if f.stat().st_size > 102_400:
                        continue
                    with open(f, encoding="utf-8", errors="replace") as fh:
                        for i, line in enumerate(fh, 1):
                            if len(line) > 4096:
                                continue
                            if regex.search(line):
                                matches.append(f"{f}:{i}: {line.strip()}")
                                if len(matches) >= 500:
                                    return
                except OSError:
                    continue

        await asyncio.to_thread(_search)
    except Exception as e:
        raise ToolError(f"Error searching files: {e}") from e

    if not matches:
        return f"No matches for '{pattern}' in {path}"

    return f"Found {len(matches)} matches:\n" + "\n".join(matches[:50])


registry.declare_effects(
    {
        "web_search": ("net",),
        "web_extract": ("net",),
        "search_files": ("fs.read",),
    }
)
