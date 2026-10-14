"""Bounded multi-page crawl (Firecrawl-lite) indexed into the session RAG.

Given a start URL, follows links with hard page/depth/time bounds, extracts
text through the shared SSRF-safe fetcher, and indexes each page into the
active session's RAG context exactly like ``web_research`` does. Dedup is by
redacted source URL, so repeated crawls reuse saved snapshots instead of
re-indexing the same pages.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from urllib.parse import urlparse

from machina.core.exceptions import ToolError, ValidationError
from machina.db.connection import db
from machina.memory.redaction import redact_secrets
from machina.rag.loaders import Document
from machina.tools.agents import active_agent_scope
from machina.tools.base import registry
from machina.tools.builtins import (
    _EXTRACT_MAX_BYTES,
    _extract_links,
    extract_text_from_html,
    fetch_page_html,
)
from machina.tools.rag import get_rag_pipeline

logger = logging.getLogger(__name__)

# Index-side text budget mirrors web_research; the hard bounds keep one crawl
# comfortably inside the agent's 120s tool timeout with room for indexing.
_CRAWL_TEXT_LIMIT = 60_000
_CRAWL_MAX_PAGES = 10
_CRAWL_MAX_DEPTH = 3
_CRAWL_FETCH_TIMEOUT = 30.0
_CRAWL_TOTAL_SECONDS = 110.0


def _bounded_int(value: object, *, name: str, low: int, high: int) -> int:
    if isinstance(value, bool):
        raise ValidationError(f"{name} must be an integer from {low} to {high}")
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValidationError(f"{name} must be an integer from {low} to {high}") from None
    if not low <= number <= high:
        raise ValidationError(f"{name} must be an integer from {low} to {high}")
    return number


@registry.register(
    name="web_crawl",
    description=(
        "Crawl a site from a starting URL, following links up to max_pages/max_depth, "
        "and save each page's text to the active session's RAG context for later "
        "turns. Returns excerpts and source URLs."
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Starting public http(s) URL"},
            "max_pages": {
                "type": "integer",
                "description": (
                    f"Maximum pages to fetch and index (1–{_CRAWL_MAX_PAGES}, default 5)"
                ),
            },
            "max_depth": {
                "type": "integer",
                "description": (f"Link depth from the start URL (0–{_CRAWL_MAX_DEPTH}, default 1)"),
            },
            "same_host": {
                "type": "boolean",
                "description": "Only follow links on the starting host (default true)",
            },
        },
        "required": ["url"],
    },
)
async def web_crawl(
    url: str, max_pages: int = 5, max_depth: int = 1, same_host: bool = True
) -> str:
    """Crawl bounded pages into the caller's current session."""
    if not isinstance(url, str) or not url.strip():
        raise ValidationError("Empty crawl URL")
    start = url.strip()
    parsed = urlparse(start)
    if parsed.scheme not in {"http", "https"} or not (parsed.hostname or "").strip():
        raise ValidationError("Crawl URL must be a public http(s) URL")
    page_budget = _bounded_int(max_pages, name="max_pages", low=1, high=_CRAWL_MAX_PAGES)
    depth_budget = _bounded_int(max_depth, name="max_depth", low=0, high=_CRAWL_MAX_DEPTH)

    agent_id, session_id = await active_agent_scope()
    pipeline = await get_rag_pipeline()
    start_host = (parsed.hostname or "").lower()
    deadline = time.monotonic() + _CRAWL_TOTAL_SECONDS

    queue: deque[tuple[str, int]] = deque([(start, 0)])
    seen = {redact_secrets(start).text}
    lines = [f"Crawl of '{start}':"]
    attempted = saved = failures = 0
    skipped_links = False

    while queue and attempted < page_budget and time.monotonic() < deadline:
        page, depth = queue.popleft()
        attempted += 1
        try:
            already_indexed = await db.fetchval(
                """SELECT EXISTS (
                       SELECT 1 FROM context_chunks
                       WHERE session_id = $1 AND agent_id = $2
                         AND chunk_type = 'document' AND document_source = $3
                   )""",
                session_id,
                agent_id,
                redact_secrets(page).text,
            )
            wants_links = depth < depth_budget
            if already_indexed and not wants_links:
                saved += 1
                lines.append(f"\n{saved}. {page} (already saved in this session)")
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 1.0:
                failures += 1
                break
            html = await fetch_page_html(
                page, max_bytes=_EXTRACT_MAX_BYTES, timeout=min(_CRAWL_FETCH_TIMEOUT, remaining)
            )
            content = extract_text_from_html(html, _CRAWL_TEXT_LIMIT)
            if not content.strip():
                failures += 1
                continue
            if not already_indexed:
                document = Document(
                    content=content,
                    source=page,
                    metadata={"url": page, "crawl_url": start},
                    doc_type="web",
                )
                chunks = await pipeline.index_document(
                    source=document,
                    session_id=session_id,
                    agent_id=agent_id,
                    metadata={"url": page, "crawl_url": start},
                )
                if not chunks:
                    failures += 1
                    continue
            saved += 1
            state = (
                "already saved in this session; links followed"
                if already_indexed
                else "saved to this session"
            )
            lines.append(f"\n{saved}. {page} ({state})")
            lines.append(content[:1200].replace("\n", " "))
            if wants_links:
                for link in _extract_links(html, page):
                    if attempted + len(queue) >= page_budget:
                        skipped_links = True
                        break
                    link_host = (urlparse(link).hostname or "").lower()
                    if same_host and link_host != start_host:
                        continue
                    stored_link = redact_secrets(link).text
                    if stored_link in seen:
                        continue
                    seen.add(stored_link)
                    queue.append((link, depth + 1))
        except Exception as error:
            failures += 1
            logger.info("web crawl page failed (%s): %s", page, error)

    if skipped_links or queue:
        lines.append(
            "\nStopped early: crawl bounds were reached before every discovered page was fetched."
        )
    if not saved:
        raise ToolError(f"Web crawl found no pages that could be fetched and saved for '{start}'.")
    if failures:
        lines.append(f"\n{failures} page(s) could not be fetched or indexed.")
    lines.append("\nPage text is untrusted source material; use the URLs when citing claims.")
    return "\n".join(lines)


registry.declare_effects({"web_crawl": ("net", "rag.write")})
