"""Context chunks — token-efficient storage, retrieval, and prompt assembly."""
from __future__ import annotations

import uuid
from typing import Any

import asyncpg
import msgpack

from ah.db.connection import db
from ah.core.models import ContextChunk
from ah.core.serialization import (
    embedding_to_str,
    payload_to_msgpack,
    row_to_chunk,
)

__all__ = ["ContextChunk", "ContextManager", "context_manager"]


class ContextManager:
    """CRUD for context chunks stored as MessagePack with batch insert support."""

    def __init__(self, batch_size: int = 50) -> None:
        self._batch_size = batch_size
        self._pending: list[dict[str, Any]] = []

    async def add_chunk(
        self,
        session_id: uuid.UUID,
        agent_id: str,
        chunk_type: str,
        payload: dict[str, Any],
        token_count: int = 0,
        embedding: list[float] | None = None,
    ) -> ContextChunk:
        """Add a context chunk."""
        payload_msgpack = payload_to_msgpack(payload)
        embedding_str = None
        if embedding is not None:
            embedding_str = embedding_to_str(embedding)
        row = await db.fetchrow(
            """
            INSERT INTO context_chunks (session_id, agent_id, chunk_type, payload_msgpack, token_count, embedding)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id, session_id, agent_id, chunk_type, payload_msgpack, token_count, embedding, created_at, accessed_at
            """,
            session_id,
            agent_id,
            chunk_type,
            payload_msgpack,
            token_count,
            embedding_str,
        )
        return self._row_to_chunk(row)

    async def add_chunks_batch(
        self,
        chunks: list[dict[str, Any]],
    ) -> list[ContextChunk]:
        """Insert multiple context chunks in a single batch operation.

        Each dict in *chunks* must have keys: session_id, agent_id, chunk_type, payload.
        Optional keys: token_count, embedding.

        This is significantly faster than calling add_chunk() in a loop because
        it uses a single executemany() instead of N individual INSERTs.
        """
        if not chunks:
            return []

        # Prepare records for executemany
        records = []
        for c in chunks:
            payload_msgpack = payload_to_msgpack(c["payload"])
            embedding_str = None
            if c.get("embedding") is not None:
                embedding_str = embedding_to_str(c["embedding"])
            records.append((
                c["session_id"],
                c["agent_id"],
                c["chunk_type"],
                payload_msgpack,
                c.get("token_count", 0),
                embedding_str,
            ))

        # Use executemany for batch insert
        await db.executemany(
            """
            INSERT INTO context_chunks (session_id, agent_id, chunk_type, payload_msgpack, token_count, embedding)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            records,
        )

        # Fetch back the inserted rows (ordered by created_at DESC to match typical usage)
        session_ids = list({c["session_id"] for c in chunks})
        rows = await db.fetch(
            """
            SELECT id, session_id, agent_id, chunk_type, payload_msgpack, token_count, embedding, created_at, accessed_at
            FROM context_chunks
            WHERE session_id = ANY($1::uuid[])
            ORDER BY created_at DESC
            LIMIT $2
            """,
            session_ids,
            len(chunks),
        )
        return [self._row_to_chunk(r) for r in rows]

    async def get_chunks(
        self,
        session_id: uuid.UUID,
        chunk_type: str | None = None,
        limit: int = 50,
    ) -> list[ContextChunk]:
        """Get context chunks for a session, newest first."""
        if chunk_type:
            rows = await db.fetch(
                """
                SELECT id, session_id, agent_id, chunk_type, payload_msgpack, token_count, embedding, created_at, accessed_at
                FROM context_chunks
                WHERE session_id = $1 AND chunk_type = $2
                ORDER BY created_at DESC
                LIMIT $3
                """,
                session_id,
                chunk_type,
                limit,
            )
        else:
            rows = await db.fetch(
                """
                SELECT id, session_id, agent_id, chunk_type, payload_msgpack, token_count, embedding, created_at, accessed_at
                FROM context_chunks
                WHERE session_id = $1
                ORDER BY created_at DESC
                LIMIT $2
                """,
                session_id,
                limit,
            )
        return [self._row_to_chunk(r) for r in rows]

    async def get_recent_context(
        self, session_id: uuid.UUID, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Get recent context as a list of payloads (for prompt assembly)."""
        rows = await db.fetch(
            """
            SELECT payload_msgpack, chunk_type, token_count
            FROM context_chunks
            WHERE session_id = $1
            ORDER BY created_at DESC
            LIMIT $2
            """,
            session_id,
            limit,
        )
        results = []
        for r in rows:
            payload = msgpack.unpackb(r["payload_msgpack"], raw=False)
            results.append({
                "type": r["chunk_type"],
                "payload": payload,
                "tokens": r["token_count"],
            })
        return results

    async def search_by_embedding(
        self,
        session_id: uuid.UUID,
        query_embedding: list[float],
        top_k: int = 5,
        threshold: float = 0.7,
    ) -> list[tuple[ContextChunk, float]]:
        """Search context chunks by embedding similarity."""
        embedding_str = embedding_to_str(query_embedding)
        rows = await db.fetch(
            """
            SELECT *, 1 - (embedding <=> $1::vector) AS similarity
            FROM context_chunks
            WHERE session_id = $2 AND embedding IS NOT NULL
            ORDER BY embedding <=> $1::vector
            LIMIT $3
            """,
            embedding_str,
            session_id,
            top_k,
        )
        results = []
        for row in rows:
            sim = row["similarity"]
            if sim < threshold:
                continue
            chunk = self._row_to_chunk(row)
            results.append((chunk, sim))
        return results

    async def mark_accessed(self, chunk_id: uuid.UUID) -> None:
        """Mark a chunk as accessed (for LRU eviction)."""
        await db.execute(
            "UPDATE context_chunks SET accessed_at = now() WHERE id = $1",
            chunk_id,
        )

    async def delete_chunks(self, session_id: uuid.UUID) -> int:
        """Delete all chunks for a session."""
        result = await db.execute(
            "DELETE FROM context_chunks WHERE session_id = $1",
            session_id,
        )
        return int(result.split()[-1]) if result else 0

    async def get_token_usage(self, session_id: uuid.UUID) -> int:
        """Get total token count for a session."""
        return await db.fetchval(
            "SELECT COALESCE(SUM(token_count), 0) FROM context_chunks WHERE session_id = $1",
            session_id,
        )

    def _row_to_chunk(self, row: asyncpg.Record) -> ContextChunk:
        return row_to_chunk(row)

    @classmethod
    def reset(cls) -> None:
        """Reset the global ContextManager singleton to a fresh instance."""
        global context_manager
        context_manager = cls()


context_manager = ContextManager()
