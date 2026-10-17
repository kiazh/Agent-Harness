"""Hybrid search — PostgreSQL full-text ranking + dense vector + RRF fusion."""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Any

import asyncpg
import msgpack

from machina.core.models import ContextChunk
from machina.core.provider import audit_log
from machina.core.serialization import embedding_to_str, str_to_embedding
from machina.core.text_search import build_or_tsquery

logger = logging.getLogger(__name__)


@dataclass
class SearchResult:
    """A search result with chunk and relevance information."""

    chunk: ContextChunk
    score: float
    dense_score: float = 0.0
    sparse_score: float = 0.0
    rrf_score: float = 0.0
    rerank_score: float | None = None
    retrieval_mode: str = "unknown"
    rerank_status: str = "not-applied"
    retrieval_degraded: bool = False


#: Document-only retrieval scope (AH-AUDIT-033). Document search returns
#: only document chunks; conversation/tool chunks use dedicated recall APIs
#: (or an explicitly requested combined scope), never crowd out documents.
DOCUMENT_TYPES: tuple[str, ...] = ("document",)


class SearchUnavailableError(RuntimeError):
    """Both retrieval branches failed; an empty answer would be a lie.

    Raised instead of returning ``[]`` so callers (the search tool, the
    evaluation harness) can render a retrieval outage distinctly from a
    genuinely empty index (R4-088(d)). Inherits ``RuntimeError`` and stays
    defined here so the rag package's core-import ratchet is untouched.
    """


