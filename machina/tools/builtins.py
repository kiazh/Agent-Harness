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
from urllib.parse import parse_qs, urlparse

import httpx

from machina.core.config import config
from machina.core.exceptions import ToolError, ValidationError
from machina.observability.diagnostics import record_failure
from machina.tools.base import registry

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
    results = []
    for href, title in parser.results:
        url = _canonical_result_url(href)
        if url and title:
            results.append((url, title))
    return results


def _canonical_result_url(href: str) -> str | None:
    """Unwrap DuckDuckGo result redirects into public HTTP(S) source URLs."""
    try:
        url = f"https:{href}" if href.startswith("//") else href
        parts = urlparse(url)
        if (parts.hostname or "").lower() in {
            "duckduckgo.com",
            "www.duckduckgo.com",
        } and parts.path.startswith("/l/"):
            url = parse_qs(parts.query).get("uddg", [""])[0]
            parts = urlparse(url)
        host = (parts.hostname or "").lower().rstrip(".")
        if parts.scheme not in {"http", "https"} or not host or parts.username or parts.password:
            return None
        _ = parts.port  # Reject malformed ports before showing a link to the agent.
        if host in {"localhost", "localhost.localdomain"}:
            return None
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            if not address.is_global:
                return None
        return parts._replace(fragment="").geturl()
    except ValueError:
        return None


async def _search_web_results(query: str, limit: int = 5) -> list[tuple[str, str, str]]:
    """Return validated (title, URL, snippet) search results."""
    results: list[tuple[str, str, str]] = []
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
                for item in data.get("results", [])[:limit]:
                    url = _canonical_result_url(str(item.get("url", "")))
                    title = str(item.get("title", "No title")).strip()
                    if url and title:
                        results.append((title, url, str(item.get("content", ""))[:300]))
                if results:
                    return results
        except httpx.TimeoutException:
            pass
        except httpx.HTTPError:
            pass
        except Exception as error:
            record_failure("builtins.web_search", error)

    try:
        resp = await asyncio.to_thread(
            httpx.get,
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            timeout=10,
            headers={"User-Agent": "Machina/0.1"},
        )
        if resp.status_code == 200:
            results = [(title, url, "") for url, title in _parse_duckduckgo_results(resp.text)]
            return results[:limit]
    except httpx.TimeoutException:
        pass
    except httpx.HTTPError:
        pass
    except Exception as error:
        record_failure("builtins.web_search", error)
    return []


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
_DNS_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="machina-dns"
)


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


@registry.register(description="Search the web for information")
async def web_search(query: str, limit: int = 5) -> str:
    """Search the web using SearXNG (self-hosted) or DuckDuckGo."""
    if not query or not query.strip():
        raise ValidationError("Empty search query")

    results = await _search_web_results(query, limit=max(1, min(int(limit), 10)))
    if not results:
        raise ToolError(f"Search failed for '{query}'. No results.")
    lines = [f"Search results for '{query}':"]
    for title, url, snippet in results:
        lines.extend((f"\n  {title}", f"  {url}"))
        if snippet:
            lines.append(f"  {snippet[:200]}")
    return "\n".join(lines)


class _ExtractedText(HTMLParser):
    """Discard markup and executable/style content from bounded HTML input."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in ("script", "style", "template", "noscript"):
            self.hidden.append(tag)
        elif not self.hidden and tag in ("p", "div", "br", "li", "h1", "h2", "h3", "article"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self.hidden and tag == self.hidden[-1]:
            self.hidden.pop()
        elif not self.hidden:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


@registry.register(description="Extract content from a URL")
async def web_extract(url: str) -> str:
    """Extract text through a direct, validated-IP connection (no proxy).

    DNS, TLS, redirects, headers, body size, and overall deadline are enforced
    by the shared pinned fetch. TLS verifies the original host; every redirect
    undergoes the same public-address policy.
    """
    if not url or not url.strip():
        raise ValidationError("Empty URL")
    return await extract_url_text(url)


async def extract_url_text(url: str) -> str:
    """Fetch and extract bounded page text through the SSRF-safe fetcher."""
    from machina.security.fetch import FetchError, pinned_fetch

    try:
        content = await asyncio.to_thread(pinned_fetch, url.strip(), max_bytes=20_000, timeout=30)
    except FetchError as error:
        raise ToolError(f"Error extracting URL: {error}") from None
    if re.search(
        r"<(?:!doctype|html|head|body|div|p|h[1-6]|article|script|style)\b", content, re.I
    ):
        parser = _ExtractedText()
        parser.feed(content)
        content = "\n".join(
            line.strip() for line in "".join(parser.parts).splitlines() if line.strip()
        )
    return content[:5000]


@registry.register(description="Search file contents with regex")
async def search_files(pattern: str, path: str = ".", file_glob: str | None = None) -> str:
    """Search file contents using regex pattern."""
    from machina.tools.file import resolve_path

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
