"""Agent-callable reads of retained tool artifacts (exact originals).

When a tool result is too large to carry whole, the full original is retained
as a session-scoped artifact and the model receives a handle. These tools page,
search, and describe those originals. Every call re-authorizes against the
active session's ancestry; expired or out-of-scope handles answer with an
explicit unavailable message, never a weaker fallback.
"""

from __future__ import annotations

from typing import Any

from machina.core.artifacts import ArtifactError, artifact_store
from machina.core.exceptions import ToolError
from machina.tools.agents import active_agent_scope
from machina.tools.base import registry

# Store-side bound for one read page (mirrors machina.core.artifacts).
_MAX_READ_LIMIT = 20_000
# Bounded resize attempts: shrink the body window by the measured overflow and
# re-read, so header + body + cursor fit the model-visible budget (C5).
_PAGE_ATTEMPTS = 4


def _fail(error: ArtifactError) -> ToolError:
    return ToolError(str(error))


def _page_budget(tool_name: str) -> int:
    """Model-visible cap this tool must budget its whole envelope to (C5)."""
    from machina.core.tool_views import append_limit

    return append_limit(tool_name)


def _read_envelope(page: dict[str, Any]) -> str:
    """Format one read page: header + emitted body + continuation cursor."""
    start = page["offset"]
    body = page["content"] or ""
    # The range must describe the bytes actually emitted, not the requested
    # window (C5): with a resized body the store's numbers can be larger.
    end = start + len(body)
    revision = page.get("content_revision") or "unknown"
    header = (
        f"[artifact {page['artifact_id']} kind={page['kind']} "
        f"chars {start}-{end} of {page['total_chars']} revision={revision}"
        f"{'; more remains — raise offset' if page['truncated'] else ''}]"
    )
    text = f"{header}\n{body}"
    if page.get("next_cursor"):
        text += f"\nnext_cursor={page['next_cursor']}"
    return text


def _search_disclosure(page: dict[str, Any]) -> str:
    disclosure = (
        f"scanned={page['scanned']} scan_limit={page['scan_limit']} "
        f"scan_truncated={str(bool(page['scan_truncated'])).lower()}"
    )
    if page.get("next_cursor"):
        disclosure += f"\nnext_cursor={page['next_cursor']}"
    return disclosure


def _search_match_lines(matches: list[dict[str, Any]]) -> list[str]:
    return [
        f"artifact={m['artifact_id']} kind={m['kind']} chars={m['total_chars']} "
        f"pos={m['position']}: {m['excerpt']!r}"
        for m in matches
    ]


def _search_envelope(page: dict[str, Any], *, scoped: bool) -> str:
    """Format one search page; match lines are atomic (never partial)."""
    disclosure = _search_disclosure(page)
    matches = page["matches"]
    if not matches:
        if page["scan_truncated"]:
            return (
                "No artifact matches in the scanned window; the window was cut short, "
                "so an unscanned artifact may still match — resume with the cursor.\n"
                f"{disclosure}"
            )
        if scoped:
            # Single-artifact scope: scanned=1 and not truncated, so the
            # bounded question has a definitive answer.
            return "No artifact matches."
        return f"No artifact matches; every readable artifact in scope was scanned.\n{disclosure}"
    lines = _search_match_lines(matches)
    lines.append(disclosure)
    return "\n".join(lines)


def _page_refusal(tool_name: str, cap: int) -> str:
    """Bounded structured refusal: a page envelope is never blind-sliced (C5)."""
    marker = (
        f"{tool_name} page exceeds the {cap}-char model-visible budget; request a smaller limit."
    )
    return marker[:cap]


@registry.register(
    name="artifact_read",
    description=(
        "Read a retained tool-result artifact by its handle (character paging). "
        "Use for exact originals of oversized results referenced by an earlier "
        "artifact id. Resume a truncated page with its next_cursor; a cursor "
        "whose bound revision no longer matches the artifact is rejected "
        "explicitly, never silently paged against a different evidence basis."
    ),
    parameters={
        "type": "object",
        "properties": {
            "artifact_id": {"type": "string", "description": "Artifact UUID from a tool result"},
            "offset": {"type": "integer", "description": "Start character (0-based)"},
            "limit": {"type": "integer", "description": "Max characters to return (1-20000)"},
            "cursor": {
                "type": "string",
                "description": "Continuation cursor from a previous artifact_read result",
            },
        },
        "required": ["artifact_id"],
    },
)
async def artifact_read(
    artifact_id: str, offset: int = 0, limit: int = 4000, cursor: str = ""
) -> str:
    """Return a bounded page of the exact original text.

    The header cites the stored ``content_revision``; a truncated page carries
    a bound ``next_cursor`` (revision/source-version/retention) so a resume
    that no longer matches is answered with a stale-cursor error, never with
    data from a different evidence basis (C09/C14).

    The whole envelope — header, body AND cursor line — is budgeted to the
    model-visible append cap before the body window is chosen (C5). An
    oversized first attempt is re-read with a smaller window; a page that
    cannot fit even minimally is answered with a structured refusal telling
    the caller to request a smaller limit, never with a blind slice.
    """
    agent_id, session_id = await active_agent_scope()
    cap = _page_budget("artifact_read")
    attempt_limit = max(1, min(int(limit), _MAX_READ_LIMIT))
    for _ in range(_PAGE_ATTEMPTS):
        try:
            page = await artifact_store.read(
                artifact_id,
                session_id=session_id,
                agent_id=agent_id,
                offset=offset,
                limit=attempt_limit,
                cursor=cursor or None,
            )
        except ArtifactError as error:
            raise _fail(error) from None
        text = _read_envelope(page)
        overflow = len(text) - cap
        if overflow <= 0:
            return text
        # Header + cursor are part of the budget: shrink the body window by
        # the exact overflow measured on the formatted envelope.
        reduced = attempt_limit - overflow
        if reduced < 1:
            break
        attempt_limit = reduced
    return _page_refusal("artifact_read", cap)


