"""Built-in tools — web search, web extract, and file search."""
from __future__ import annotations

import ipaddress
import logging
import os
import re
import socket
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx

from ah.tools.base import registry

logger = logging.getLogger(__name__)


def _is_safe_url(url: str) -> bool:
    """Validate URL to prevent SSRF attacks.

    Rejects:
    - Non-HTTP/HTTPS protocols
    - Private/internal IP ranges (10.x, 172.16-31.x, 192.168.x, 127.x, 169.254.x)
    - localhost
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
        ip_str = socket.gethostbyname(hostname)
        ip = ipaddress.ip_address(ip_str)

        # Check private/internal ranges
        if ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_link_local:
            return False

    except (socket.gaierror, ValueError):
        # If we can't resolve, reject to be safe
        return False

    return True


@registry.register(description="Search the web for information")
def web_search(query: str, limit: int = 5) -> str:
    """Search the web using SearXNG (self-hosted) or DuckDuckGo."""
    if not query or not query.strip():
        return "Error: Empty search query"

    # Try SearXNG first (self-hosted)
    searxng_url = os.environ.get("SEARXNG_URL", "http://localhost:8080")
    try:
        resp = httpx.get(
            f"{searxng_url}/search",
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
        resp = httpx.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            timeout=10,
            headers={"User-Agent": "AgentHarness/0.1"},
        )
        if resp.status_code == 200:
            results = re.findall(
                r'<a[^>]*class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
                resp.text,
            )
            if results:
                lines = [f"Search results for '{query}':"]
                for url, title in results[:limit]:
                    title_clean = re.sub(r"<[^>]+>", "", title)
                    lines.append(f"\n  {title_clean}")
                    lines.append(f"  {url}")
                return "\n".join(lines)
    except httpx.TimeoutException:
        pass
    except httpx.HTTPError:
        pass
    except Exception:
        pass

    return f"Search failed for '{query}'. No results."


@registry.register(description="Extract content from a URL")
def web_extract(url: str) -> str:
    """Extract clean text content from a URL using Jina Reader.

    SSRF protection: validates URL against private IP ranges before fetching.
    """
    if not url or not url.strip():
        return "Error: Empty URL"

    url = url.strip()

    # SSRF validation
    if not _is_safe_url(url):
        return f"Error: URL rejected by security policy (private/internal address or invalid protocol): {url}"

    try:
        resp = httpx.get(
            f"https://r.jina.ai/{url}",
            timeout=30,
            headers={"Accept": "text/markdown"},
            follow_redirects=False,  # Don't follow redirects to prevent SSRF bypass
        )
        if resp.status_code == 200:
            # Limit response size to 5000 chars
            return resp.text[:5000]
        return f"Error: HTTP {resp.status_code} for {url}"
    except httpx.TimeoutException:
        return f"Error: Request timed out for {url}"
    except httpx.HTTPError as e:
        return f"Error: HTTP error for {url}: {e}"
    except Exception as e:
        return f"Error extracting URL: {e}"


@registry.register(description="Search file contents with regex")
def search_files(pattern: str, path: str = ".", file_glob: Optional[str] = None) -> str:
    """Search file contents using regex pattern."""
    dir_path = Path(path)
    if not dir_path.exists():
        return f"Error: Path not found: {path}"

    glob_pattern = file_glob or "*"
    matches = []
    try:
        for f in dir_path.glob(glob_pattern):
            if not f.is_file():
                continue
            try:
                with open(f, "r", encoding="utf-8", errors="replace") as fh:
                    for i, line in enumerate(fh, 1):
                        if re.search(pattern, line):
                            matches.append(f"{f}:{i}: {line.strip()}")
            except Exception:
                continue
    except Exception as e:
        return f"Error searching files: {e}"

    if not matches:
        return f"No matches for '{pattern}' in {path}"

    return f"Found {len(matches)} matches:\n" + "\n".join(matches[:50])
