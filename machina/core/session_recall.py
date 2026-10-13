"""Agent-scoped discovery of past conversation evidence."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import msgpack

from machina.core.text_search import build_or_tsquery, normalize_for_hash
from machina.db.connection import db

__all__ = ["RecallHit", "RecallMessage", "SessionRecall"]

RecallSource = Literal["live", "archive", "title"]


@dataclass(frozen=True)
class RecallHit:
    session_id: uuid.UUID
    chunk_id: uuid.UUID | None
    title: str | None
    source: RecallSource
    preview: str
    score: float
    occurred_at: datetime | None


@dataclass(frozen=True)
class RecallMessage:
    chunk_id: uuid.UUID
    session_id: uuid.UUID
    chunk_type: str
    source: Literal["live", "archive"]
    payload: dict[str, Any]
    occurred_at: datetime | None


def _is_catalog_or_echo(preview: str, chunk_type: str | None) -> bool:
    """Detect repeated catalog listings and recall-echo chunks (precision filter).

    Catalogs (skill pages, agent listings) and prior recall outputs echoed
    back into context repeat query terms and dominate ts_rank. They are
    demoted, never dropped, so evidence is preserved but real instructions
    rank first. Conservative: ordinary conversation never matches.
    """
    p = preview or ""
    pl = p.lower()
    if "session=" in pl and "chunk=" in pl:
        return True
    if p.startswith("Skill ") and "[" in p and "]:" in p:
        return True
    if p.startswith("ECC resource"):
        return True
    if "available agents" in pl or "no agents are available" in pl:
        return True
    if (chunk_type or "") == "tool_call" and (
        "skill_list" in pl or "skill_read" in pl or "session_recall" in pl
    ):
        return True
    return False


class SessionRecall:
    """Search session titles and transcript chunks without modifying archive state."""

    async def discover(
        self,
        agent_id: str,
        query: str,
        limit: int = 20,
        *,
        session_id: uuid.UUID | None = None,
        chunk_types: list[str] | tuple[str, ...] | set[str] | None = None,
        since: datetime | None = None,
    ) -> list[RecallHit]:
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
        wanted_types = set(chunk_types) if chunk_types is not None else None
        # Bounded over-fetch: SQL ranks, Python suppresses/dedups/re-ranks,
        # then trims to limit. Previews stay 240 chars (sufficient, bounded).
        fetch_limit = min(100, limit * 4 + 10)

        rows = await db.fetch(
            """
            WITH search_query AS (
                SELECT to_tsquery('english', $2) AS terms
            ), matches AS (
                SELECT s.id AS session_id, c.id AS chunk_id, s.title,
                       'live'::text AS source, LEFT(c.search_text, 240) AS preview,
                       c.created_at AS occurred_at,
                       ts_rank(to_tsvector('english', c.search_text), q.terms) AS score,
                       0 AS source_priority, c.chunk_type AS chunk_type
                FROM context_chunks c
                JOIN sessions s ON s.id = c.session_id
                CROSS JOIN search_query q
                WHERE s.agent_id = $1
                  AND ($4::uuid IS NULL OR s.id = $4)
                  AND ($5::text[] IS NULL OR c.chunk_type = ANY($5))
                  AND ($6::timestamptz IS NULL OR c.created_at >= $6)
                  AND to_tsvector('english', c.search_text) @@ q.terms
                UNION ALL
                SELECT s.id, a.chunk_id, s.title,
                       'archive'::text, LEFT(a.search_text, 240),
                       COALESCE(a.original_created_at, a.archived_at),
                       ts_rank(to_tsvector('english', COALESCE(a.search_text, '')), q.terms),
                       1, a.chunk_type
                FROM context_archive a
                JOIN sessions s ON s.id = a.session_id
                CROSS JOIN search_query q
                WHERE s.agent_id = $1
                  AND ($4::uuid IS NULL OR s.id = $4)
                  AND ($5::text[] IS NULL OR a.chunk_type = ANY($5))
                  AND ($6::timestamptz IS NULL OR COALESCE(a.original_created_at, a.archived_at) >= $6)
                  AND to_tsvector('english', COALESCE(a.search_text, '')) @@ q.terms
                UNION ALL
                SELECT s.id, NULL::uuid, s.title,
                       'title'::text, LEFT(s.title, 240), s.last_activity,
                       ts_rank(to_tsvector('english', COALESCE(s.title, '')), q.terms) + 1,
                       2, NULL::text
                FROM sessions s
                CROSS JOIN search_query q
                WHERE s.agent_id = $1
                  AND ($4::uuid IS NULL OR s.id = $4)
                  AND ($5::text[] IS NULL)
                  AND ($6::timestamptz IS NULL OR s.last_activity >= $6)
                  AND to_tsvector('english', COALESCE(s.title, '')) @@ q.terms
            ), deduplicated AS (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY session_id, chunk_id ORDER BY source_priority
                ) AS row_number
                FROM matches
            )
            SELECT session_id, chunk_id, title, source, preview, occurred_at, score, chunk_type
            FROM deduplicated
            WHERE row_number = 1
            ORDER BY score DESC, occurred_at DESC NULLS LAST, session_id, chunk_id NULLS LAST
            LIMIT $3
            """,
            agent_id,
            tsquery,
            fetch_limit,
            session_id,
            list(wanted_types) if wanted_types is not None else None,
            since,
        )

        def _ctype(row: Any) -> str | None:
            try:
                if hasattr(row, "keys") and "chunk_type" in row.keys():
                    return row["chunk_type"]
            except Exception:
                pass
            try:
                return row.get("chunk_type")
            except Exception:
                return None

        # Python-side filters mirror SQL for mocked DBs and post-rank trims.
        filtered: list[Any] = []
        for row in rows:
            if session_id is not None and row["session_id"] != session_id:
                continue
            if wanted_types is not None and _ctype(row) not in wanted_types:
                continue
            if since is not None and row["occurred_at"] is not None:
                try:
                    if row["occurred_at"] < since:
                        continue
                except Exception:
                    pass
            filtered.append(row)
        # Content-hash suppression of repeated catalog/recall-echo chunks.
        seen_hashes: set[str] = set()
        deduped: list[Any] = []
        for row in filtered:
            preview = row["preview"] or ""
            norm = normalize_for_hash(preview)
            digest = hashlib.sha256(norm.encode("utf-8")).hexdigest()
            if digest in seen_hashes:
                continue
            seen_hashes.add(digest)
            deduped.append(row)

        def _rank_key(row: Any) -> tuple[int, int]:
            preview = row["preview"] or ""
            if _is_catalog_or_echo(preview, _ctype(row)):
                return (2, 0)
            if (_ctype(row) or "") in {"user_message", "user"}:
                return (0, 0)
            return (1, 0)

        ranked = [(_rank_key(r), i, r) for i, r in enumerate(deduped)]
        ranked.sort(key=lambda t: (t[0], t[1]))
        trimmed = [r for _, _, r in ranked[:limit]]
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
            for row in trimmed
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