@registry.register(
    name="artifact_search",
    description=(
        "Search retained artifacts (one handle, or the session's recent "
        "artifacts) for a case-insensitive substring. Returns match positions "
        "and excerpts for precise paging with artifact_read, plus the scan "
        "window (scanned/scan_limit/scan_truncated) and a continuation cursor "
        "so an empty page never implies the query is absent."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Case-insensitive substring to find"},
            "artifact_id": {
                "type": "string",
                "description": "Artifact UUID to search (empty = all session artifacts)",
            },
            "limit": {"type": "integer", "description": "Max matches (1-20)"},
            "cursor": {
                "type": "string",
                "description": "Continuation cursor from a previous artifact_search result",
            },
        },
        "required": ["query"],
    },
)
async def artifact_search(
    query: str, artifact_id: str = "", limit: int = 5, cursor: str = ""
) -> str:
    """Return match positions and bounded excerpts with scan disclosure.

    The store scans a bounded window (default 50 newest readable artifacts);
    ``scanned``/``scan_limit``/``scan_truncated`` state exactly what was
    covered, and a non-empty ``next_cursor`` lets the caller continue instead
    of reading an empty page as a definitive absence.

    Match lines are atomic and the envelope (lines + disclosure + cursor) is
    budgeted to the model-visible append cap (C5). An oversized page is
    re-queried with fewer matches; the store's cursor is anchored on the last
    SCANNED artifact, so shrinking a page never skips matches.
    """
    agent_id, session_id = await active_agent_scope()
    cap = _page_budget("artifact_search")
    scoped = bool(artifact_id)
    page_limit = max(1, min(int(limit), 20))
    for _ in range(_PAGE_ATTEMPTS):
        try:
            page = await artifact_store.search_page(
                query,
                session_id=session_id,
                agent_id=agent_id,
                artifact_id=artifact_id or None,
                limit=page_limit,
                cursor=cursor or None,
            )
        except ArtifactError as error:
            raise _fail(error) from None
        text = _search_envelope(page, scoped=scoped)
        overflow = len(text) - cap
        if overflow <= 0:
            return text
        longest = max((len(line) for line in _search_match_lines(page["matches"])), default=0)
        if longest and page_limit > 1:
            page_limit = max(1, page_limit - max(1, -(-overflow // longest)))
            continue
        break
    return _page_refusal("artifact_search", cap)


@registry.register(
    name="artifact_describe",
    description=(
        "Show metadata for a retained artifact (kind, size, status, "
        "completeness, source/revision binding, retention) without reading "
        "its content."
    ),
    parameters={
        "type": "object",
        "properties": {"artifact_id": {"type": "string", "description": "Artifact UUID"}},
        "required": ["artifact_id"],
    },
)
async def artifact_describe(artifact_id: str) -> str:
    """Return artifact metadata as key=value lines.

    Completeness fields are tri-state in the store: ``true``/``false`` are
    claims, ``unknown`` means the producer's provenance was never recorded —
    it never silently reads as complete (B0/C07).
    """
    agent_id, session_id = await active_agent_scope()
    try:
        meta = await artifact_store.describe(artifact_id, session_id=session_id, agent_id=agent_id)
    except ArtifactError as error:
        raise _fail(error) from None
    fields = (
        "artifact_id",
        "kind",
        "status",
        "source_version",
        "content_revision",
        "complete",
        "upstream_truncated",
        "stored_truncated",
        "record_count",
        "content_bytes",
        "token_estimate",
        "created_at",
        "expires_at",
        "access_count",
    )
    tri_state = {
        "source_version",
        "content_revision",
        "complete",
        "upstream_truncated",
        "stored_truncated",
        "record_count",
    }
    lines = [
        f"{field}={'unknown' if field in tri_state and meta.get(field) is None else meta.get(field)}"
        for field in fields
    ]
    if meta.get("summary"):
        lines.append(f"summary={meta['summary']}")
    return "\n".join(lines)


registry.declare_effects(
    {
        "artifact_read": ("context.read",),
        "artifact_search": ("context.read",),
        "artifact_describe": ("context.read",),
    }
)
