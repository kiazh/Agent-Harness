"""Context chunks — token-efficient storage, retrieval, and prompt assembly."""

from __future__ import annotations

import base64
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

import asyncpg
import msgpack

from ah.core.images import MAX_IMAGE_BYTES, MAX_IMAGES, MAX_TOTAL_IMAGE_BYTES, validate_images
from ah.core.models import ContextChunk
from ah.core.serialization import (
    embedding_to_str,
    payload_to_msgpack,
    row_to_chunk,
    str_to_embedding,
)
from ah.core.text_search import build_or_tsquery
from ah.db.lazy import LazyDb

logger = logging.getLogger(__name__)

__all__ = ["ContextChunk", "ContextManager", "context_manager"]

db = LazyDb()

# Five 255-character names can consume 5100 UTF-8 bytes; allow the bounded
# keys, media types and MessagePack headers in addition to those names.
_MAX_IMAGE_CONTEXT_BYTES = MAX_TOTAL_IMAGE_BYTES + 8192


def _document_source_for(
    chunk_type: Any, payload: dict[str, Any], current: str | None
) -> str | None:
    """Resolve a document chunk's source: explicit value wins, else the
    payload's source/metadata.source fallback. Never raises."""
    if current is not None or chunk_type != "document":
        return current
    try:
        src = payload.get("source")
        if isinstance(src, str) and src:
            return src
        meta = payload.get("metadata")
        if isinstance(meta, dict):
            msrc = meta.get("source")
            if isinstance(msrc, str) and msrc:
                return msrc
    except Exception:
        return None
    return None


def _is_missing_image_relation(exc: BaseException) -> bool:
    """Detect a missing session_image_context table (unmigrated database).

    Reads degrade to no images and writes fall back to plain chunk inserts
    so a pending runtime migration never loses prompts or kills turns.
    The relation name is matched so an unrelated missing table stays loud.
    """
    if isinstance(exc, asyncpg.UndefinedTableError):
        return "session_image_context" in str(exc)
    return getattr(exc, "sqlstate", None) == "42P01" and "session_image_context" in str(exc)


# Sessions whose full legacy scan found no images while the cache table
# exists. Consulted only on a cache miss; any image-bearing write through
# add_chunk/add_chunks_batch creates a cache row (checked first) and discards
# the mark, so marks can never suppress newly arrived images. Dead sessions
# leave harmless small entries until process exit.
_NO_LEGACY_IMAGES: set[tuple[uuid.UUID, str]] = set()

# Bound the per-turn legacy scan: one DB round trip per candidate row.
_LEGACY_SCAN_ROW_LIMIT = 50


_ADD_CHUNK_SQL = """
    INSERT INTO context_chunks (session_id, agent_id, chunk_type, payload_msgpack, token_count, embedding, search_text, document_source)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
    RETURNING id, session_id, agent_id, chunk_type, payload_msgpack, token_count, embedding, created_at, accessed_at, document_source
    """

_ADD_CHUNK_WITH_IMAGES_SQL = """
    WITH inserted AS (
        INSERT INTO context_chunks (session_id, agent_id, chunk_type, payload_msgpack, token_count, embedding, search_text, document_source)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        RETURNING id, session_id, agent_id, chunk_type, payload_msgpack, token_count, embedding, created_at, accessed_at, document_source
    ), latest_images AS (
        INSERT INTO session_image_context (session_id, agent_id, images_msgpack)
        SELECT session_id, agent_id, $9::bytea FROM inserted
        ON CONFLICT (session_id, agent_id) DO UPDATE
        SET images_msgpack=EXCLUDED.images_msgpack, updated_at=now()
    )
    SELECT * FROM inserted
    """

_ADD_CHUNKS_BATCH_SQL = """
    INSERT INTO context_chunks (id, session_id, agent_id, chunk_type, payload_msgpack, token_count, embedding, search_text, document_source)
    SELECT * FROM UNNEST($1::uuid[], $2::uuid[], $3::text[], $4::text[], $5::bytea[], $6::int[], $7::text[]::vector[], $8::text[], $9::text[])
    RETURNING id, session_id, agent_id, chunk_type, payload_msgpack, token_count, embedding, created_at, accessed_at, document_source
    """