class HybridSearch:
    """Hybrid search combining PostgreSQL full-text ranking and dense vector search.

    Architecture:
        Query → Dense retrieval (top 50)  ──┐
                                         ├──▶ RRF Fusion ──▶ Results
              → Keyword retrieval (top 50)  ──┘

    RRF formula: RRF(d) = Σ w_i / (k + rank_i(d))
    Default k=60 (standard for large corpora); w_i are the per-branch
    weights (``dense_weight`` / ``keyword_weight``, config-driven) that let
    an unreliable branch contribute without dominating the fusion.
    """

    DEFAULT_TOP_K = 50
    DEFAULT_RRF_K = 60
    DEFAULT_FINAL_TOP_K = 10

    def __init__(
        self,
        rrf_k: int = DEFAULT_RRF_K,
        top_k: int = DEFAULT_TOP_K,
        final_top_k: int = DEFAULT_FINAL_TOP_K,
        dense_weight: float = 1.0,
        keyword_weight: float = 1.0,
    ) -> None:
        self._rrf_k = rrf_k
        self._top_k = top_k
        self._final_top_k = final_top_k
        self._dense_weight = dense_weight
        self._keyword_weight = keyword_weight

    async def search(
        self,
        session_id: uuid.UUID,
        query_embedding: list[float],
        query_text: str,
        db: Any,
        top_k: int | None = None,
        chunk_types: tuple[str, ...] | None = None,
        concurrent: bool = True,
    ) -> list[SearchResult]:
        """Perform hybrid search: PostgreSQL FTS + dense vector + RRF fusion.

        Runs both retrievals concurrently for lower latency. When *db* is a
        single acquired connection (the pipeline's REPEATABLE READ snapshot),
        ``concurrent=False`` runs them sequentially: asyncpg permits one
        operation at a time per connection. Document-only scope by default
        (AH-AUDIT-033); pass explicit chunk_types for a documented combined
        scope.
        """
        k = top_k or self._final_top_k
        scope = tuple(chunk_types) if chunk_types else DOCUMENT_TYPES
        # Pipeline already passes k*2 for reranking headroom; do not multiply
        # again here (would over-retrieve k*4).
        retrieval_k = max(self._top_k, k)

        # Run dense and sparse retrieval (concurrently when the caller owns a
        # pool/direct executor; serially on one snapshot connection).
        dense_task = self._dense_search(session_id, query_embedding, retrieval_k, db, scope)
        sparse_task = self._bm25_search(session_id, query_text, retrieval_k, db, scope)

        if concurrent:
            dense_results, sparse_results = await asyncio.gather(
                dense_task, sparse_task, return_exceptions=True
            )
        else:
            try:
                dense_results = await dense_task
            except Exception as e:  # surfaced through the shared branch policy
                dense_results = e
            try:
                sparse_results = await sparse_task
            except Exception as e:  # surfaced through the shared branch policy
                sparse_results = e

        # Handle exceptions with intent: a single surviving branch keeps its
        # evidence (marked degraded); when BOTH branches fail the answer is
        # unknown, not empty — raising a typed error keeps a retrieval
        # outage distinguishable from a genuinely empty index (R4-088(d)).
        dense_failed = isinstance(dense_results, Exception)
        sparse_failed = isinstance(sparse_results, Exception)
        if dense_failed and sparse_failed:
            logger.error(
                "Both retrieval branches failed: dense=%s, keyword=%s",
                type(dense_results).__name__,
                type(sparse_results).__name__,
            )
            audit_log(
                "rag_search_unavailable",
                session_id=str(session_id),
                query_text=query_text[:100],
                dense_error=type(dense_results).__name__,
                sparse_error=type(sparse_results).__name__,
            )
            raise SearchUnavailableError(
                "both retrieval branches failed "
                f"(dense: {type(dense_results).__name__}, "
                f"keyword: {type(sparse_results).__name__}); "
                "results are unavailable — this is not an empty index"
            )
        degraded = False
        if dense_failed:
            logger.error("Dense search failed: %s", dense_results)
            dense_results = []
            degraded = True
        if sparse_failed:
            logger.error("PostgreSQL FTS search failed: %s", sparse_results)
            sparse_results = []
            degraded = True
        if degraded:
            logger.warning("Hybrid search degraded: one retrieval branch failed")

        # Fuse with RRF
        fused = self._rrf_fuse(dense_results, sparse_results)

        # Sort by RRF score and return top-k
        fused.sort(key=lambda r: r.rrf_score, reverse=True)
        for result in fused:
            result.retrieval_degraded = degraded

        audit_log(
            "rag_search_complete",
            session_id=str(session_id),
            query_text=query_text[:100],
            dense_results=len(dense_results),
            sparse_results=len(sparse_results),
            fused_results=len(fused),
            top_k=k,
            degraded=degraded,
        )

        return fused[:k]

    async def search_dense(
        self,
        session_id: uuid.UUID,
        query_embedding: list[float],
        db: Any,
        top_k: int | None = None,
        chunk_types: tuple[str, ...] | None = None,
    ) -> list[SearchResult]:
        """Return dense vector matches without the keyword search or RRF."""
        matches = await self._dense_search(
            session_id,
            query_embedding,
            top_k or self._final_top_k,
            db,
            tuple(chunk_types) if chunk_types else DOCUMENT_TYPES,
        )
        return [
            SearchResult(chunk=chunk, score=score, dense_score=score) for chunk, score in matches
        ]

    async def search_text(
        self,
        session_id: uuid.UUID,
        query_text: str,
        db: Any,
        top_k: int | None = None,
        chunk_types: tuple[str, ...] | None = None,
    ) -> list[SearchResult]:
        """Keyword-only matches for embedder-less operation (LP-10 fallback).

        No paid provider key needed; PostgreSQL FTS over stored search_text.
        Document-only scope by default, matching dense/hybrid paths.
        """
        matches = await self._bm25_search(
            session_id,
            query_text,
            top_k or self._final_top_k,
            db,
            tuple(chunk_types) if chunk_types else DOCUMENT_TYPES,
        )
        return [
            SearchResult(chunk=chunk, score=score, sparse_score=score) for chunk, score in matches
        ]

    async def _dense_search(
        self,
        session_id: uuid.UUID,
        query_embedding: list[float],
        top_k: int,
        db: Any,
        chunk_types: tuple[str, ...] = DOCUMENT_TYPES,
    ) -> list[tuple[ContextChunk, float]]:
        """Dense vector search using pgvector cosine similarity."""
        # Build the pgvector literal with the shared helper so the format
        # matches how embeddings are stored (embedding_to_str).
        embedding_str = embedding_to_str(query_embedding)
        rows = await db.fetch(
            """
            SELECT id, session_id, agent_id, chunk_type, payload_msgpack,
                   token_count, created_at, accessed_at,
                   1 - (embedding <=> $1::vector) AS similarity
            FROM context_chunks
            WHERE session_id = $2 AND embedding IS NOT NULL
              AND chunk_type = ANY($4::text[])
            ORDER BY embedding <=> $1::vector
            LIMIT $3
            """,
            embedding_str,
            session_id,
            top_k,
            list(chunk_types),
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
        chunk_types: tuple[str, ...] = DOCUMENT_TYPES,
    ) -> list[tuple[ContextChunk, float]]:
        """PostgreSQL FTS search using PostgreSQL full-text search (tsvector + GIN).

        Schema failures propagate so callers can report unavailable retrieval.
        Document-only scope by default (AH-AUDIT-033).
        """
        # Cap query terms via shared helper (max 16 OR terms) to bound FTS cost.
        tsquery = build_or_tsquery(query_text)
        if not tsquery:
            return []
        rows = await db.fetch(
            """
            SELECT id, session_id, agent_id, chunk_type, payload_msgpack,
                   token_count, embedding, created_at, accessed_at,
                   ts_rank(to_tsvector('english', search_text), to_tsquery('english', $2)) AS rank
            FROM context_chunks
            WHERE session_id = $1
              AND search_text IS NOT NULL
              AND chunk_type = ANY($4::text[])
              AND to_tsvector('english', search_text) @@ to_tsquery('english', $2)
            ORDER BY rank DESC
            LIMIT $3
            """,
            session_id,
            tsquery,
            top_k,
            list(chunk_types),
        )

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
        """Fuse dense and sparse results using weighted Reciprocal Rank Fusion."""
        return rrf_fuse(
            dense_results,
            sparse_results,
            rrf_k=self._rrf_k,
            dense_weight=self._dense_weight,
            keyword_weight=self._keyword_weight,
        )

    def _row_to_chunk(self, row: asyncpg.Record) -> ContextChunk:
        """Convert a database row to a ContextChunk.

        Tolerates projections without the embedding column (dense search
        selects similarity, not the vector itself).
        """
        payload = msgpack.unpackb(row["payload_msgpack"], raw=False)
        embedding = None
        try:
            raw_emb = row["embedding"]
        except (KeyError, IndexError):
            raw_emb = None
        if raw_emb is not None:
            try:
                embedding = str_to_embedding(raw_emb)
            except (ValueError, TypeError):
                embedding = None

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


def rrf_fuse(
    dense_results: list[tuple[ContextChunk, float]],
    sparse_results: list[tuple[ContextChunk, float]],
    rrf_k: int = 60,
    *,
    dense_weight: float = 1.0,
    keyword_weight: float = 1.0,
) -> list[SearchResult]:
    """Weighted Reciprocal Rank Fusion over (chunk, score) result lists.

    RRF(d) = Σ w_i / (k + rank_i(d)) with k=60 by default; rank_i is the
    1-based rank in result list i, and w_i is that branch's weight
    (``dense_weight`` / ``keyword_weight``). Weights let a weak branch add
    candidate recall without dragging the fused ranking below the strong
    branch; defaults are neutral (1.0). Shared by hybrid search and
    benchmarks so fusion semantics stay identical wherever rankings combine.
    """
    chunk_scores: dict[uuid.UUID, SearchResult] = {}

    # Process dense results
    for rank, (chunk, score) in enumerate(dense_results, start=1):
        rrf_score = dense_weight / (rrf_k + rank)
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
        rrf_score = keyword_weight / (rrf_k + rank)
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
