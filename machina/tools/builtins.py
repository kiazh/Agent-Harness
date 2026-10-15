"""Built-in tools — web search, web extract, and file search."""

from __future__ import annotations

import asyncio
import concurrent.futures
import ipaddress
import json
import logging
import os
import re
import socket
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import httpx

from machina.core.config import config
from machina.core.exceptions import ToolError, ValidationError
from machina.observability.diagnostics import record_failure
from machina.permissions.policy import sensitive_path
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
            # Operator-configured instance, not agent input: an explicitly
            # configured loopback address (e.g. a self-hosted container on
            # localhost) is allowed. Private ranges, link-local/metadata IPs
            # and non-HTTP schemes stay rejected by the policy.
            if not _is_safe_url(probe, allow_loopback=True):
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


def _is_safe_url(url: str, *, allow_loopback: bool = False) -> bool:
    """Validate URL to prevent SSRF attacks.

    Rejects:
    - Non-HTTP/HTTPS protocols
    - Private/internal IP ranges (10.x, 172.16-31.x, 192.168.x, 127.x, 169.254.x)
    - localhost
    - IPv6 private ranges
    - Multicast, unspecified (0.0.0.0/::), reserved and link-local addresses

    ``allow_loopback=True`` is reserved for explicitly operator-configured
    service URLs (the SearXNG instance): loopback hosts (localhost, 127.0.0.0/8,
    ::1) are then permitted. All other rejections above still apply.
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

    # Reject localhost unless the caller explicitly allows loopback
    if hostname.lower() in ("localhost", "localhost.localdomain"):
        return allow_loopback

    # Resolve hostname to IP and check against private ranges
    try:
        # Get all addresses (IPv4 and IPv6) with bounded timeout.
        addr_infos = _getaddrinfo_timeout(hostname, timeout=3.0)
        for _, _, _, _, sockaddr in addr_infos:
            ip = ipaddress.ip_address(sockaddr[0])
            if ip.is_loopback and allow_loopback:
                continue
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


@registry.register(
    name="web_search",
    description="Search the web for information",
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query, e.g. 'python asyncio timeout pattern'",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 10,
                "default": 5,
                "description": "Maximum number of results to return (1–10, default 5)",
            },
        },
        "required": ["query"],
    },
)
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


@registry.register(
    name="web_extract",
    description="Extract content from a URL",
    parameters={
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "Public http(s) URL to fetch and convert to plain text",
            },
        },
        "required": ["url"],
    },
)
async def web_extract(url: str) -> str:
    """Extract text through a direct, validated-IP connection (no proxy).

    DNS, TLS, redirects, headers, body size, and overall deadline are enforced
    by the shared pinned fetch. TLS verifies the original host; every redirect
    undergoes the same public-address policy.
    """
    if not url or not url.strip():
        raise ValidationError("Empty URL")
    return await extract_url_text(url)


# Extraction bounds: the raw-body cap is generous so real pages (often a few
# hundred KB of markup) can still be processed; the extracted-text cap stays
# modest because tool output feeds model context. Callers that index pages
# (web_research, web_crawl) pass a larger text_limit for retrieval quality.
_EXTRACT_MAX_BYTES = 1_000_000
_EXTRACT_TEXT_LIMIT = 20_000
_EXTRACT_TIMEOUT = 30.0


async def fetch_page_html(
    url: str, *, max_bytes: int = _EXTRACT_MAX_BYTES, timeout: float = _EXTRACT_TIMEOUT
) -> str:
    """Fetch a page body through the SSRF-safe pinned fetcher (the only network path)."""
    from machina.security.fetch import FetchError, pinned_fetch

    try:
        return await asyncio.to_thread(
            pinned_fetch, url.strip(), max_bytes=max_bytes, timeout=timeout
        )
    except FetchError as error:
        raise ToolError(f"Error fetching URL: {error}") from None


def extract_text_from_html(content: str, text_limit: int = _EXTRACT_TEXT_LIMIT) -> str:
    """Strip markup and trim to the text budget."""
    if re.search(
        r"<(?:!doctype|html|head|body|div|p|h[1-6]|article|script|style)\b", content, re.I
    ):
        parser = _ExtractedText()
        parser.feed(content)
        content = "\n".join(
            line.strip() for line in "".join(parser.parts).splitlines() if line.strip()
        )
    return content[:text_limit]


async def extract_url_text(url: str, *, text_limit: int = _EXTRACT_TEXT_LIMIT) -> str:
    """Fetch and extract bounded page text through the SSRF-safe fetcher."""
    content = await fetch_page_html(url)
    return extract_text_from_html(content, text_limit)


class _PageLinks(HTMLParser):
    """Collect anchor hrefs for crawling; malformed markup never escapes."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        for key, value in attrs:
            if key == "href" and value:
                self.hrefs.append(value)


def _extract_links(html: str, base_url: str) -> list[str]:
    """Public http(s) links from a page, resolved and policy-checked, in order."""
    parser = _PageLinks()
    try:
        parser.feed(html)
    except Exception:
        return []
    links: list[str] = []
    seen: set[str] = set()
    for href in parser.hrefs:
        canonical = _canonical_result_url(urljoin(base_url, href))
        if canonical and canonical not in seen:
            seen.add(canonical)
            links.append(canonical)
    return links


# search_files sharing bounds. Matching runs in an isolated interpreter with a
# hard deadline: a thread + timeout cannot interrupt catastrophic backtracking
# in `re` (the abandoned thread keeps burning CPU), but a subprocess can be
# killed. Legitimate scans stay under the deadline; a pathological pattern
# (e.g. "(a+)+$") is terminated and reported as a bounded ToolError.
_SEARCH_MAX_FILES = 1000
_SEARCH_MAX_FILE_BYTES = 102_400
_SEARCH_MAX_LINE_CHARS = 4096
_SEARCH_MAX_MATCHES = 500
_SEARCH_DEADLINE_SECONDS = 3.0

