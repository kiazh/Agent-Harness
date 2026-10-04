"""Context chunks — token-efficient storage, retrieval, and prompt assembly."""

from __future__ import annotations

import logging
import uuid
from collections import OrderedDict
from datetime import UTC, datetime
from typing import Any

import asyncpg
import msgpack

from ah.core.models import ContextChunk
from ah.core.serialization import (
    embedding_to_str,
    payload_to_msgpack,
    row_to_chunk,
    str_to_embedding,
)
from ah.db.connection import db

logger = logging.getLogger(__name__)

__all__ = ["ContextChunk", "ContextManager", "context_manager"]


def _search_text_for(payload: dict[str, Any]) -> str | None:
    """Derive full-text-search content from a chunk payload.

    Without this, only RAG documents had ``search_text`` set, so BM25/FTS in
    hybrid search never matched ordinary conversation or tool chunks.
    """
    parts: list[str] = []
    for key in ("content", "text", "tool", "result_preview", "result", "message", "prompt"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            parts.append(value)
    args = payload.get("args")
    if isinstance(args, dict):
        parts.extend(str(v) for v in args.values() if v is not None)
    text = " ".join(parts).strip()
    return text[:20000] or None


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
            INSERT INTO context_chunks (session_id, agent_id, chunk_type, payload_msgpack, token_count, embedding, search_text)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING id, session_id, agent_id, chunk_type, payload_msgpack, token_count, embedding, created_at, accessed_at
            """,
            session_id,
            agent_id,
            chunk_type,
            payload_msgpack,
            token_count,
            embedding_str,
            _search_text_for(payload),
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
            records.append(
                (
                    c["session_id"],
                    c["agent_id"],
                    c["chunk_type"],
                    payload_msgpack,
                    c.get("token_count", 0),
                    embedding_str,
                    _search_text_for(c["payload"]),
                )
            )

        # Assign ids client-side so the batch insert can be a single
        # executemany() *and* we can still fetch back exactly the rows we
        # inserted. Selecting by session_id with a LIMIT would also match
        # pre-existing rows in that session (created_at is identical for every
        # row written in one transaction, so the ordering is nondeterministic).
        chunk_ids = [uuid.uuid4() for _ in records]
        records_with_ids = [(cid, *rec) for cid, rec in zip(chunk_ids, records, strict=True)]

        # Use executemany for batch insert
        await db.executemany(
            """
            INSERT INTO context_chunks (id, session_id, agent_id, chunk_type, payload_msgpack, token_count, embedding, search_text)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            records_with_ids,
        )

        session_ids = list({c["session_id"] for c in chunks})
        # Invalidate cache for all affected sessions
        for sid in session_ids:
            self._recent_cache.pop(sid, None)
        rows = await db.fetch(
            """
            SELECT id, session_id, agent_id, chunk_type, payload_msgpack, token_count, embedding, created_at, accessed_at
            FROM context_chunks
            WHERE id = ANY($1::uuid[])
            ORDER BY created_at DESC, id DESC
            """,
            chunk_ids,
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
        # Check cache first. Entries store (fetched_limit, rows); a hit is only
        # valid if the cached fetch covered at least the requested limit, or the
        # session simply had fewer rows than were asked for.
        if session_id in self._recent_cache:
            cached_limit, cached = self._recent_cache[session_id]
            if cached_limit >= limit or len(cached) < cached_limit:
                self._recent_cache.move_to_end(session_id)
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
            results.append(
                {
                    "type": r["chunk_type"],
                    "payload": payload,
                    "tokens": r["token_count"],
                }
            )

        # Cache the results with the limit they were fetched at
        self._recent_cache[session_id] = (limit, results)
        self._recent_cache.move_to_end(session_id)
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

    async def replace_chunks(self, session_id: uuid.UUID, chunks: list[ContextChunk]) -> int:
        """Atomically replace all chunks of a session with *chunks*.

        Runs in a single transaction so a failure can never leave the session
        with its context deleted, and preserves each chunk's ``created_at`` so
        conversation order (and recency-based retrieval) survive compression.
        Returns the number of chunks written.
        """
        records = []
        for c in chunks:
            created = c.created_at or datetime.now(UTC)
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            records.append(
                (
                    uuid.uuid4(),
                    session_id,
                    c.agent_id,
                    c.chunk_type,
                    payload_to_msgpack(c.payload),
                    c.token_count,
                    embedding_to_str(c.embedding) if c.embedding is not None else None,
                    _search_text_for(c.payload),
                    created,
                )
            )

        async with db.acquire() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM context_chunks WHERE session_id = $1", session_id)
                if records:
                    await conn.executemany(
                        """
                        INSERT INTO context_chunks
                            (id, session_id, agent_id, chunk_type, payload_msgpack, token_count, embedding, search_text, created_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                        """,
                        records,
                    )
        self._recent_cache.pop(session_id, None)
        return len(records)

    async def delete_chunks(self, session_id: uuid.UUID) -> int:
        """Delete all chunks for a session."""
        result = await db.execute(
            "DELETE FROM context_chunks WHERE session_id = $1",
            session_id,
        )
        from ah.db.connection import parse_command_count

        return parse_command_count(result)

    async def get_token_usage(self, session_id: uuid.UUID) -> int:
        """Get total token count for a session."""
        return await db.fetchval(
            "SELECT COALESCE(SUM(token_count), 0) FROM context_chunks WHERE session_id = $1",
            session_id,
        )

    async def archive_chunk(
        self,
        session_id: uuid.UUID,
        chunk_id: uuid.UUID,
        payload_msgpack: bytes,
        embedding: list[float] | None = None,
        archive_reason: str = "evicted",
    ) -> dict[str, Any] | None:
        """Archive a chunk to context_archive before deletion.

        Stores the byte-exact payload_msgpack and optional embedding for
        later resurrection via embedding similarity.
        """
        embedding_str = embedding_to_str(embedding) if embedding is not None else None
        row = await db.fetchrow(
            """
            INSERT INTO context_archive (session_id, chunk_id, payload_msgpack, embedding, archive_reason)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id, session_id, chunk_id, payload_msgpack, embedding, archived_at, archive_reason, resurrection_count, last_resurrected
            """,
            session_id,
            chunk_id,
            payload_msgpack,
            embedding_str,
            archive_reason,
        )
        return row

    async def resurrect_context(
        self,
        session_id: uuid.UUID,
        query_embedding: list[float],
        top_k: int = 5,
        threshold: float = 0.7,
    ) -> list[tuple[ContextChunk, float]]:
        """Retrieve archived chunks by embedding similarity.

        Queries context_archive for chunks semantically similar to the query,
        increments resurrection_count, and returns (chunk, similarity) tuples.
        """
        embedding_str = embedding_to_str(query_embedding)
        rows = await db.fetch(
            """
            SELECT *, 1 - (embedding <=> $1::vector) AS similarity
            FROM context_archive
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
            # Increment resurrection_count
            await db.execute(
                """
                UPDATE context_archive
                SET resurrection_count = resurrection_count + 1, last_resurrected = now()
                WHERE id = $1
                """,
                row["id"],
            )
            # Build a ContextChunk from the archived row
            from ah.core.serialization import str_to_embedding

            payload = msgpack.unpackb(row["payload_msgpack"], raw=False)
            emb = str_to_embedding(row["embedding"]) if row["embedding"] else None
            chunk = ContextChunk(
                id=row["chunk_id"],
                session_id=row["session_id"],
                agent_id="harness",
                chunk_type="archived",
                payload=payload,
                embedding=emb,
                created_at=row["archived_at"],
            )
            results.append((chunk, sim))
        return results

    async def evict_old_chunks(
        self,
        session_id: uuid.UUID,
        max_tokens: int | None = None,
        max_chunks: int | None = None,
    ) -> int:
        """Evict old chunks to enforce token/chunk limits.

        Uses LRU eviction based on accessed_at timestamp.
        Preserves the most recent chunks and tool_call/result pairs.
        Archives chunks to context_archive before deletion (reversible eviction).

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

        if (max_tokens is None or total_tokens <= max_tokens) and (
            max_chunks is None or total_chunks <= max_chunks
        ):
            return 0

        # Get chunks to evict (oldest first, preserving recent and tool pairs)
        # Strategy: evict oldest chunks first, but preserve the most recent 10
        # and any tool_call/result pairs
        rows = await db.fetch(
            """
            SELECT id, token_count, chunk_type, created_at, payload_msgpack, embedding, agent_id
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
            # Stop when all active eviction targets are met
            tokens_done = max_tokens is None or tokens_freed >= tokens_to_evict
            chunks_done = max_chunks is None or chunks_freed >= chunks_to_evict
            if tokens_done and chunks_done:
                break

            # Archive before delete (reversible eviction)
            # Use .get() for fields that may be absent in test mocks
            try:
                payload_msgpack = row.get("payload_msgpack") or b""
                embedding_raw = row.get("embedding")
                embedding = str_to_embedding(embedding_raw) if embedding_raw else None
                await self.archive_chunk(
                    session_id=session_id,
                    chunk_id=row["id"],
                    payload_msgpack=payload_msgpack,
                    embedding=embedding,
                    archive_reason="evicted",
                )
            except Exception as e:
                # If archiving fails, log and proceed with deletion
                logger.warning("Failed to archive chunk %s: %s", row["id"], e)

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
