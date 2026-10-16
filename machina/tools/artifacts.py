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
        "artifact id."
    ),
    parameters={
        "type": "object",
        "properties": {
            "artifact_id": {"type": "string", "description": "Artifact UUID from a tool result"},
            "offset": {"type": "integer", "description": "Start character (0-based)"},
            "limit": {"type": "integer", "description": "Max characters to return (1-20000)"},
        },
        "required": ["artifact_id"],
    },
)
async def artifact_read(artifact_id: str, offset: int = 0, limit: int = 4000) -> str:
    """Return a bounded page of the exact original text."""
    agent_id, session_id = await active_agent_scope()
    try:
        page = await artifact_store.read(
            artifact_id,
            session_id=session_id,
            agent_id=agent_id,
            offset=offset,
            limit=limit,
        )
    except ArtifactError as error:
        raise _fail(error) from None
    start = page["offset"]
    end = start + page["returned_chars"]
    header = (
        f"[artifact {page['artifact_id']} kind={page['kind']} "
        f"chars {start}-{end} of {page['total_chars']}"
        f"{'; more remains — raise offset' if page['truncated'] else ''}]"
    )
    return f"{header}\n{page['content']}"


@registry.register(
    name="artifact_search",
    description=(
        "Search retained artifacts (one handle, or the session's recent "
        "artifacts) for a case-insensitive substring. Returns match positions "
        "and excerpts for precise paging with artifact_read."
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
        },
        "required": ["query"],
    },
)
async def artifact_search(query: str, artifact_id: str = "", limit: int = 5) -> str:
    """Return match positions and bounded excerpts."""
    agent_id, session_id = await active_agent_scope()
    try:
        matches = await artifact_store.search(
            query,
            session_id=session_id,
            agent_id=agent_id,
            artifact_id=artifact_id or None,
            limit=limit,
        )
    except ArtifactError as error:
        raise _fail(error) from None
    if not matches:
        return "No artifact matches."
    return "\n".join(
        f"artifact={m['artifact_id']} kind={m['kind']} chars={m['total_chars']} "
        f"pos={m['position']}: {m['excerpt']!r}"
        for m in matches
    )


@registry.register(
    name="artifact_describe",
    description=(
        "Show metadata for a retained artifact (kind, size, status, retention) "
        "without reading its content."
    ),
    parameters={
        "type": "object",
        "properties": {"artifact_id": {"type": "string", "description": "Artifact UUID"}},
        "required": ["artifact_id"],
    },
)
async def artifact_describe(artifact_id: str) -> str:
    """Return artifact metadata as key=value lines."""
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
        "content_bytes",
        "token_estimate",
        "created_at",
        "expires_at",
        "access_count",
    )
    lines = [f"{field}={meta.get(field)}" for field in fields]
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
