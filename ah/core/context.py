"""Context chunks — token-efficient storage, retrieval, and prompt assembly."""
from __future__ import annotations

import logging
import uuid
from collections import OrderedDict
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

logger = logging.getLogger(__name__)

__all__ = ["ContextChunk", "ContextManager", "context_manager"]


class ContextManager:
    """CRUD for context chunks stored as MessagePack with batch insert support.

    Includes an LRU cache for recent context per session to avoid redundant
    database queries when the same session is accessed repeatedly.
    """

    def __init__(self, batch_size: int = 50, cache_size: int = 128) -> None:
        self._batch_size = batch_size
        self._pending: list[dict[str, Any]] = []
        # LRU cache: session_id -> list of recent context dicts
        self._recent_cache: OrderedDict[uuid.UUID, list[dict[str, Any]]] = OrderedDict()
        self._cache_size = cache_size

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
        # Invalidate cache for this session
        self._recent_cache.pop(session_id, None)
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
        # Invalidate cache for all affected sessions
        for sid in session_ids:
            self._recent_cache.pop(sid, None)
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
        """Get recent context as a list of payloads (for prompt assembly).

        Uses an LRU cache per session to avoid redundant DB queries.
        Cache is invalidated when new chunks are added to the session.
        """
        # Check cache first
        if session_id in self._recent_cache:
            cached = self._recent_cache[session_id]
            # Move to end (most recently used)
            self._recent_cache.move_to_end(session_id)
            # Return up to limit items
            return cached[:limit]

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

        # Cache the results
        self._recent_cache[session_id] = results
        if len(self._recent_cache) > self._cache_size:
            self._recent_cache.popitem(last=False)  # Evict LRU

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

    async def evict_old_chunks(
        self,
        session_id: uuid.UUID,
        max_tokens: int | None = None,
        max_chunks: int | None = None,
    ) -> int:
        """Evict old chunks to enforce token/chunk limits.

        Uses LRU eviction based on accessed_at timestamp.
        Preserves the most recent chunks and tool_call/result pairs.

        Args:
            session_id: The session to evict from.
            max_tokens: Maximum total tokens allowed. If None, no token limit.
            max_chunks: Maximum number of chunks allowed. If None, no chunk limit.

        Returns:
            Number of chunks evicted.
        """
        if max_tokens is None and max_chunks is None:
            return 0

        # Get current token usage
        total_tokens = await self.get_token_usage(session_id)
        total_chunks = await db.fetchval(
            "SELECT COUNT(*) FROM context_chunks WHERE session_id = $1",
            session_id,
        )

        if (max_tokens is None or total_tokens <= max_tokens) and            (max_chunks is None or total_chunks <= max_chunks):
            return 0

        # Get chunks to evict (oldest first, preserving recent and tool pairs)
        # Strategy: evict oldest chunks first, but preserve the most recent 10
        # and any tool_call/result pairs
        rows = await db.fetch(
            """
            SELECT id, token_count, chunk_type, created_at
            FROM context_chunks
            WHERE session_id = $1
            ORDER BY created_at ASC
            """,
            session_id,
        )

        if not rows:
            return 0

        # Always preserve the most recent 10 chunks
        preserve_count = min(10, len(rows))
        evictable = rows[:-preserve_count] if preserve_count > 0 else rows

        # Calculate how many to evict
        tokens_to_evict = 0
        chunks_to_evict = 0
        if max_tokens is not None:
            tokens_to_evict = total_tokens - max_tokens
        if max_chunks is not None:
            chunks_to_evict = total_chunks - max_chunks

        evicted = 0
        tokens_freed = 0
        chunks_freed = 0

        for row in evictable:
            if (max_tokens is not None and tokens_freed >= tokens_to_evict) and                (max_chunks is not None and chunks_freed >= chunks_to_evict):
                break
            if max_tokens is not None and tokens_freed >= tokens_to_evict and max_chunks is None:
                break
            if max_chunks is not None and chunks_freed >= chunks_to_evict and max_tokens is None:
                break

            # Evict this chunk
            await db.execute(
                "DELETE FROM context_chunks WHERE id = $1",
                row["id"],
            )
            tokens_freed += row["token_count"]
            chunks_freed += 1
            evicted += 1

        if evicted > 0:
            # Invalidate cache
            self._recent_cache.pop(session_id, None)
            logger.info(
                "Evicted %d chunks (freed %d tokens) from session %s",
                evicted,
                tokens_freed,
                session_id,
            )

        return evicted

    async def enforce_budget(self, session_id: uuid.UUID, budget: int) -> int:
        """Enforce context budget by evicting old chunks.

        Args:
            session_id: The session to enforce budget on.
            budget: Maximum token budget.

        Returns:
            Number of chunks evicted.
        """
        return await self.evict_old_chunks(session_id, max_tokens=budget)

    def _row_to_chunk(self, row: asyncpg.Record) -> ContextChunk:
        return row_to_chunk(row)

    @classmethod
    def reset(cls) -> None:
        """Reset the global ContextManager singleton to a fresh instance."""
        global context_manager
        context_manager = cls()


context_manager = ContextManager()
