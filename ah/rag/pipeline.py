"""RAG pipeline — ingestion, retrieval, and generation orchestration."""

from __future__ import annotations

import copy
import hashlib
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

import msgpack

from ah.core.models import ContextChunk
from ah.core.provider import audit_log
from ah.core.serialization import (
    embedding_to_str,
    payload_to_msgpack,
    row_to_chunk,
)
from ah.db.connection import db
from ah.memory.redaction import redact_secrets
from ah.observability.diagnostics import record_failure
from ah.rag.chunker import Chunk, RecursiveCharacterTextSplitter
from ah.rag.embedder import Embedder, OpenAIEmbedder
from ah.rag.loaders import Document, FileLoader
from ah.rag.reranker import IdentityReranker, Reranker
from ah.rag.search import DOCUMENT_TYPES, HybridSearch, SearchResult

logger = logging.getLogger(__name__)


def _redact_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Apply structured redaction to metadata values (AH-AUDIT-012).

    Provenance identifiers (source/chunk_index/embedding_*) are set by the
    pipeline itself after this runs, so they stay intact. Secret-bearing
    values — including nested dicts/lists — are scrubbed before storage.
    Keys are preserved as-is to keep index management stable.
    """
    if not metadata:
        return {}
    redacted: dict[str, Any] = {}
    for key, value in metadata.items():
        try:
            if isinstance(value, str):
                redacted[str(key)] = redact_secrets(value).text
            elif isinstance(value, (list, tuple)):
                redacted[str(key)] = [
                    redact_secrets(v).text if isinstance(v, str) else v for v in value
                ]
            elif isinstance(value, dict):
                redacted[str(key)] = {
                    str(k): redact_secrets(v).text if isinstance(v, str) else v
                    for k, v in value.items()
                }
            else:
                redacted[str(key)] = value
        except Exception:
            redacted[str(key)] = value
    return redacted


@dataclass
class RAGConfig:
    """Configuration for the RAG pipeline."""

    chunk_size: int = 512
    chunk_overlap: int = 76
    top_k: int = 10
    retrieval_top_k: int = 50
    rrf_k: int = 60
    enable_reranking: bool = True
    enable_hybrid_search: bool = True


class RAGPipeline:
    """Production RAG pipeline: ingestion → retrieval → reranking → generation.

    Usage:
        pipeline = RAGPipeline()
        await pipeline.index_document("path/to/doc.md", session_id)
        results = await pipeline.search("query", session_id)
    """

    # Each pipeline owns its own search cache so different embedders and
    # retrieval settings cannot reuse one another's results.
    _search_cache_ttl: float = 300.0  # 5 minutes
    _search_cache_max_size: int = 256

    def __init__(
        self,
        embedder: Embedder | None = None,
        chunker: RecursiveCharacterTextSplitter | None = None,
        reranker: Reranker | None = None,
        search: HybridSearch | None = None,
        loader: FileLoader | None = None,
        config: RAGConfig | None = None,
    ) -> None:
        self._config = config or RAGConfig()
        self._search_cache: dict[tuple, tuple[float, list[SearchResult]]] = {}
        # AH-AUDIT-032: keyword-capable pipeline with no embedder. Dense
        # clients initialize lazily: absent keys yield an explicit
        # keyword-only pipeline (NULL embeddings, honest mode reporting),
        # never a construction crash before the keyword fallback.
        if embedder is not None:
            self._embedder: Embedder | None = embedder
        else:
            try:
                self._embedder = OpenAIEmbedder()
            except Exception as e:
                logger.warning("embedder unavailable, keyword-only pipeline: %s", e)
                self._embedder = None
        self._chunker = chunker or RecursiveCharacterTextSplitter(
            chunk_size=self._config.chunk_size,
            chunk_overlap=self._config.chunk_overlap,
        )
        self._reranker = reranker or IdentityReranker()
        self._search = search or HybridSearch(
            rrf_k=self._config.rrf_k,
            top_k=self._config.retrieval_top_k,
            final_top_k=self._config.top_k,
        )
        self._loader = loader or FileLoader()

    @property
    def config(self) -> RAGConfig:
        return self._config

    def reranker_identity(self) -> dict[str, str]:
        """Actual reranker identity for honest status (AH-AUDIT-036).

        Derived from the instantiated component, never from key presence
        alone. A present credential is not evidence the component is
        selected or functioning.
        """
        try:
            name = type(self._reranker).__name__
            if "Cohere" in name:
                return {"reranker": "cohere", "detail": getattr(self._reranker, "_model", "")}
            return {"reranker": "passthrough", "detail": name}
        except Exception:
            return {"reranker": "unknown", "detail": ""}

    async def index_document(
        self,
        source: str | Document,
        session_id: uuid.UUID,
        agent_id: str = "harness",
        metadata: dict[str, Any] | None = None,
    ) -> list[ContextChunk]:
        """Index a document: load → chunk → embed → store.

        Args:
            source: File path or Document object to index.
            session_id: Session to associate chunks with.
            agent_id: Agent identifier.
            metadata: Additional metadata to attach to chunks.

        Returns:
            List of stored ContextChunk objects.
        """
        # Load document
        if isinstance(source, Document):
            doc = source
        else:
            doc = self._loader.load(source)

        audit_log(
            "rag_index_start",
            session_id=str(session_id),
            source=doc.source,
            doc_type=doc.doc_type,
        )

        # Chunk
        chunks = self._chunk_document(doc, metadata)

        # Embed in batch (keyword-only when no embedder: NULL embeddings,
        # honest mode reporting — AH-AUDIT-032). Never fabricate zero-vector
        # embeddings for unavailable providers.
        texts = [redact_secrets(c.text).text for c in chunks]
        # Keep redacted text for storage so secrets never reach the DB.
        for chunk, redacted_text in zip(chunks, texts, strict=True):
            chunk.text = redacted_text
        if self._embedder is None:
            embeddings: list = [None] * len(texts)
            embed_model_name = "keyword-only"
        else:
            try:
                embeddings = await self._embedder.embed_batch(texts)
            except Exception as e:
                logger.warning("batch embedding failed, storing keyword-only rows: %s", e)
                embeddings = [None] * len(texts)
                embed_model_name = "keyword-only"
            else:
                embed_model_name = getattr(
                    self._embedder,
                    "model_name",
                    getattr(self._embedder, "_model", "unknown"),
                )

        # Store in database (batch)
        stored_chunks: list[ContextChunk] = []
        records = []
        for chunk, embedding in zip(chunks, embeddings, strict=True):
            # AH-AUDIT-012/035: caller metadata applies FIRST so reserved
            # embedding/source fields cannot be overwritten by malicious or
            # mistaken keys; all outward-facing metadata values pass
            # structured redaction while provenance identifiers stay intact.
            chunk_meta = {
                **_redact_metadata(metadata),
                **_redact_metadata(chunk.metadata),
                "source": redact_secrets(str(doc.source)).text,
                "doc_type": doc.doc_type,
                "chunk_index": chunk.index,
                "embedding_model": str(embed_model_name),
                "embedding_dims": len(embedding) if embedding else 0,
            }

            search_text = chunk.text
            payload = {
                "text": chunk.text,
                "metadata": chunk_meta,
                # AH-AUDIT-012: stored payload source follows the same
                # redaction policy as chunk text and metadata.
                "source": redact_secrets(str(doc.source)).text,
            }
            payload_msgpack = payload_to_msgpack(payload)
            embedding_str = embedding_to_str(embedding) if embedding else None
            records.append(
                (
                    session_id,
                    agent_id,
                    "document",
                    payload_msgpack,
                    chunk.token_count,
                    embedding_str,
                    search_text,
                )
            )

        if not records:
            return []

        # Batch insert all rows in a single query for better performance.
        # Use unnest to insert multiple rows at once with RETURNING.
        rows = await db.fetch(
            """
            INSERT INTO context_chunks
                (session_id, agent_id, chunk_type, payload_msgpack, token_count, embedding, search_text)
            SELECT t.session_id, t.agent_id, t.chunk_type, t.payload_msgpack, t.token_count, t.embedding, t.search_text
            FROM unnest($1::uuid[], $2::text[], $3::text[], $4::bytea[], $5::int[], $6::text[]::vector[], $7::text[])
                AS t(session_id, agent_id, chunk_type, payload_msgpack, token_count, embedding, search_text)
            RETURNING id, session_id, agent_id, chunk_type, payload_msgpack, token_count, embedding, created_at, accessed_at
            """,
            [r[0] for r in records],  # session_id array
            [r[1] for r in records],  # agent_id array
            [r[2] for r in records],  # chunk_type array
            [r[3] for r in records],  # payload_msgpack array
            [r[4] for r in records],  # token_count array
            [r[5] for r in records],  # embedding array
            [r[6] for r in records],  # search_text array
        )
        stored_chunks = [self._row_to_chunk(r) for r in rows]

        audit_log(
            "rag_index_complete",
            session_id=str(session_id),
            source=doc.source,
            chunks_stored=len(stored_chunks),
        )

        self._invalidate_search_cache(session_id)
        return stored_chunks

    async def search(
        self,
        query: str,
        session_id: uuid.UUID,
        top_k: int | None = None,
        rerank: bool = True,
        chunk_types: tuple[str, ...] | None = None,
    ) -> list[SearchResult]:
        """Search the RAG index: query → retrieve → rerank → top-k.

        Args:
            query: Search query text.
            session_id: Session to search within.
            top_k: Number of results to return (default: config.top_k).
            rerank: Whether to apply cross-encoder reranking.
            chunk_types: Retrieval scope (document-only by default,
                AH-AUDIT-033). Pass an explicit tuple for a documented
                combined scope.

        Returns:
            List of SearchResult objects sorted by relevance.
        """
        scope = tuple(chunk_types) if chunk_types else DOCUMENT_TYPES
        k = top_k or self._config.top_k

        audit_log(
            "rag_search_start",
            session_id=str(session_id),
            query=query[:100],
            top_k=k,
        )

        # Check TTL cache (namespaced by embedder model + retrieval flags).
        if self._embedder is None:
            embed_model = "keyword-only"
        else:
            embed_model = getattr(
                self._embedder, "model_name", getattr(self._embedder, "_model", "")
            )
        cache_key = (
            session_id,
            hashlib.sha256(query.encode()).hexdigest(),
            k,
            rerank,
            str(embed_model),
            bool(self._config.enable_reranking),
            bool(self._config.enable_hybrid_search),
            scope,
        )
        now = time.monotonic()
        if cache_key in self._search_cache:
            cached_time, cached_results = self._search_cache[cache_key]
            if now - cached_time < self._search_cache_ttl:
                logger.debug("RAG search cache hit for session %s", session_id)
                return copy.deepcopy(cached_results[:k])
            else:
                # Expired
                del self._search_cache[cache_key]

        # Embed query, or fall back to keyword-only search when no embedding
        # provider is available (LP-10: basic operation needs no second paid
        # key). The fallback mode is recorded for honest status reporting.
        self._last_search_mode = "hybrid"
        query_embedding = None
        if self._embedder is None:
            logger.warning("no embedder configured, keyword-only search")
        else:
            try:
                query_embedding = await self._embedder.embed(query)
            except Exception as e:
                logger.warning("query embedding unavailable, keyword-only fallback: %s", e)
                query_embedding = None
        if query_embedding is None:
            results = await self._search.search_text(
                session_id=session_id,
                query_text=query,
                db=db,
                top_k=k * 2,
                chunk_types=scope,
            )
            self._last_search_mode = "keyword-only"
            return await self._finish_search(
                results, query, session_id, k, rerank, cache_key, now, embed_model
            )
        # Embedding-space identity (AH-AUDIT-035): dimension AND model must
        # match the stored dense space. Different same-dimension models
        # compare incorrectly — fail explicitly with migration guidance
        # instead of returning corrupt similarity. Mixed keyword-only rows
        # (NULL embeddings) are unaffected.
        try:
            from ah.core.serialization import embedding_to_str as _e2s

            probe = _e2s(query_embedding)
            dims = len(probe.split(",")) if probe else 0
            if dims and dims != 1536:
                raise ValueError(
                    f"embedding dimension mismatch: got {dims}, stored vectors are 1536. "
                    "Reindex after switching embedding models."
                )
        except ValueError:
            raise
        except Exception as _boundary_error:
            # Optional fallback preserves the primary outcome; report no payload.
            record_failure("pipeline.search", _boundary_error)
        await self._enforce_embedding_space(session_id, str(embed_model), scope)

        # Hybrid search (BM25 + dense + RRF)
        if self._config.enable_hybrid_search:
            results = await self._search.search(
                session_id=session_id,
                query_embedding=query_embedding,
                query_text=query,
                db=db,
                top_k=k * 2,  # Retrieve more for reranking
                chunk_types=scope,
            )
        else:
            # Dense-only search
            results = await self._search.search_dense(
                session_id=session_id,
                query_embedding=query_embedding,
                db=db,
                top_k=k * 2,
                chunk_types=scope,
            )
        return await self._finish_search(
            results, query, session_id, k, rerank, cache_key, now, embed_model
        )

    async def _enforce_embedding_space(
        self,
        session_id: uuid.UUID,
        query_model: str,
        chunk_types: tuple[str, ...] = DOCUMENT_TYPES,
    ) -> None:
        """Reject cross-model dense comparison (AH-AUDIT-035).

        Compares the query embedding model against distinct stored models
        for scoped chunks with non-NULL embeddings in this session. A
        mismatch raises an explicit error (reindex/migrate); matching or
        keyword-only indexes proceed. Legacy rows without model metadata
        are treated as the default model only when dimensions agree.
        Inspect all candidate rows in bounded pages: sampling payloads can
        miss a second model, and database or metadata errors must not allow
        an unverified dense comparison.
        """
        page_size = 256
        cursor: uuid.UUID | None = None
        stored: set[str] = set()
        while True:
            rows = await db.fetch(
                """SELECT id, payload_msgpack FROM context_chunks
                   WHERE session_id = $1 AND chunk_type = ANY($2::text[])
                     AND embedding IS NOT NULL
                     AND ($3::uuid IS NULL OR id > $3)
                   ORDER BY id LIMIT $4""",
                session_id,
                list(chunk_types),
                cursor,
                page_size,
            )
            for row in rows:
                try:
                    payload = msgpack.unpackb(row["payload_msgpack"], raw=False)
                    if not isinstance(payload, dict):
                        raise ValueError("embedding payload must be a dict")
                    metadata = payload.get("metadata")
                    if metadata is None:
                        metadata = {}
                    if not isinstance(metadata, dict):
                        raise ValueError("embedding metadata must be a dict")
                    model = metadata.get("embedding_model", "")
                    if model is None:
                        model = ""
                    if not isinstance(model, str):
                        raise ValueError("embedding model must be a string")
                    stored.add(model or OpenAIEmbedder.DEFAULT_MODEL)
                except Exception as e:
                    raise ValueError(
                        "Cannot verify stored embedding model metadata. "
                        "Reindex (or migrate/backfill) before dense retrieval."
                    ) from e
            if stored - {query_model}:
                raise ValueError(
                    f"embedding space mismatch: query model {query_model!r} vs stored "
                    f"{sorted(stored)!r}. Reindex (or migrate/backfill) so candidate "
                    "and query embeddings share one enforced space."
                )
            if len(rows) < page_size:
                return
            cursor = rows[-1]["id"]

    async def _finish_search(
        self,
        results: list[SearchResult],
        query: str,
        session_id: uuid.UUID,
        k: int,
        rerank: bool,
        cache_key: tuple,
        now: float,
        embed_model: str,
    ) -> list[SearchResult]:
        # Rerank
        if rerank and self._config.enable_reranking and len(results) > k:
            documents = [r.chunk.payload.get("text", "") for r in results]
            rerank_results = await self._reranker.rerank(query, documents, top_k=k)

            # Map reranked results back to SearchResult objects
            reranked: list[SearchResult] = []
            for rr in rerank_results:
                if rr.index < len(results):
                    original = results[rr.index]
                    original.score = rr.score
                    reranked.append(original)
            results = reranked

        final_results = results[:k]

        # Own the full snapshot so callers cannot mutate cached result objects
        # or the nested chunk payloads through fresh results or cache hits.
        # Never cache empty results: a delete+reindex inside the TTL would
        # otherwise keep serving stale emptiness.
        if final_results:
            self._search_cache[cache_key] = (now, copy.deepcopy(final_results))
            # Record embedding model/dims namespace for incompat detection.
            try:
                self._last_embed_model = str(embed_model)
            except Exception as _boundary_error:
                # Optional fallback preserves the primary outcome; report no payload.
                record_failure("pipeline._finish_search", _boundary_error)
            # Evict oldest if cache is full (simple approach)
            if len(self._search_cache) > self._search_cache_max_size:
                oldest_key = min(self._search_cache, key=lambda k: self._search_cache[k][0])
                del self._search_cache[oldest_key]

        audit_log(
            "rag_search_complete",
            session_id=str(session_id),
            query=query[:100],
            results_returned=len(final_results),
        )

        return final_results

    async def index_session_context(
        self,
        session_id: uuid.UUID,
        agent_id: str = "harness",
    ) -> list[uuid.UUID]:
        """Index all existing context chunks for a session that don't have embeddings.

        Useful for bootstrapping RAG on existing sessions.
        """
        rows = await db.fetch(
            """
            SELECT id, session_id, agent_id, chunk_type, payload_msgpack, token_count, created_at, accessed_at
            FROM context_chunks
            WHERE session_id = $1 AND embedding IS NULL
            """,
            session_id,
        )

        if not rows:
            return []

        chunks_to_embed = []
        for row in rows:
            payload = msgpack.unpackb(row["payload_msgpack"], raw=False)
            text = payload.get("text", payload.get("content", str(payload)))
            chunks_to_embed.append((row, str(text), payload))

        # Embed in batch (keyword-only rows keep NULL embeddings when the
        # embedder is unavailable — AH-AUDIT-032; search_text still indexed).
        texts = [text for _, text, _ in chunks_to_embed]
        if self._embedder is None:
            embeddings = [None] * len(texts)
        else:
            try:
                embeddings = await self._embedder.embed_batch(texts)
            except Exception as e:
                logger.warning("batch embedding failed, keyword-only backfill: %s", e)
                embeddings = [None] * len(texts)

        embed_model_name = (
            getattr(self._embedder, "model_name", getattr(self._embedder, "_model", "unknown"))
            if self._embedder is not None
            else "keyword-only"
        )
        # Persist each vector and its identity in the same row update.
        updated = []
        for (row, text, payload), embedding in zip(chunks_to_embed, embeddings, strict=True):
            payload["metadata"] = {
                **(payload.get("metadata") or {}),
                "embedding_model": str(embed_model_name) if embedding else "keyword-only",
                "embedding_dims": len(embedding) if embedding else 0,
            }
            updated.append(
                (
                    embedding_to_str(embedding) if embedding else None,
                    text,
                    payload_to_msgpack(payload),
                    row["id"],
                )
            )

        await db.executemany(
            """
            UPDATE context_chunks
            SET embedding = $1, search_text = $2, payload_msgpack = $3
            WHERE id = $4
            """,
            updated,
        )

        # Indexed content changed — drop cached search results for this session.
        self._invalidate_search_cache(session_id)

        audit_log(
            "rag_index_context_complete",
            session_id=str(session_id),
            chunks_embedded=len(updated),
        )

        return [row["id"] for row, _, _ in chunks_to_embed]

    def _invalidate_search_cache(self, session_id: uuid.UUID) -> None:
        """Remove cached search results for *session_id* (after re-indexing)."""
        for key in [k for k in self._search_cache if k[0] == session_id]:
            self._search_cache.pop(key, None)

    async def delete_session_context(self, session_id: uuid.UUID) -> int:
        """Delete indexed chunks for a session and invalidate cached searches."""
        result = await db.execute(
            "DELETE FROM context_chunks WHERE session_id = $1",
            session_id,
        )
        self._invalidate_search_cache(session_id)
        try:
            from ah.db.connection import parse_command_count

            return parse_command_count(result)
        except Exception:
            return 0

    def _chunk_document(
        self,
        doc: Document,
        metadata: dict[str, Any] | None = None,
    ) -> list[Chunk]:
        """Chunk a document based on its type."""
        if doc.doc_type == "markdown":
            return self._chunker.split_markdown(doc.content, metadata)
        elif doc.doc_type == "code":
            lang = doc.metadata.get("extension", "").lstrip(".")
            return self._chunker.split_code(doc.content, language=lang, metadata=metadata)
        else:
            return self._chunker.split_text(doc.content, metadata)

    def _row_to_chunk(self, row: Any) -> ContextChunk:
        """Convert a database row to a ContextChunk."""
        return row_to_chunk(row)
