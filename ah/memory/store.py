"""Memory store — async CRUD for long-term memories using asyncpg.

Follows the same patterns as ContextManager: asyncpg, typed dataclasses,
structured logging, and audit logging.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from ah.core.provider import audit_log
from ah.core.serialization import embedding_to_str, str_to_embedding
from ah.db.connection import db
from ah.memory.models import MemoryEntry
from ah.memory.redaction import SecretRedactor

__all__ = ["MemoryStore", "memory_store"]

logger = logging.getLogger(__name__)


class MemoryStore:
    """CRUD for long-term memories stored in PostgreSQL with pgvector."""

    def __init__(self) -> None:
        self._redactor = SecretRedactor()

    async def add(
        self,
        session_id: uuid.UUID | None,
        agent_id: str,
        content: str,
        category: str,
        importance: float = 0.5,
        embedding: list[float] | None = None,
        explicitly_important: bool = False,
        base_strength: float = 1.0,
    ) -> MemoryEntry:
        """Add a new memory entry. Secrets are redacted before storage."""
        # Redact secrets from content before storing
        redaction_result = self._redactor.redact(content)
        content = redaction_result.text

        embedding_str = None
        if embedding is not None:
            embedding_str = embedding_to_str(embedding)

        row = await db.fetchrow(
            """
            INSERT INTO memories (
                session_id, agent_id, content, category, importance,
                embedding, explicitly_important, base_strength
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING id, session_id, agent_id, content, category, importance,
                      created_at, last_accessed, access_count, embedding,
                      explicitly_important, base_strength
            """,
            session_id,
            agent_id,
            content,
            category,
            importance,
            embedding_str,
            explicitly_important,
            base_strength,
        )
        memory = self._row_to_entry(row)
        audit_log(
            "memory_add",
            memory_id=str(memory.id),
            agent_id=agent_id,
            category=category,
            importance=importance,
        )
        return memory

    async def get(self, memory_id: uuid.UUID) -> MemoryEntry | None:
        """Get a memory by ID."""
        row = await db.fetchrow(
            """
            SELECT id, session_id, agent_id, content, category, importance,
                   created_at, last_accessed, access_count, embedding,
                   explicitly_important, base_strength
            FROM memories WHERE id = $1
            """,
            memory_id,
        )
        if row is None:
            return None
        return self._row_to_entry(row)

    async def search(
        self,
        agent_id: str | None = None,
        category: str | None = None,
        limit: int = 20,
        offset: int = 0,
        session_id: uuid.UUID | None = None,
    ) -> list[MemoryEntry]:
        """Search memories with optional filters (fully parameterized)."""
        conditions = []
        params: list[Any] = []
        param_idx = 1

        if agent_id is not None:
            conditions.append(f"agent_id = ${param_idx}")
            params.append(agent_id)
            param_idx += 1

        if category is not None:
            conditions.append(f"category = ${param_idx}")
            params.append(category)
            param_idx += 1

        if session_id is not None:
            conditions.append(f"session_id = ${param_idx}")
            params.append(session_id)
            param_idx += 1

        where_clause = " AND ".join(conditions) if conditions else "TRUE"

        params.extend([limit, offset])
        rows = await db.fetch(
            f"""
            SELECT id, session_id, agent_id, content, category, importance,
                   created_at, last_accessed, access_count, embedding,
                   explicitly_important, base_strength
            FROM memories
            WHERE {where_clause}
            ORDER BY importance DESC, created_at DESC
            LIMIT ${param_idx} OFFSET ${param_idx + 1}
            """,
            *params,
        )
        return [self._row_to_entry(row) for row in rows]

    async def search_by_embedding(
        self,
        embedding: list[float],
        agent_id: str | None = None,
        category: str | None = None,
        limit: int = 5,
    ) -> list[tuple[MemoryEntry, float]]:
        """Search memories by embedding similarity (cosine distance).

        Returns list of (MemoryEntry, similarity_score) tuples, sorted by
        similarity descending.
        """
        embedding_str = embedding_to_str(embedding)

        conditions = ["embedding IS NOT NULL"]
        params: list[Any] = [embedding_str, limit]
        param_idx = 3

        if agent_id is not None:
            conditions.append(f"agent_id = ${param_idx}")
            params.append(agent_id)
            param_idx += 1

        if category is not None:
            conditions.append(f"category = ${param_idx}")
            params.append(category)
            param_idx += 1

        where_clause = " AND ".join(conditions)

        rows = await db.fetch(
            f"""
            SELECT id, session_id, agent_id, content, category, importance,
                   created_at, last_accessed, access_count, embedding,
                   explicitly_important, base_strength,
                   1 - (embedding <=> $1::vector) AS similarity
            FROM memories
            WHERE {where_clause}
            ORDER BY embedding <=> $1::vector
            LIMIT $2
            """,
            *params,
        )
        results = []
        for row in rows:
            entry = self._row_to_entry(row)
            similarity = row["similarity"]
            results.append((entry, similarity))
        return results

    async def delete(self, memory_id: uuid.UUID) -> bool:
        """Delete a memory by ID. Returns True if deleted."""
        result = await db.execute(
            "DELETE FROM memories WHERE id = $1",
            memory_id,
        )
        deleted = result != "DELETE 0"
        if deleted:
            audit_log("memory_delete", memory_id=str(memory_id))
        return deleted

    async def update_access(self, memory_id: uuid.UUID) -> None:
        """Update last_accessed and increment access_count."""
        await db.execute(
            """
            UPDATE memories
            SET last_accessed = now(), access_count = access_count + 1
            WHERE id = $1
            """,
            memory_id,
        )

    async def batch_update_access(self, memory_ids: list[uuid.UUID]) -> None:
        """Batch update last_accessed and increment access_count for multiple memories."""
        if not memory_ids:
            return
        await db.execute(
            """
            UPDATE memories
            SET last_accessed = now(), access_count = access_count + 1
            WHERE id = ANY($1::uuid[])
            """,
            memory_ids,
        )

    async def update_importance(self, memory_id: uuid.UUID, importance: float) -> None:
        """Update the importance score of a memory."""
        importance = max(0.0, min(1.0, importance))
        await db.execute(
            "UPDATE memories SET importance = $1 WHERE id = $2",
            importance,
            memory_id,
        )

    async def list_all(
        self,
        agent_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[MemoryEntry]:
        """List all memories with optional agent filter."""
        if agent_id:
            return await self.search(agent_id=agent_id, limit=limit, offset=offset)
        return await self.search(limit=limit, offset=offset)

    async def count(
        self, agent_id: str | None = None, session_id: uuid.UUID | None = None
    ) -> int:
        """Count memories, optionally filtered by agent and session."""
        if agent_id is not None and session_id is not None:
            result = await db.fetchval(
                "SELECT COUNT(*) FROM memories WHERE agent_id = $1 AND session_id = $2",
                agent_id,
                session_id,
            )
        elif session_id is not None:
            result = await db.fetchval(
                "SELECT COUNT(*) FROM memories WHERE session_id = $1",
                session_id,
            )
        elif agent_id is not None:
            result = await db.fetchval(
                "SELECT COUNT(*) FROM memories WHERE agent_id = $1",
                agent_id,
            )
        else:
            result = await db.fetchval("SELECT COUNT(*) FROM memories")
        return result or 0

    async def delete_by_session(self, session_id: uuid.UUID) -> int:
        """Delete all memories associated with a session. Returns count deleted."""
        result = await db.execute(
            "DELETE FROM memories WHERE session_id = $1",
            session_id,
        )
        # Parse "DELETE N" format
        from ah.db.connection import parse_command_count

        return parse_command_count(result)

    async def evict_weak_memories(
        self,
        threshold: float = 0.05,
        agent_id: str | None = None,
        max_memories: int | None = None,
    ) -> int:
        """Evict weak/unimportant memories to prevent unbounded growth.

        Args:
            threshold: Importance threshold below which memories are evicted.
            agent_id: Optional agent ID filter.
            max_memories: If set, evict down to this many memories per agent.

        Returns:
            Number of memories evicted.
        """
        if max_memories is not None:
            # Evict oldest/weakest memories beyond the limit
            if agent_id:
                count = await db.fetchval(
                    "SELECT COUNT(*) FROM memories WHERE agent_id = $1",
                    agent_id,
                )
                if count <= max_memories:
                    return 0
                # Delete oldest, least important memories beyond the limit
                result = await db.execute(
                    """
                    DELETE FROM memories
                    WHERE id IN (
                        SELECT id FROM memories
                        WHERE agent_id = $1
                        ORDER BY importance ASC, created_at ASC
                        LIMIT $2
                    )
                    """,
                    agent_id,
                    count - max_memories,
                )
            else:
                count = await db.fetchval("SELECT COUNT(*) FROM memories")
                if count <= max_memories:
                    return 0
                result = await db.execute(
                    """
                    DELETE FROM memories
                    WHERE id IN (
                        SELECT id FROM memories
                        ORDER BY importance ASC, created_at ASC
                        LIMIT $1
                    )
                    """,
                    count - max_memories,
                )
            from ah.db.connection import parse_command_count

            return parse_command_count(result)
        else:
            # Evict by importance threshold
            if agent_id:
                result = await db.execute(
                    "DELETE FROM memories WHERE agent_id = $1 AND importance < $2",
                    agent_id,
                    threshold,
                )
            else:
                result = await db.execute(
                    "DELETE FROM memories WHERE importance < $1",
                    threshold,
                )
            from ah.db.connection import parse_command_count

            return parse_command_count(result)

    async def get_weak_memories(
        self,
        threshold: float = 0.05,
        agent_id: str | None = None,
        limit: int = 50,
    ) -> list[MemoryEntry]:
        """Get memories with low importance (candidates for forgetting).

        This is a simple importance-based query. The full ForgettingModel
        computes time-decayed strength, but for DB-level filtering we use
        importance as a proxy.
        """
        if agent_id:
            rows = await db.fetch(
                """
                SELECT id, session_id, agent_id, content, category, importance,
                       created_at, last_accessed, access_count, embedding,
                       explicitly_important, base_strength
                FROM memories
                WHERE agent_id = $1 AND importance < $2
                ORDER BY importance ASC
                LIMIT $3
                """,
                agent_id,
                threshold,
                limit,
            )
        else:
            rows = await db.fetch(
                """
                SELECT id, session_id, agent_id, content, category, importance,
                       created_at, last_accessed, access_count, embedding,
                       explicitly_important, base_strength
                FROM memories
                WHERE importance < $1
                ORDER BY importance ASC
                LIMIT $2
                """,
                threshold,
                limit,
            )
        return [self._row_to_entry(row) for row in rows]

    def _row_to_entry(self, row: Any) -> MemoryEntry:
        """Convert a database row to a MemoryEntry."""
        embedding = None
        if row["embedding"] is not None:
            embedding = str_to_embedding(row["embedding"])

        return MemoryEntry(
            id=row["id"],
            session_id=row["session_id"],
            agent_id=row["agent_id"],
            content=row["content"],
            category=row["category"],
            importance=row["importance"],
            created_at=row["created_at"],
            last_accessed=row["last_accessed"],
            access_count=row["access_count"],
            embedding=embedding,
            explicitly_important=row["explicitly_important"],
            base_strength=row["base_strength"],
        )


# Global singleton
memory_store = MemoryStore()