_ADD_CHUNKS_BATCH_WITH_IMAGES_SQL = """
    WITH inserted AS (
        INSERT INTO context_chunks (id, session_id, agent_id, chunk_type, payload_msgpack, token_count, embedding, search_text, document_source)
        SELECT * FROM UNNEST($1::uuid[], $2::uuid[], $3::text[], $4::text[], $5::bytea[], $6::int[], $7::text[]::vector[], $8::text[], $9::text[])
        RETURNING id, session_id, agent_id, chunk_type, payload_msgpack, token_count, embedding, created_at, accessed_at, document_source
    ), latest_images AS (
        INSERT INTO session_image_context (session_id, agent_id, images_msgpack)
        SELECT * FROM UNNEST($10::uuid[], $11::text[], $12::bytea[]) WHERE TRUE
        ON CONFLICT (session_id, agent_id) DO UPDATE
        SET images_msgpack=EXCLUDED.images_msgpack, updated_at=now()
    )
    SELECT * FROM inserted
    """

_SEED_LATEST_IMAGES_SQL = """
    INSERT INTO session_image_context (session_id, agent_id, images_msgpack)
    VALUES ($1, $2, $3::bytea)
    ON CONFLICT (session_id, agent_id) DO NOTHING
    """


def _image_context_for(chunk_type: str, payload: dict[str, Any]) -> bytes | None:
    if chunk_type not in ("user", "user_message") or not payload.get("images"):
        return None
    images = validate_images(payload["images"])
    # Store decoded bytes so the durable latest-only context does not retain
    # the base64 expansion. The metadata allowance is bounded separately.
    encoded = msgpack.packb(
        [{**image, "data": base64.b64decode(image["data"], validate=True)} for image in images],
        use_bin_type=True,
    )
    if len(encoded) > _MAX_IMAGE_CONTEXT_BYTES:
        raise ValueError("image context exceeds storage limit")
    return encoded


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
    """CRUD for context chunks stored as MessagePack with batch insert support."""

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
        document_source: str | None = None,
    ) -> ContextChunk:
        """Add a context chunk."""
        payload_msgpack = payload_to_msgpack(payload)
        embedding_str = None
        if embedding is not None:
            embedding_str = embedding_to_str(embedding)
        document_source = _document_source_for(chunk_type, payload, document_source)
        query = _ADD_CHUNK_SQL
        base_args = (
            session_id,
            agent_id,
            chunk_type,
            payload_msgpack,
            token_count,
            embedding_str,
            _search_text_for(payload),
            document_source,
        )
        image_context = _image_context_for(chunk_type, payload)
        if image_context is None:
            row = await db.fetchrow(query, *base_args)
            return self._row_to_chunk(row)
        _NO_LEGACY_IMAGES.discard((session_id, agent_id))
        try:
            row = await db.fetchrow(_ADD_CHUNK_WITH_IMAGES_SQL, *base_args, image_context)
        except Exception as exc:
            if not _is_missing_image_relation(exc):
                raise
            logger.warning(
                "session_image_context missing for session %s agent %s; "
                "stored chunk payload intact without image-cache update",
                session_id,
                agent_id,
                exc_info=True,
            )
            row = await db.fetchrow(query, *base_args)
        return self._row_to_chunk(row)

    async def add_chunks_batch(
        self,
        chunks: list[dict[str, Any]],
    ) -> list[ContextChunk]:
        """Insert multiple context chunks in a single batch operation.

        Each dict in *chunks* must have keys: session_id, agent_id, chunk_type, payload.
        Optional keys: token_count, embedding, document_source.

        This is significantly faster than calling add_chunk() in a loop because
        it uses a single execute with UNNEST + RETURNING instead of N individual INSERTs.
        """
        if not chunks:
            return []

        # Prepare columns for the single UNNEST execute
        records = []
        image_updates: dict[tuple[uuid.UUID, str], bytes] = {}
        for c in chunks:
            image_context = _image_context_for(c["chunk_type"], c["payload"])
            if image_context is not None:
                image_updates[(c["session_id"], c["agent_id"])] = image_context
            payload_msgpack = payload_to_msgpack(c["payload"])
            embedding_str = None
            if c.get("embedding") is not None:
                embedding_str = embedding_to_str(c["embedding"])
            doc_src = _document_source_for(
                c.get("chunk_type"), c.get("payload") or {}, c.get("document_source")
            )
            records.append(
                (
                    c["session_id"],
                    c["agent_id"],
                    c["chunk_type"],
                    payload_msgpack,
                    c.get("token_count", 0),
                    embedding_str,
                    _search_text_for(c["payload"]),
                    doc_src,
                )
            )

        # Assign ids client-side so the batch insert can be a single
        # UNNEST execute *and* we can still fetch back exactly the rows we
        # inserted. Selecting by session_id with a LIMIT would also match
        # pre-existing rows in that session (created_at is identical for every
        # row written in one transaction, so the ordering is nondeterministic).
        chunk_ids = [uuid.uuid4() for _ in records]
        records_with_ids = [(cid, *rec) for cid, rec in zip(chunk_ids, records, strict=True)]

        # Use a single execute with RETURNING to insert all rows and get them
        # back in one round-trip, avoiding the executemany + extra fetch.
        query = _ADD_CHUNKS_BATCH_SQL
        base_args = (
            [r[0] for r in records_with_ids],
            [r[1] for r in records_with_ids],
            [r[2] for r in records_with_ids],
            [r[3] for r in records_with_ids],
            [r[4] for r in records_with_ids],
            [r[5] for r in records_with_ids],
            [r[6] for r in records_with_ids],
            [r[7] for r in records_with_ids],
            [r[8] for r in records_with_ids],
        )
        if not image_updates:
            rows = await db.fetch(query, *base_args)
            return [self._row_to_chunk(r) for r in rows]
        for key in image_updates:
            _NO_LEGACY_IMAGES.discard(key)
        try:
            rows = await db.fetch(
                _ADD_CHUNKS_BATCH_WITH_IMAGES_SQL,
                *base_args,
                [key[0] for key in image_updates],
                [key[1] for key in image_updates],
                list(image_updates.values()),
            )
        except Exception as exc:
            if not _is_missing_image_relation(exc):
                raise
            logger.warning(
                "session_image_context missing; stored batch payloads intact without image-cache update",
                exc_info=True,
            )
            rows = await db.fetch(query, *base_args)
        return [self._row_to_chunk(r) for r in rows]

    async def get_latest_images(self, session_id: uuid.UUID, agent_id: str) -> list[dict[str, str]]:
        """Retrieve the latest image-bearing user turn independently of compaction.

        A missing session_image_context table (pending runtime migration)
        falls back to a legacy chunk scan rather than failing the turn. A
        corrupt cache row is quarantined (deleted) and likewise falls back
        to legacy chunks, so one bad row cannot fail every turn.
        """
        try:
            row = await db.fetchrow(
                "SELECT images_msgpack FROM session_image_context WHERE session_id=$1 AND agent_id=$2",
                session_id,
                agent_id,
            )
        except Exception as exc:
            if not _is_missing_image_relation(exc):
                raise
            logger.warning(
                "session_image_context missing for session %s agent %s; "
                "stored chunk payloads intact, serving legacy images unseeded",
                session_id,
                agent_id,
                exc_info=True,
            )
            return await self._get_legacy_images_and_seed(
                session_id, agent_id, allow_negative_mark=False
            )
        if row is None:
            if (session_id, agent_id) in _NO_LEGACY_IMAGES:
                return []
            return await self._get_legacy_images_and_seed(session_id, agent_id)
        try:
            return self._decode_image_context(row["images_msgpack"])
        except Exception:
            logger.error(
                "corrupt session_image_context row for session %s agent %s; "
                "quarantining row and falling back to legacy chunks",
                session_id,
                agent_id,
                exc_info=True,
            )
            try:
                await db.execute(
                    "DELETE FROM session_image_context WHERE session_id=$1 AND agent_id=$2",
                    session_id,
                    agent_id,
                )
            except Exception as cleanup_exc:
                if not _is_missing_image_relation(cleanup_exc):
                    logger.warning(
                        "corrupt image-cache row could not be quarantined for session %s agent %s",
                        session_id,
                        agent_id,
                        exc_info=True,
                    )
            return await self._get_legacy_images_and_seed(session_id, agent_id)

    @staticmethod
    def _decode_image_context(raw: bytes) -> list[dict[str, str]]:
        if len(raw) > _MAX_IMAGE_CONTEXT_BYTES:
            raise ValueError("saved image context exceeds storage limit")
        images = msgpack.unpackb(
            raw,
            raw=False,
            max_bin_len=MAX_IMAGE_BYTES,
            max_array_len=MAX_IMAGES,
            max_map_len=3,
            max_str_len=1024,
        )
        if not isinstance(images, list):
            raise ValueError("saved image context is invalid")
        try:
            encoded = [
                {**image, "data": base64.b64encode(image["data"]).decode("ascii")}
                for image in images
            ]
        except (KeyError, TypeError) as exc:
            raise ValueError("saved image context is invalid") from exc
        return validate_images(encoded)

    async def _get_legacy_images_and_seed(
        self, session_id: uuid.UUID, agent_id: str, *, allow_negative_mark: bool = True
    ) -> list[dict[str, str]]:
        """Read older image-bearing turns and seed the durable cache.

        The recovered bytes are written back with DO NOTHING semantics so a
        later compaction that removes the source chunks cannot lose them, and
        a concurrent newer write is never clobbered. Seeding is best-effort:
        the recovered images are returned even if the seed write fails.
        When the table exists and the scan exhausts with no images, the
        session/agent pair is marked so later turns skip the repeat scan;
        any image-bearing write clears the mark (and a fresh cache row always
        takes precedence over it).
        """
        images = await self._get_legacy_images(session_id, agent_id)
        if not images:
            if allow_negative_mark:
                _NO_LEGACY_IMAGES.add((session_id, agent_id))
            return images
        _NO_LEGACY_IMAGES.discard((session_id, agent_id))
        try:
            packed = _image_context_for("user_message", {"images": images})
        except ValueError:
            logger.warning(
                "legacy images (%d image(s)) exceed cache bound for session %s agent %s; "
                "serving without seeding, compaction may lose them",
                len(images),
                session_id,
                agent_id,
            )
            return images
        try:
            await db.execute(_SEED_LATEST_IMAGES_SQL, session_id, agent_id, packed)
        except Exception as exc:
            if _is_missing_image_relation(exc):
                logger.warning(
                    "session_image_context missing for session %s agent %s; "
                    "serving legacy images unseeded",
                    session_id,
                    agent_id,
                    exc_info=True,
                )
            else:
                logger.warning(
                    "latest-image seed write failed for session %s agent %s; "
                    "serving recovered images",
                    session_id,
                    agent_id,
                    exc_info=True,
                )
        return images

    async def _get_legacy_images(
        self, session_id: uuid.UUID, agent_id: str
    ) -> list[dict[str, str]]:
        """Read older image-bearing turns when the latest-image cache is empty.

        Filter on the MessagePack 'images' key to avoid fetching text-only
        history. Inspect one bounded payload at a time and retain session and
        agent isolation. New writes always use the durable cache above.
        The scan is capped: corrupt rows are skipped (newest valid wins) and
        exhaustion returns no images rather than looping forever. NULL
        timestamps use the same epoch floor as the other readers so the
        keyset cursor always advances.
        """
        before = None
        for _ in range(_LEGACY_SCAN_ROW_LIMIT):
            row = await db.fetchrow(
                """SELECT id, created_at, payload_msgpack FROM context_chunks
                   WHERE session_id=$1 AND agent_id=$2
                     AND chunk_type IN ('user', 'user_message')
                     AND octet_length(payload_msgpack) <= $3
                     AND position($4::bytea IN payload_msgpack) > 0
                     AND ($6::uuid IS NULL OR (COALESCE(created_at, '0001-01-01 00:00:00+00'::timestamptz), id)
                               < (COALESCE($5::timestamptz, '0001-01-01 00:00:00+00'::timestamptz), $6::uuid))
                   ORDER BY COALESCE(created_at, '0001-01-01 00:00:00+00'::timestamptz) DESC,
                            id DESC LIMIT 1""",
                session_id,
                agent_id,
                MAX_TOTAL_IMAGE_BYTES * 2,
                # b"\xa6images" is the MessagePack fixstr encoding of the
                # 'images' key: pre-filters text-only rows. The size cap is 2x
                # the image budget to admit base64 expansion in old payloads.
                b"\xa6images",
                before[0] if before else None,
                before[1] if before else None,
            )
            if row is None:
                return []
            try:
                payload = msgpack.unpackb(
                    row["payload_msgpack"],
                    raw=False,
                    max_str_len=MAX_TOTAL_IMAGE_BYTES * 2,
                    max_bin_len=MAX_IMAGE_BYTES,
                    max_array_len=MAX_IMAGES,
                    max_map_len=1024,
                )
                if isinstance(payload, dict) and payload.get("images"):
                    return validate_images(payload["images"])
            except Exception:
                logger.warning(
                    "skipping corrupt legacy chunk %s during image recovery",
                    row["id"],
                    exc_info=True,
                )
            before = (row["created_at"], row["id"])
        logger.warning(
            "legacy image scan exhausted %d rows without images for session %s agent %s; stopping",
            _LEGACY_SCAN_ROW_LIMIT,
            session_id,
            agent_id,
        )
        return []

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
                SELECT id, session_id, agent_id, chunk_type, payload_msgpack, token_count, embedding, created_at, accessed_at, document_source
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
                SELECT id, session_id, agent_id, chunk_type, payload_msgpack, token_count, embedding, created_at, accessed_at, document_source
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
                SELECT id, session_id, agent_id, chunk_type, payload_msgpack, token_count, embedding, created_at, accessed_at, document_source
                FROM context_chunks
                WHERE session_id = $1 AND chunk_type = $2
                  AND ($4::uuid IS NULL OR (COALESCE(created_at, '0001-01-01 00:00:00+00'::timestamptz), id)
                       < (COALESCE($3::timestamptz, '0001-01-01 00:00:00+00'::timestamptz), $4::uuid))
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
                SELECT id, session_id, agent_id, chunk_type, payload_msgpack, token_count, embedding, created_at, accessed_at, document_source
                FROM context_chunks
                WHERE session_id = $1
                  AND ($3::uuid IS NULL OR (COALESCE(created_at, '0001-01-01 00:00:00+00'::timestamptz), id)
                       < (COALESCE($2::timestamptz, '0001-01-01 00:00:00+00'::timestamptz), $3::uuid))
                ORDER BY COALESCE(created_at, '0001-01-01 00:00:00+00'::timestamptz) DESC, id DESC
                LIMIT $4
                """,
                session_id,
                before_time,
                before_id,
                limit,
            )
        return [self._row_to_chunk(r) for r in rows]

    async def get_chunks_since(
        self,
        session_id: uuid.UUID,
        *,
        since_time=None,
        since_id: uuid.UUID | None = None,
        limit: int = 200,
    ) -> list[ContextChunk]:
        """Chunks strictly newer than the (time, id) consolidation cursor.

        Lets extraction process only new input; failed ranges retry because
        the cursor advances only after successful writes.
        """
        rows = await db.fetch(
            """
            SELECT id, session_id, agent_id, chunk_type, payload_msgpack, token_count, embedding, created_at, accessed_at, document_source
            FROM context_chunks
            WHERE session_id = $1
              AND ($3::uuid IS NULL OR (COALESCE(created_at, '0001-01-01 00:00:00+00'::timestamptz), id)
                   > (COALESCE($2::timestamptz, '0001-01-01 00:00:00+00'::timestamptz), $3::uuid))
            ORDER BY COALESCE(created_at, '0001-01-01 00:00:00+00'::timestamptz) ASC, id ASC
            LIMIT $4
            """,
            session_id,
            since_time,
            since_id,
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
            ORDER BY created_at DESC, id DESC
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
        """Mark a chunk as accessed (updates accessed_at for observability;
        eviction is FIFO by created_at, not LRU)."""
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
        expected_owner: str | None = None,
    ) -> int:
        """Replace only *original_ids* with *chunks* (AH-008).

        Unlike :meth:`replace_chunks` (delete-all), concurrent inserts with
        IDs outside *original_ids* survive. Originals are archived
        transactionally before deletion (AH-009); a failure rolls back
        archive+delete+insert together so originals remain recoverable.

        AH-AUDIT-014: when *expected_owner* is given, the live mutation
        token is validated INSIDE the same transaction (owner + expiry on
        the DB clock) — a preflight check alone is not a fence. Ownership
        loss raises StaleOwnershipError with full rollback.
        AH-AUDIT-015: the locked original set must equal the requested
        input set. Missing rows abort via StaleSnapshotError instead of
        inserting duplicate/conflicting output for a stale snapshot.
        Retried committed operations are idempotent at the caller level:
        a retry with already-replaced IDs aborts rather than duplicating.
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
                if expected_owner:
                    # Transactional fence (AH-AUDIT-014): owner, kind, and
                    # expiry validated here, on the DB clock, atomically
                    # with the destructive writes below.
                    from ah.core.exceptions import StaleOwnershipError

                    claim = await conn.fetchrow(
                        "SELECT claim_owner, claim_kind, claim_expires_at "
                        "FROM sessions WHERE id = $1 FOR UPDATE",
                        session_id,
                    )
                    live = (
                        claim is not None
                        and claim["claim_owner"] == expected_owner
                        and claim["claim_expires_at"] is not None
                    )
                    if live:
                        try:
                            live = claim["claim_expires_at"] > await conn.fetchval("SELECT now()")
                        except Exception:
                            live = False
                    if not live:
                        raise StaleOwnershipError(
                            "mutation ownership lost before replacement; aborted"
                        )
                if original_ids:
                    from ah.core.exceptions import StaleSnapshotError

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
                    # Exact input-set validation (AH-AUDIT-015): stale or
                    # partial snapshots abort instead of double-inserting.
                    locked = {r["id"] for r in orig_rows}
                    wanted = set(original_ids)
                    if locked != wanted:
                        raise StaleSnapshotError(
                            f"original set changed ({len(wanted) - len(locked)} missing); "
                            "stale replacement aborted"
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
        """Delete all chunks for a session.

        The latest-image cache is session-scoped too: without clearing it, a
        later turn would resurrect pre-wipe images. (Compaction paths keep
        the cache intentionally; only the explicit wipe clears it.)
        """
        result = await db.execute(
            "DELETE FROM context_chunks WHERE session_id = $1",
            session_id,
        )
        from ah.db.connection import parse_command_count

        count = parse_command_count(result)
        try:
            await db.execute(
                "DELETE FROM session_image_context WHERE session_id = $1",
                session_id,
            )
        except Exception as exc:
            if not _is_missing_image_relation(exc):
                raise
            logger.warning(
                "session_image_context missing while wiping session %s",
                session_id,
                exc_info=True,
            )
        _NO_LEGACY_IMAGES.difference_update(
            {key for key in _NO_LEGACY_IMAGES if key[0] == session_id}
        )
        return count

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
                -- AH-AUDIT-034: document lifecycle is separate from
                -- conversational eviction; indexed documents are never
                -- selected here (dedicated document operations only).
                WHERE session_id = $1 AND chunk_type <> 'document' AND (
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
