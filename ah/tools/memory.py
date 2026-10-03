"""Memory tools — remember() and recall() for agent tool use."""
from __future__ import annotations

import logging
import uuid

from ah.core.exceptions import ToolError, ValidationError
from ah.core.provider import audit_log
from ah.memory.store import memory_store
from ah.memory.retriever import MemoryRetriever
from ah.tools.base import registry

logger = logging.getLogger(__name__)


@registry.register(
    name="remember",
    description="Store a memory for future reference. Use when the user says 'remember this' or when you learn a durable fact, preference, or decision.",
    parameters={
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "The memory content to store",
            },
            "category": {
                "type": "string",
                "description": "Memory category",
                "enum": ["preference", "decision", "fact", "event", "transient"],
                "default": "fact",
            },
            "importance": {
                "type": "number",
                "description": "Importance score 0-1 (default 0.5)",
                "default": 0.5,
            },
        },
        "required": ["content"],
    },
)
async def remember(
    content: str,
    category: str = "fact",
    importance: float = 0.5,
    agent_id: str = "harness",
    session_id: str | None = None,
) -> str:
    """Store a memory for future reference.

    Args:
        content: The memory content to store.
        category: One of 'preference', 'decision', 'fact', 'event', 'transient'.
        importance: Importance score from 0.0 to 1.0.
        agent_id: The agent ID (defaults to 'harness').
        session_id: Optional session ID to associate with.

    Returns:
        Confirmation string with memory ID.
    """
    try:
        # Validate category
        valid_categories = {"preference", "decision", "fact", "event", "transient"}
        if category not in valid_categories:
            raise ValidationError(f"Invalid category '{category}'. Must be one of: {valid_categories}")

        # Clamp importance
        importance = max(0.0, min(1.0, importance))

        # Parse session_id if provided
        sid = None
        if session_id:
            try:
                sid = uuid.UUID(session_id)
            except ValueError:
                raise ValidationError(f"Invalid session_id '{session_id}'")

        # Create and store memory
        entry = await memory_store.add(
            session_id=sid,
            agent_id=agent_id,
            content=content,
            category=category,
            importance=importance,
            explicitly_important=True,
        )

        audit_log(
            "memory_remember",
            memory_id=str(entry.id),
            agent_id=agent_id,
            category=category,
            importance=importance,
        )

        return f"Memory stored: [{entry.id}] ({category}, importance={importance:.2f}) {content[:100]}"

    except Exception as e:
        logger.error("Failed to store memory: %s", e)
        raise ToolError(f"Error storing memory: {e}")


@registry.register(
    name="recall",
    description="Retrieve relevant memories. Use when you need past context, user preferences, or previous decisions.",
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query to find relevant memories",
            },
            "category": {
                "type": "string",
                "description": "Filter by category",
                "enum": ["preference", "decision", "fact", "event", "transient"],
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of memories to return (default 5)",
                "default": 5,
            },
        },
        "required": ["query"],
    },
)
async def recall(
    query: str,
    category: str | None = None,
    limit: int = 5,
    agent_id: str = "harness",
) -> str:
    """Retrieve relevant memories for a query.

    Args:
        query: Search query to find relevant memories.
        category: Optional category filter.
        limit: Maximum number of memories to return.
        agent_id: The agent ID (defaults to 'harness').

    Returns:
        Formatted string with retrieved memories.
    """
    try:
        retriever = MemoryRetriever(store=memory_store, top_k=limit)
        results = await retriever.retrieve(
            query=query,
            agent_id=agent_id,
            category=category,
        )

        if not results:
            return f"No memories found for query: '{query}'"

        lines = [f"Retrieved {len(results)} memories for '{query}':"]
        for i, rm in enumerate(results, 1):
            m = rm.memory
            lines.append(
                f"\n  [{i}] ({m.category}, importance={m.importance:.2f}, "
                f"score={rm.score:.3f}, source={rm.source})"
            )
            lines.append(f"      {m.content[:200]}")

        return "\n".join(lines)

    except Exception as e:
        logger.error("Failed to recall memories: %s", e)
        raise ToolError(f"Error recalling memories: {e}")