# stdlib-only worker; the pattern and validated file identities arrive as
# JSON on stdin and match lines are returned as JSON on stdout, preserving the
# report lines the tool has always produced (`path:line: text`).
_SEARCH_WORKER_SOURCE = """
import json
import os
import re
import stat
import sys


def main():
    payload = json.load(sys.stdin)
    regex = re.compile(payload["pattern"])
    max_line_chars = payload["max_line_chars"]
    max_matches = payload["max_matches"]
    matches = []
    for entry in payload["files"]:
        path = entry["path"]
        fd = -1
        try:
            # H18: this process re-opens by pathname, so the opened descriptor
            # must still be the file the parent validated (dev/ino identity);
            # a pathname swapped to another file after validation — e.g. a
            # symlink retargeted outside the workspace — is skipped, never
            # followed. O_NONBLOCK + S_ISREG keep a swapped FIFO from
            # blocking the matcher.
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                continue
            if (info.st_dev, info.st_ino) != (entry["dev"], entry["ino"]):
                continue
            with os.fdopen(fd, encoding="utf-8", errors="replace") as handle:
                fd = -1  # fdopen now owns the descriptor
                for number, line in enumerate(handle, 1):
                    if len(line) > max_line_chars:
                        continue
                    if regex.search(line):
                        matches.append(f"{path}:{number}: {line.strip()}")
                        if len(matches) >= max_matches:
                            print(json.dumps(matches))
                            return
        except OSError:
            continue
        finally:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
    print(json.dumps(matches))


main()
"""


async def _match_files_bounded(pattern: str, files: list[dict[str, object]]) -> list[str]:
    """Match *pattern* against *files*, enforcing the deadline by process kill.

    Returns report lines in ``path:line: text`` form. Raises ToolError when the
    matcher exceeds ``_SEARCH_DEADLINE_SECONDS`` (terminated, not abandoned) or
    fails; the child never outlives the call. Each entry carries the validated
    file identity (path + dev/ino) the matcher verifies before reading (H18).
    """
    if not files:
        return []
    if not sys.executable:
        raise ToolError("Error searching files: no isolated interpreter available")
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            _SEARCH_WORKER_SOURCE,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as e:
        raise ToolError(f"Error searching files: cannot start isolated matcher: {e}") from e
    payload = json.dumps(
        {
            "pattern": pattern,
            "files": files,
            "max_line_chars": _SEARCH_MAX_LINE_CHARS,
            "max_matches": _SEARCH_MAX_MATCHES,
        }
    ).encode()
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(payload), timeout=_SEARCH_DEADLINE_SECONDS
        )
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise ToolError(
            f"Search timed out after {_SEARCH_DEADLINE_SECONDS:g}s for pattern "
            f"'{pattern}' (possible catastrophic backtracking); matcher terminated"
        ) from None
    except BaseException:
        # Caller cancellation (e.g. an agent-level tool timeout) must not leak
        # the isolated matcher either.
        proc.kill()
        raise
    if proc.returncode != 0:
        detail = stderr.decode(errors="replace").strip().splitlines()[-1:] or [
            f"exit code {proc.returncode}"
        ]
        raise ToolError(f"Error searching files: isolated matcher failed: {detail[0]}")
    try:
        matches = json.loads(stdout.decode() or "[]")
    except json.JSONDecodeError as e:
        raise ToolError(f"Error searching files: malformed matcher output: {e}") from e
    return [str(match) for match in matches]


@registry.register(
    name="search_files",
    description="Search file contents with regex",
    parameters={
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Regular expression to search for (max 200 characters)",
            },
            "path": {
                "type": "string",
                "default": ".",
                "description": "Directory to search, relative to the workspace (default '.')",
            },
            "file_glob": {
                "type": "string",
                "default": "*",
                "description": "Glob filter for file names, e.g. '*.py' (default '*' = all files)",
            },
        },
        "required": ["pattern"],
    },
)
async def search_files(pattern: str, path: str = ".", file_glob: str | None = None) -> str:
    """Search file contents using regex pattern."""
    from machina.tools.file import _open_secure_read, resolve_path

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

    # Validate the pattern here (cheap); matching happens in the isolated
    # worker, so a hostile pattern can never backtrack in this process.
    try:
        re.compile(pattern)
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

    def _collect_files() -> list[dict[str, object]]:
        files: list[dict[str, object]] = []
        for f in dir_path.glob(glob_pattern):
            if len(files) >= _SEARCH_MAX_FILES:
                break
            try:
                resolved = resolve_path(str(f))
            except ValueError:
                continue
            # R01: every file a search opens is checked against the shared
            # sensitive-path definition — the tool's narrower private list
            # alone let `credentials`/`production.env` leak into results.
            if sensitive_path(str(resolved)):
                continue
            # H18: capture the validated file's identity through the same
            # protected descriptor traversal the ordinary file tools use
            # (no-follow open + dev/ino + realpath containment). The isolated
            # matcher re-opens the pathname later and verifies the opened
            # descriptor still matches this identity, so a concurrent
            # symlink swap cannot redirect the read outside the workspace.
            try:
                fd = _open_secure_read(resolved)
            except (OSError, ValueError):
                continue
            try:
                info = os.fstat(fd)
            finally:
                os.close(fd)
            if info.st_size > _SEARCH_MAX_FILE_BYTES:
                continue
            files.append({"path": str(f), "dev": info.st_dev, "ino": info.st_ino})
        return files

    try:
        files = await asyncio.to_thread(_collect_files)
        matches = await _match_files_bounded(pattern, files)
    except ToolError:
        raise
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
