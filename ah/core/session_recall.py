"""Agent-scoped discovery of past conversation evidence."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import msgpack

from ah.core.text_search import build_or_tsquery
from ah.db.connection import db

RecallSource = Literal["live", "archive", "title"]


@dataclass(frozen=True)
class RecallHit:
    session_id: uuid.UUID
    chunk_id: uuid.UUID | None
    title: str | None
    source: RecallSource
    preview: str
    score: float
    occurred_at: datetime


@dataclass(frozen=True)
class RecallMessage:
    chunk_id: uuid.UUID
    session_id: uuid.UUID
    chunk_type: str
    source: Literal["live", "archive"]
    payload: dict[str, Any]
    occurred_at: datetime


class SessionRecall:
    """Search session titles and transcript chunks without modifying archive state."""

    async def discover(self, agent_id: str, query: str, limit: int = 20) -> list[RecallHit]:
        if not agent_id.strip():
            raise ValueError("agent_id is required")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        query = query.strip()
        if not query:
            return []
        if len(query) > 200:
            raise ValueError("query must be at most 200 characters")
        tsquery = build_or_tsquery(query)
        if not tsquery:
            return []

        rows = await db.fetch(
            """
            WITH search_query AS (
                SELECT to_tsquery('english', $2) AS terms
            ), matches AS (
                SELECT s.id AS session_id, c.id AS chunk_id, s.title,
                       'live'::text AS source, LEFT(c.search_text, 240) AS preview,
                       c.created_at AS occurred_at,
                       ts_rank(to_tsvector('english', c.search_text), q.terms) AS score,
                       0 AS source_priority
                FROM context_chunks c
                JOIN sessions s ON s.id = c.session_id
                CROSS JOIN search_query q
                WHERE s.agent_id = $1
                  AND to_tsvector('english', c.search_text) @@ q.terms
                UNION ALL
                SELECT s.id, a.chunk_id, s.title,
                       'archive'::text, LEFT(a.search_text, 240),
                       COALESCE(a.original_created_at, a.archived_at),
                       ts_rank(to_tsvector('english', COALESCE(a.search_text, '')), q.terms),
                       1
                FROM context_archive a
                JOIN sessions s ON s.id = a.session_id
                CROSS JOIN search_query q
                WHERE s.agent_id = $1
                  AND to_tsvector('english', COALESCE(a.search_text, '')) @@ q.terms
                UNION ALL
                SELECT s.id, NULL::uuid, s.title,
                       'title'::text, LEFT(s.title, 240), s.last_activity,
                       ts_rank(to_tsvector('english', COALESCE(s.title, '')), q.terms) + 1,
                       2
                FROM sessions s
                CROSS JOIN search_query q
                WHERE s.agent_id = $1
                  AND to_tsvector('english', COALESCE(s.title, '')) @@ q.terms
            ), deduplicated AS (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY session_id, chunk_id ORDER BY source_priority
                ) AS row_number
                FROM matches
            )
            SELECT session_id, chunk_id, title, source, preview, occurred_at, score
            FROM deduplicated
            WHERE row_number = 1
            ORDER BY score DESC, occurred_at DESC NULLS LAST, session_id, chunk_id NULLS LAST
            LIMIT $3
            """,
            agent_id,
            tsquery,
            limit,
        )
        return [
            RecallHit(
                session_id=row["session_id"],
                chunk_id=row["chunk_id"],
                title=row["title"],
                source=row["source"],
                preview=row["preview"] or "",
                score=float(row["score"]),
                occurred_at=row["occurred_at"],
            )
            for row in rows
        ]

    async def window(
        self,
        agent_id: str,
        session_id: uuid.UUID,
        chunk_id: uuid.UUID,
        *,
        before: int = 5,
        after: int = 5,
    ) -> list[RecallMessage]:
        """Read a bounded chronological window around a chunk, including archives."""
        if not agent_id.strip():
            raise ValueError("agent_id is required")
        if not 0 <= before <= 20 or not 0 <= after <= 20:
            raise ValueError("before and after must be between 0 and 20")
        rows = await db.fetch(
            """
            WITH owned_session AS (
                SELECT id FROM sessions WHERE id = $2 AND agent_id = $1
            ), all_chunks AS (
                SELECT c.id AS chunk_id, c.session_id, c.chunk_type,
                       c.payload_msgpack, c.created_at AS occurred_at,
                       'live'::text AS source, 0 AS source_priority
                FROM context_chunks c
                JOIN owned_session s ON s.id = c.session_id
                UNION ALL
                SELECT a.chunk_id, a.session_id, a.chunk_type,
                       a.payload_msgpack,
                       COALESCE(a.original_created_at, a.archived_at),
                       'archive'::text, 1
                FROM context_archive a
                JOIN owned_session s ON s.id = a.session_id
            ), deduplicated AS (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY chunk_id ORDER BY source_priority
                ) AS source_order
                FROM all_chunks
            ), ordered AS (
                SELECT chunk_id, session_id, chunk_type, payload_msgpack,
                       occurred_at, source,
                       ROW_NUMBER() OVER (ORDER BY occurred_at, chunk_id) AS ordinal
                FROM deduplicated WHERE source_order = 1
            ), anchor AS (
                SELECT ordinal FROM ordered WHERE chunk_id = $3
            )
            SELECT o.chunk_id, o.session_id, o.chunk_type, o.payload_msgpack,
                   o.occurred_at, o.source
            FROM ordered o CROSS JOIN anchor a
            WHERE o.ordinal BETWEEN a.ordinal - $4 AND a.ordinal + $5
            ORDER BY o.ordinal
            """,
            agent_id,
            session_id,
            chunk_id,
            before,
            after,
        )
        return [
            RecallMessage(
                chunk_id=row["chunk_id"],
                session_id=row["session_id"],
                chunk_type=row["chunk_type"],
                source=row["source"],
                payload=msgpack.unpackb(row["payload_msgpack"], raw=False),
                occurred_at=row["occurred_at"],
            )
            for row in rows
        ]
