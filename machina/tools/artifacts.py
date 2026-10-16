"""Agent-callable reads of retained tool artifacts (exact originals).

When a tool result is too large to carry whole, the full original is retained
as a session-scoped artifact and the model receives a handle. These tools page,
search, and describe those originals. Every call re-authorizes against the
active session's ancestry; expired or out-of-scope handles answer with an
explicit unavailable message, never a weaker fallback.
"""

from __future__ import annotations

from machina.core.artifacts import ArtifactError, artifact_store
from machina.core.exceptions import ToolError
from machina.tools.agents import active_agent_scope
from machina.tools.base import registry


def _fail(error: ArtifactError) -> ToolError:
    return ToolError(str(error))


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
    """
    agent_id, session_id = await active_agent_scope()
    try:
        page = await artifact_store.read(
            artifact_id,
            session_id=session_id,
            agent_id=agent_id,
            offset=offset,
            limit=limit,
            cursor=cursor or None,
        )
    except ArtifactError as error:
        raise _fail(error) from None
    start = page["offset"]
    end = start + page["returned_chars"]
    revision = page.get("content_revision") or "unknown"
    header = (
        f"[artifact {page['artifact_id']} kind={page['kind']} "
        f"chars {start}-{end} of {page['total_chars']} revision={revision}"
        f"{'; more remains — raise offset' if page['truncated'] else ''}]"
    )
    text = f"{header}\n{page['content']}"
    if page.get("next_cursor"):
        text += f"\nnext_cursor={page['next_cursor']}"
    return text


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
    """
    agent_id, session_id = await active_agent_scope()
    try:
        page = await artifact_store.search_page(
            query,
            session_id=session_id,
            agent_id=agent_id,
            artifact_id=artifact_id or None,
            limit=limit,
            cursor=cursor or None,
        )
    except ArtifactError as error:
        raise _fail(error) from None
    disclosure = (
        f"scanned={page['scanned']} scan_limit={page['scan_limit']} "
        f"scan_truncated={str(bool(page['scan_truncated'])).lower()}"
    )
    if page.get("next_cursor"):
        disclosure += f"\nnext_cursor={page['next_cursor']}"
    matches = page["matches"]
    if not matches:
        if page["scan_truncated"]:
            return (
                "No artifact matches in the scanned window; the window was cut short, "
                "so an unscanned artifact may still match — resume with the cursor.\n"
                f"{disclosure}"
            )
        if artifact_id:
            # Single-artifact scope: scanned=1 and not truncated, so the
            # bounded question has a definitive answer.
            return "No artifact matches."
        return (
            "No artifact matches; every readable artifact in scope was scanned.\n"
            f"{disclosure}"
        )
    lines = [
        f"artifact={m['artifact_id']} kind={m['kind']} chars={m['total_chars']} "
        f"pos={m['position']}: {m['excerpt']!r}"
        for m in matches
    ]
    lines.append(disclosure)
    return "\n".join(lines)


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
