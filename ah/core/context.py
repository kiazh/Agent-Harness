"""Context chunks — token-efficient storage, retrieval, and prompt assembly."""

from __future__ import annotations

import logging
import uuid
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
from ah.core.text_search import build_or_tsquery
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

        # Use a single execute with RETURNING to insert all rows and get them
        # back in one round-trip, avoiding the executemany + extra fetch.
        rows = await db.fetch(
            """
            INSERT INTO context_chunks (id, session_id, agent_id, chunk_type, payload_msgpack, token_count, embedding, search_text)
            SELECT * FROM UNNEST($1::uuid[], $2::uuid[], $3::text[], $4::text[], $5::bytea[], $6::int[], $7::text[]::vector[], $8::text[])
            RETURNING id, session_id, agent_id, chunk_type, payload_msgpack, token_count, embedding, created_at, accessed_at
            """,
            [r[0] for r in records_with_ids],
            [r[1] for r in records_with_ids],
            [r[2] for r in records_with_ids],
            [r[3] for r in records_with_ids],
            [r[4] for r in records_with_ids],
            [r[5] for r in records_with_ids],
            [r[6] for r in records_with_ids],
            [r[7] for r in records_with_ids],
        )
        return [self._row_to_chunk(r) for r in rows]

    async def get_chunks(
        self,
        session_id: uuid.UUID,
        chunk_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ContextChunk]:
        """Get context chunks for a session, newest first.

        Deterministic tie order (created_at DESC, id DESC) so timestamp ties
        never scramble conversations or pagination.
        """
        if chunk_type:
            rows = await db.fetch(
                """
                SELECT id, session_id, agent_id, chunk_type, payload_msgpack, token_count, embedding, created_at, accessed_at
                FROM context_chunks
                WHERE session_id = $1 AND chunk_type = $2
                ORDER BY created_at DESC, id DESC
                LIMIT $3 OFFSET $4
                """,
                session_id,
                chunk_type,
                limit,
                offset,
            )
        else:
            rows = await db.fetch(
                """
                SELECT id, session_id, agent_id, chunk_type, payload_msgpack, token_count, embedding, created_at, accessed_at
                FROM context_chunks
                WHERE session_id = $1
                ORDER BY created_at DESC, id DESC
                LIMIT $2 OFFSET $3
                """,
                session_id,
                limit,
                offset,
            )
        return [self._row_to_chunk(r) for r in rows]

    async def get_chunks_before(
        self,
        session_id: uuid.UUID,
        *,
        limit: int = 500,
        before_time=None,
        before_id: uuid.UUID | None = None,
        chunk_type: str | None = None,
    ) -> list[ContextChunk]:
        """Keyset page: rows strictly older than (before_time, before_id).

        Cursor-based pagination is stable under concurrent inserts/deletes:
        new rows sort before the cursor and never duplicate into later pages;
        deleted rows are simply absent. NULL created_at sorts with the epoch
        floor, matching the eviction index.
        """
        if chunk_type:
            rows = await db.fetch(
                """
                SELECT id, session_id, agent_id, chunk_type, payload_msgpack, token_count, embedding, created_at, accessed_at
                FROM context_chunks
                WHERE session_id = $1 AND chunk_type = $2
                  AND ($3::timestamptz IS NULL OR (COALESCE(created_at, '0001-01-01 00:00:00+00'::timestamptz), id)
                       < ($3, $4::uuid))
                ORDER BY COALESCE(created_at, '0001-01-01 00:00:00+00'::timestamptz) DESC, id DESC
                LIMIT $5
                """,
                session_id,
                chunk_type,
                before_time,
                before_id,
                limit,
            )
        else:
            rows = await db.fetch(
                """
                SELECT id, session_id, agent_id, chunk_type, payload_msgpack, token_count, embedding, created_at, accessed_at
                FROM context_chunks
                WHERE session_id = $1
                  AND ($2::timestamptz IS NULL OR (COALESCE(created_at, '0001-01-01 00:00:00+00'::timestamptz), id)
                       < ($2, $3::uuid))
                ORDER BY COALESCE(created_at, '0001-01-01 00:00:00+00'::timestamptz) DESC, id DESC
                LIMIT $4
                """,
                session_id,
                before_time,
                before_id,
                limit,
            )
        return [self._row_to_chunk(r) for r in rows]

    async def get_all_chunks(self, session_id: uuid.UUID) -> list[ContextChunk]:
        """Fetch every active chunk for *session_id* (newest first).

        Keyset snapshot (5.7): pages by (created_at, id) cursor, dedupes by
        id, and returns exactly the validated input snapshot. Callers replace
        only these IDs, so concurrent inserts survive and competing
        compression cannot insert a stale summary twice for the same rows
        without detection (id-set comparison at the caller).
        """
        all_chunks: list[ContextChunk] = []
        seen: set[uuid.UUID] = set()
        before_time = None
        before_id: uuid.UUID | None = None
        while True:
            page = await self.get_chunks_before(
                session_id, limit=500, before_time=before_time, before_id=before_id
            )
            if not page:
                break
            fresh = [c for c in page if c.id not in seen]
            # Duplicate/missing-page detection: a page made only of seen ids
            # signals churn; stop rather than looping forever.
            if not fresh:
                break
            for c in fresh:
                seen.add(c.id)
            all_chunks.extend(fresh)
            if len(page) < 500:
                break
            last = page[-1]
            before_time = last.created_at
            before_id = last.id
        return all_chunks

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
            results.append(
                {
                    "type": r["chunk_type"],
                    "payload": payload,
                    "tokens": r["token_count"],
                }
            )
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

        Archives originals into context_archive before deletion (AH-009) so
        replacement is reversible. Runs in a single transaction so a failure
        can never leave the session with its context deleted, and preserves
        each chunk's ``created_at`` so conversation order (and recency-based
        retrieval) survive compression. Returns the number of chunks written.
        """
        # Fetch originals for archival (payload-exact).
        originals = await self.get_all_chunks(session_id)
        return await self.replace_chunks_by_ids(
            session_id,
            [c.id for c in originals],
            chunks,
            archive_reason="compressed",
        )

    async def replace_chunks_by_ids(
        self,
        session_id: uuid.UUID,
        original_ids: list[uuid.UUID],
        chunks: list[ContextChunk],
        archive_reason: str = "compressed",
    ) -> int:
        """Replace only *original_ids* with *chunks* (AH-008).

        Unlike :meth:`replace_chunks` (delete-all), concurrent inserts with
        IDs outside *original_ids* survive. Originals are archived
        transactionally before deletion (AH-009); a failure rolls back
        archive+delete+insert together so originals remain recoverable.
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
                if original_ids:
                    # Archive originals first (byte-exact payloads).
                    orig_rows = await conn.fetch(
                        """
                        SELECT id, agent_id, chunk_type, payload_msgpack, token_count,
                               embedding, created_at
                        FROM context_chunks
                        WHERE session_id = $1 AND id = ANY($2::uuid[])
                        FOR UPDATE
                        """,
                        session_id,
                        list(original_ids),
                    )
                    for r in orig_rows:
                        raw_payload = r["payload_msgpack"]
                        try:
                            search_text = _search_text_for(msgpack.unpackb(raw_payload, raw=False))
                        except Exception:
                            search_text = None
                        emb = r["embedding"]
                        try:
                            emb_str = embedding_to_str(str_to_embedding(emb)) if emb else None
                        except Exception:
                            emb_str = None
                        await conn.execute(
                            """
                            INSERT INTO context_archive
                                (session_id, chunk_id, payload_msgpack, embedding,
                                 archive_reason, agent_id, chunk_type, token_count,
                                 original_created_at, search_text)
                            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                            """,
                            session_id,
                            r["id"],
                            raw_payload,
                            emb_str,
                            archive_reason,
                            r["agent_id"],
                            r["chunk_type"],
                            r["token_count"],
                            r["created_at"],
                            search_text,
                        )
                    await conn.execute(
                        "DELETE FROM context_chunks WHERE session_id = $1 AND id = ANY($2::uuid[])",
                        session_id,
                        list(original_ids),
                    )
                else:
                    # No originals captured: do not delete-all (would erase
                    # concurrent inserts). Just insert the new chunks.
                    pass
                if records:
                    await conn.executemany(
                        """
                        INSERT INTO context_chunks
                            (id, session_id, agent_id, chunk_type, payload_msgpack, token_count, embedding, search_text, created_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                        """,
                        records,
                    )
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
        agent_id: str = "harness",
        chunk_type: str = "document",
        token_count: int = 0,
        created_at: datetime | None = None,
        connection: asyncpg.Connection | None = None,
    ) -> dict[str, Any] | None:
        """Archive a chunk to context_archive before deletion.

        Stores the byte-exact payload_msgpack and optional embedding for
        later resurrection via embedding similarity.
        """
        embedding_str = embedding_to_str(embedding) if embedding is not None else None
        payload = msgpack.unpackb(payload_msgpack, raw=False)
        executor = connection or db
        row = await executor.fetchrow(
            """
            INSERT INTO context_archive
                (session_id, chunk_id, payload_msgpack, embedding, archive_reason,
                 agent_id, chunk_type, token_count, original_created_at, search_text)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            RETURNING id, session_id, chunk_id, payload_msgpack, embedding, archived_at, archive_reason, resurrection_count, last_resurrected
            """,
            session_id,
            chunk_id,
            payload_msgpack,
            embedding_str,
            archive_reason,
            agent_id,
            chunk_type,
            token_count,
            created_at,
            _search_text_for(payload),
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
        Threshold is applied in SQL so LIMIT returns only qualifying rows.
        """
        embedding_str = embedding_to_str(query_embedding)
        rows = await db.fetch(
            """
            SELECT *, 1 - (embedding <=> $1::vector) AS similarity
            FROM context_archive
            WHERE session_id = $2 AND embedding IS NOT NULL
              AND 1 - (embedding <=> $1::vector) > $4
            ORDER BY embedding <=> $1::vector
            LIMIT $3
            """,
            embedding_str,
            session_id,
            top_k,
            threshold,
        )
        results = []
        # Batch the resurrection_count updates into a single query instead of
        # one UPDATE per row (N+1 pattern).
        if rows:
            ids = [row["id"] for row in rows]
            await db.execute(
                """
                UPDATE context_archive
                SET resurrection_count = resurrection_count + 1, last_resurrected = now()
                WHERE id = ANY($1::uuid[])
                """,
                ids,
            )
        for row in rows:
            sim = row["similarity"]
            # Build a ContextChunk from the archived row
            from ah.core.serialization import str_to_embedding

            payload = msgpack.unpackb(row["payload_msgpack"], raw=False)
            emb = str_to_embedding(row["embedding"]) if row["embedding"] else None
            chunk = ContextChunk(
                id=row["chunk_id"],
                session_id=row["session_id"],
                agent_id=row.get("agent_id") or "harness",
                chunk_type=row.get("chunk_type") or "document",
                payload=payload,
                token_count=row.get("token_count") or 0,
                embedding=emb,
                created_at=row.get("original_created_at") or row["archived_at"],
            )
            results.append((chunk, sim))
        return results

    async def search_archive_text(
        self, session_id: uuid.UUID, query: str, top_k: int = 3
    ) -> list[tuple[ContextChunk, float]]:
        """Recall archived conversation without requiring an embedding provider.

        Read-only: unlike resurrect_context, text search does not bump
        resurrection_count (only an actual resurrect increments it).
        """
        if not query.strip():
            return []
        tsquery = build_or_tsquery(query)
        if not tsquery:
            return []
        rows = await db.fetch(
            """
            SELECT *, ts_rank(
                to_tsvector('english', COALESCE(search_text, '')),
                to_tsquery('english', $2)
            ) AS similarity
            FROM context_archive
            WHERE session_id = $1 AND
                to_tsvector('english', COALESCE(search_text, '')) @@
                to_tsquery('english', $2)
            ORDER BY similarity DESC, archived_at DESC LIMIT $3
            """,
            session_id,
            tsquery,
            top_k,
        )
        result = []
        for row in rows:
            result.append(
                (
                    ContextChunk(
                        id=row["chunk_id"],
                        session_id=session_id,
                        agent_id=row.get("agent_id") or "harness",
                        chunk_type=row.get("chunk_type") or "document",
                        payload=msgpack.unpackb(row["payload_msgpack"], raw=False),
                        token_count=row.get("token_count") or 0,
                        created_at=row.get("original_created_at") or row["archived_at"],
                    ),
                    float(row["similarity"]),
                )
            )
        return result

    async def evict_old_chunks(
        self,
        session_id: uuid.UUID,
        max_tokens: int | None = None,
        max_chunks: int | None = None,
    ) -> int:
        """Evict old chunks to enforce token/chunk limits.

        Evicts oldest chunks first while preserving the most recent ten.
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

        # Combine SUM and COUNT into a single query to avoid two full-table scans
        row = await db.fetchrow(
            """
            SELECT COUNT(*) AS total_chunks, COALESCE(SUM(token_count), 0) AS total_tokens
            FROM context_chunks WHERE session_id = $1
            """,
            session_id,
        )
        if row is None:
            return 0
        total_chunks = int(row["total_chunks"])
        total_tokens = int(row["total_tokens"])

        if (max_tokens is None or total_tokens <= max_tokens) and (
            max_chunks is None or total_chunks <= max_chunks
        ):
            return 0

        remaining_evictable = max(0, total_chunks - 10)
        tokens_to_evict = total_tokens - max_tokens if max_tokens is not None else 0
        chunks_to_evict = total_chunks - max_chunks if max_chunks is not None else 0
        evicted = 0
        tokens_freed = 0
        chunks_freed = 0
        cursor_time: datetime | None = None
        cursor_id: uuid.UUID | None = None

        while remaining_evictable > 0:
            page_limit = min(100, remaining_evictable)
            rows = await db.fetch(
                """
                SELECT id, token_count, chunk_type, created_at, payload_msgpack,
                       embedding, agent_id
                FROM context_chunks
                WHERE session_id = $1 AND (
                    $2::timestamptz IS NULL OR
                    (COALESCE(created_at, '0001-01-01 00:00:00+00'::timestamptz), id)
                    > ($2, $3::uuid)
                )
                AND id NOT IN (
                    -- Hard floor: never select a chunk among the newest ten.
                    -- Concurrent callers each hold their own stale snapshot, so
                    -- without this a second caller's cursor advances past the
                    -- rows the first already deleted and removes the survivors.
                    SELECT id FROM context_chunks
                    WHERE session_id = $1
                    ORDER BY COALESCE(created_at, '0001-01-01 00:00:00+00'::timestamptz) DESC, id DESC
                    LIMIT 10
                )
                ORDER BY COALESCE(created_at, '0001-01-01 00:00:00+00'::timestamptz), id
                LIMIT $4
                """,
                session_id,
                cursor_time,
                cursor_id,
                page_limit,
            )
            # Some test doubles ignore SQL LIMIT; cap locally as well.
            page = rows[:page_limit]
            if not page:
                break
            remaining_evictable -= len(page)
            cursor_time = page[-1]["created_at"] or datetime(1, 1, 1, tzinfo=UTC)
            cursor_id = page[-1]["id"]

            selected = []
            selected_tokens = 0
            for row in page:
                if (max_tokens is None or tokens_freed + selected_tokens >= tokens_to_evict) and (
                    max_chunks is None or chunks_freed + len(selected) >= chunks_to_evict
                ):
                    break
                selected.append(row)
                selected_tokens += row["token_count"]
            if not selected:
                break

            try:
                async with db.acquire() as conn:
                    async with conn.transaction():
                        # Claim all rows at once to prevent concurrent eviction
                        # from archiving the same chunk twice.
                        claimed_rows = await conn.fetch(
                            "SELECT id FROM context_chunks WHERE id = ANY($1::uuid[]) FOR UPDATE",
                            [row["id"] for row in selected],
                        )
                        claimed_ids = {r["id"] for r in claimed_rows}
                        rows_to_archive = [row for row in selected if row["id"] in claimed_ids]

                        if rows_to_archive:
                            # Batch insert into context_archive
                            await conn.executemany(
                                """
                                INSERT INTO context_archive
                                    (session_id, chunk_id, payload_msgpack, embedding, archive_reason,
                                     agent_id, chunk_type, token_count, original_created_at, search_text)
                                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                                """,
                                [
                                    (
                                        session_id,
                                        row["id"],
                                        row.get("payload_msgpack") or b"",
                                        embedding_to_str(str_to_embedding(row["embedding"]))
                                        if row.get("embedding")
                                        else None,
                                        "evicted",
                                        row["agent_id"],
                                        row["chunk_type"],
                                        row["token_count"],
                                        row["created_at"],
                                        _search_text_for(
                                            msgpack.unpackb(
                                                row.get("payload_msgpack") or b"", raw=False
                                            )
                                        ),
                                    )
                                    for row in rows_to_archive
                                ],
                            )
                            # Batch delete
                            await conn.execute(
                                "DELETE FROM context_chunks WHERE id = ANY($1::uuid[])",
                                [row["id"] for row in rows_to_archive],
                            )
                        successful = rows_to_archive
            except Exception as batch_error:
                # A bad row must not turn eviction into data loss or block all
                # other rows; retry individually only on this exceptional path.
                logger.warning("Batch archive failed for session %s: %s", session_id, batch_error)
                successful = []
                for row in selected:
                    try:
                        async with db.acquire() as conn:
                            async with conn.transaction():
                                claimed = await conn.fetchval(
                                    "SELECT id FROM context_chunks WHERE id = $1 FOR UPDATE",
                                    row["id"],
                                )
                                if claimed is None:
                                    continue
                                payload_msgpack = row.get("payload_msgpack") or b""
                                embedding_raw = row.get("embedding")
                                embedding = (
                                    str_to_embedding(embedding_raw) if embedding_raw else None
                                )
                                archived = await self.archive_chunk(
                                    session_id=session_id,
                                    chunk_id=row["id"],
                                    payload_msgpack=payload_msgpack,
                                    embedding=embedding,
                                    archive_reason="evicted",
                                    agent_id=row["agent_id"],
                                    chunk_type=row["chunk_type"],
                                    token_count=row["token_count"],
                                    created_at=row["created_at"],
                                    connection=conn,
                                )
                                if archived is None:
                                    raise RuntimeError("archive insert returned no row")
                                await conn.execute(
                                    "DELETE FROM context_chunks WHERE id = $1", row["id"]
                                )
                                successful.append(row)
                    except Exception as error:
                        logger.warning("Failed to archive chunk %s: %s", row["id"], error)

            tokens_freed += sum(row["token_count"] for row in successful)
            chunks_freed += len(successful)
            evicted += len(successful)
            if (max_tokens is None or tokens_freed >= tokens_to_evict) and (
                max_chunks is None or chunks_freed >= chunks_to_evict
            ):
                break

        if evicted > 0:
            logger.info(
                "Evicted %d chunks (freed %d tokens) from session %s",
                evicted,
                tokens_freed,
                session_id,
            )

        return evicted

    def _row_to_chunk(self, row: asyncpg.Record) -> ContextChunk:
        return row_to_chunk(row)

    @classmethod
    def reset(cls) -> None:
        """Reset the global ContextManager singleton to a fresh instance."""
        global context_manager
        context_manager = cls()


context_manager = ContextManager()
