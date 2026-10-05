"""Hybrid search — BM25 (PostgreSQL FTS) + dense vector + RRF fusion."""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Any

import asyncpg
import msgpack

from ah.core.models import ContextChunk
from ah.core.provider import audit_log
from ah.core.serialization import embedding_to_str

logger = logging.getLogger(__name__)


@dataclass
class SearchResult:
    """A search result with chunk and relevance information."""

    chunk: ContextChunk
    score: float
    dense_score: float = 0.0
    sparse_score: float = 0.0
    rrf_score: float = 0.0


class HybridSearch:
    """Hybrid search combining BM25 (PostgreSQL FTS) and dense vector search.

    Architecture:
        Query → Dense retrieval (top 50)  ──┐
                                         ├──▶ RRF Fusion ──▶ Results
              → BM25 retrieval (top 50)  ──┘

    RRF formula: RRF(d) = Σ 1 / (k + rank_i(d))
    Default k=60 (standard for large corpora).
    """

    DEFAULT_TOP_K = 50
    DEFAULT_RRF_K = 60
    DEFAULT_FINAL_TOP_K = 10

    def __init__(
        self,
        rrf_k: int = DEFAULT_RRF_K,
        top_k: int = DEFAULT_TOP_K,
        final_top_k: int = DEFAULT_FINAL_TOP_K,
    ) -> None:
        self._rrf_k = rrf_k
        self._top_k = top_k
        self._final_top_k = final_top_k

    async def search(
        self,
        session_id: uuid.UUID,
        query_embedding: list[float],
        query_text: str,
        db: Any,
        top_k: int | None = None,
    ) -> list[SearchResult]:
        """Perform hybrid search: BM25 + dense vector + RRF fusion.

        Runs both retrievals concurrently for lower latency.
        """
        k = top_k or self._final_top_k
        retrieval_k = max(self._top_k, k * 2)

        # Run dense and sparse retrieval concurrently
        dense_task = self._dense_search(session_id, query_embedding, retrieval_k, db)
        sparse_task = self._bm25_search(session_id, query_text, retrieval_k, db)

        dense_results, sparse_results = await asyncio.gather(
            dense_task, sparse_task, return_exceptions=True
        )

        # Handle exceptions gracefully
        if isinstance(dense_results, Exception):
            logger.error("Dense search failed: %s", dense_results)
            dense_results = []
        if isinstance(sparse_results, Exception):
            logger.error("BM25 search failed: %s", sparse_results)
            sparse_results = []

        # Fuse with RRF
        fused = self._rrf_fuse(dense_results, sparse_results)

        # Sort by RRF score and return top-k
        fused.sort(key=lambda r: r.rrf_score, reverse=True)

        audit_log(
            "rag_search_complete",
            session_id=str(session_id),
            query_text=query_text[:100],
            dense_results=len(dense_results),
            sparse_results=len(sparse_results),
            fused_results=len(fused),
            top_k=k,
        )

        return fused[:k]

    async def search_dense(
        self,
        session_id: uuid.UUID,
        query_embedding: list[float],
        db: Any,
        top_k: int | None = None,
    ) -> list[SearchResult]:
        """Return dense vector matches without the keyword search or RRF."""
        matches = await self._dense_search(
            session_id, query_embedding, top_k or self._final_top_k, db
        )
        return [
            SearchResult(chunk=chunk, score=score, dense_score=score) for chunk, score in matches
        ]

    async def _dense_search(
        self,
        session_id: uuid.UUID,
        query_embedding: list[float],
        top_k: int,
        db: Any,
    ) -> list[tuple[ContextChunk, float]]:
        """Dense vector search using pgvector cosine similarity."""
        # Build the pgvector literal with the shared helper so the format
        # matches how embeddings are stored (embedding_to_str).
        embedding_str = embedding_to_str(query_embedding)
        rows = await db.fetch(
            """
            SELECT id, session_id, agent_id, chunk_type, payload_msgpack,
                   token_count, embedding, created_at, accessed_at,
                   1 - (embedding <=> $1::vector) AS similarity
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
            chunk = self._row_to_chunk(row)
            sim = float(row["similarity"])
            results.append((chunk, sim))

        return results

    async def _bm25_search(
        self,
        session_id: uuid.UUID,
        query_text: str,
        top_k: int,
        db: Any,
    ) -> list[tuple[ContextChunk, float]]:
        """BM25 search using PostgreSQL full-text search (tsvector + GIN).

        Returns empty results if search_text column is not available.
        """
        # Try FTS first
        try:
            rows = await db.fetch(
                """
                SELECT id, session_id, agent_id, chunk_type, payload_msgpack,
                       token_count, embedding, created_at, accessed_at,
                       ts_rank(to_tsvector('english', search_text), plainto_tsquery('english', $2)) AS rank
                FROM context_chunks
                WHERE session_id = $1
                  AND search_text IS NOT NULL
                  AND to_tsvector('english', search_text) @@ plainto_tsquery('english', $2)
                ORDER BY rank DESC
                LIMIT $3
                """,
                session_id,
                query_text,
                top_k,
            )
        except asyncpg.UndefinedColumnError:
            # search_text column doesn't exist — no fallback possible
            # (casting bytea to text produces hex, not readable text)
            logger.warning(
                "search_text column not found, skipping BM25 search — returning empty results"
            )
            rows = []

        results = []
        for row in rows:
            chunk = self._row_to_chunk(row)
            rank = float(row["rank"])
            results.append((chunk, rank))

        return results

    def _rrf_fuse(
        self,
        dense_results: list[tuple[ContextChunk, float]],
        sparse_results: list[tuple[ContextChunk, float]],
    ) -> list[SearchResult]:
        """Fuse dense and sparse results using Reciprocal Rank Fusion.

        RRF(d) = Σ 1 / (k + rank_i(d))
        where k=60 (default), rank_i is the 1-based rank in result list i.
        """
        chunk_scores: dict[uuid.UUID, SearchResult] = {}

        # Process dense results
        for rank, (chunk, score) in enumerate(dense_results, start=1):
            rrf_score = 1.0 / (self._rrf_k + rank)
            if chunk.id in chunk_scores:
                chunk_scores[chunk.id].rrf_score += rrf_score
                chunk_scores[chunk.id].dense_score = score
            else:
                chunk_scores[chunk.id] = SearchResult(
                    chunk=chunk,
                    score=score,
                    dense_score=score,
                    rrf_score=rrf_score,
                )

        # Process sparse results
        for rank, (chunk, score) in enumerate(sparse_results, start=1):
            rrf_score = 1.0 / (self._rrf_k + rank)
            if chunk.id in chunk_scores:
                chunk_scores[chunk.id].rrf_score += rrf_score
                chunk_scores[chunk.id].sparse_score = score
            else:
                chunk_scores[chunk.id] = SearchResult(
                    chunk=chunk,
                    score=score,
                    sparse_score=score,
                    rrf_score=rrf_score,
                )

        # Update final score to RRF score
        for result in chunk_scores.values():
            result.score = result.rrf_score

        return list(chunk_scores.values())

    def _row_to_chunk(self, row: asyncpg.Record) -> ContextChunk:
        """Convert a database row to a ContextChunk."""
        payload = msgpack.unpackb(row["payload_msgpack"], raw=False)
        embedding = None
        if row["embedding"] is not None:
            embedding = [float(x) for x in str(row["embedding"]).strip("[]").split(",")]

        return ContextChunk(
            id=row["id"],
            session_id=row["session_id"],
            agent_id=row["agent_id"],
            chunk_type=row["chunk_type"],
            payload=payload,
            token_count=row["token_count"],
            embedding=embedding,
            created_at=row["created_at"],
            accessed_at=row["accessed_at"],
        )
