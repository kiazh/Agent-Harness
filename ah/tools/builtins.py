"""Built-in tools — web search, web extract, and file search."""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
import socket
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx

from ah.core.config import config
from ah.core.exceptions import ToolError, ValidationError
from ah.tools.base import registry

logger = logging.getLogger(__name__)


def _is_safe_url(url: str) -> bool:
    """Validate URL to prevent SSRF attacks.

    Rejects:
    - Non-HTTP/HTTPS protocols
    - Private/internal IP ranges (10.x, 172.16-31.x, 192.168.x, 127.x, 169.254.x)
    - localhost
    - IPv6 private ranges
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
        # Get all addresses (IPv4 and IPv6)
        addr_infos = socket.getaddrinfo(hostname, None)
        for family, _, _, _, sockaddr in addr_infos:
            ip = ipaddress.ip_address(sockaddr[0])
            if ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_link_local:
                return False
    except (socket.gaierror, ValueError):
        # If we can't resolve, reject to be safe
        return False

    return True


def _get_pinned_ip(hostname: str) -> str | None:
    """Resolve hostname to IP and return it for pinning in HTTP requests."""
    try:
        addr_infos = socket.getaddrinfo(hostname, None)
        if addr_infos:
            return addr_infos[0][4][0]
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
    try:
        resp = await asyncio.to_thread(
            httpx.get,
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
        resp = await asyncio.to_thread(
            httpx.get,
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
        raise ValidationError(f"URL rejected by security policy (private/internal address or invalid protocol): {url}")

    try:
        # Pin the resolved IP to prevent DNS rebinding
        parsed = urlparse(url)
        hostname = parsed.hostname or ""
        pinned_ip = _get_pinned_ip(hostname)
        if pinned_ip is None:
            raise ToolError(f"Could not resolve hostname: {hostname}")

        # Replace hostname with pinned IP in the URL
        pinned_url = url.replace(hostname, pinned_ip, 1)
        resp = await asyncio.to_thread(
            httpx.get,
            f"https://r.jina.ai/{pinned_url}",
            timeout=30,
            headers={"Accept": "text/markdown", "Host": hostname},
            follow_redirects=False,  # Don't follow redirects to prevent SSRF bypass
        )
        if resp.status_code == 200:
            # Limit response size to 5000 chars
            return resp.text[:5000]
        raise ToolError(f"HTTP {resp.status_code} for {url}")
    except httpx.TimeoutException:
        raise ToolError(f"Request timed out for {url}")
    except httpx.HTTPError as e:
        raise ToolError(f"HTTP error for {url}: {e}")
    except Exception as e:
        raise ToolError(f"Error extracting URL: {e}")


def _resolve_path(path: str, base_dir: str | None = None) -> Path:
    """Resolve a path and ensure it stays within the base directory."""
    # Use the same base directory as file.py for consistency
    from ah.tools.file import _BASE_DIR
    base = Path(base_dir or _BASE_DIR).resolve()
    # Handle absolute paths directly
    p = Path(path)
    if p.is_absolute():
        resolved = p.resolve()
    else:
        resolved = (base / path).resolve()
    try:
        resolved.relative_to(base)
    except ValueError:
        raise ValueError(f"Path '{path}' escapes base directory '{base}'")
    return resolved


@registry.register(description="Search file contents with regex")
async def search_files(pattern: str, path: str = ".", file_glob: Optional[str] = None) -> str:
    """Search file contents using regex pattern."""
    try:
        dir_path = _resolve_path(path)
    except ValueError as e:
        raise ToolError(f"{e}")
    if not dir_path.exists():
        raise ToolError(f"Path not found: {path}")

    glob_pattern = file_glob or "*"
    matches = []
    try:
        def _search():
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
        await asyncio.to_thread(_search)
    except Exception as e:
        raise ToolError(f"Error searching files: {e}")

    if not matches:
        return f"No matches for '{pattern}' in {path}"

    return f"Found {len(matches)} matches:\n" + "\n".join(matches[:50])
