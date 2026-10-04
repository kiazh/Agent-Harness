"""Agent-callable, active-session-scoped search of past conversations."""

from __future__ import annotations

import uuid

from ah.core.exceptions import ToolError
from ah.core.session_recall import SessionRecall
from ah.tools.agents import active_agent_scope
from ah.tools.base import registry


async def _owner_agent() -> str:
    agent_id, _ = await active_agent_scope()
    return agent_id


@registry.register(
    name="session_recall",
    description="Find evidence in your own past conversation transcripts. Use session_recall_window to read surrounding messages.",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Words to find in past conversations"}
        },
        "required": ["query"],
    },
)
async def session_recall(query: str) -> str:
    """Return matching live and archived transcript chunks."""
    try:
        hits = await SessionRecall().discover(await _owner_agent(), query, limit=10)
    except ValueError as exc:
        raise ToolError(str(exc)) from None
    if not hits:
        return "No matching past conversations."
    return "\n".join(
        f"[{hit.source}] {hit.title or '(untitled)'} | session={hit.session_id} "
        f"chunk={hit.chunk_id or '-'} | {hit.preview[:160]}"
        for hit in hits
    )


@registry.register(
    name="session_recall_window",
    description="Read messages around a session_recall hit from one of your past conversations.",
    parameters={
        "type": "object",
        "properties": {
            "session_id": {"type": "string", "description": "Session UUID from session_recall"},
            "chunk_id": {"type": "string", "description": "Chunk UUID from session_recall"},
        },
        "required": ["session_id", "chunk_id"],
    },
)
async def session_recall_window(session_id: str, chunk_id: str) -> str:
    """Return a small chronological window around a matching chunk."""
    agent_id = await _owner_agent()
    try:
        target_session_id = uuid.UUID(session_id)
        target_chunk_id = uuid.UUID(chunk_id)
    except ValueError:
        raise ToolError("session_id and chunk_id must be UUIDs") from None
    messages = await SessionRecall().window(
        agent_id, target_session_id, target_chunk_id, before=2, after=2
    )
    if not messages:
        return "No matching chunk in your past conversations."
    return "\n".join(
        f"[{message.chunk_type}/{message.source}] "
        f"{str(message.payload.get('content', message.payload.get('text', message.payload)))[:300]}"
        for message in messages
    )
