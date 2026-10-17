"""RAG pipeline — ingestion, retrieval, and generation orchestration."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import logging
import math
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from typing import Any

import msgpack

from machina.core.models import ContextChunk
from machina.core.provider import audit_log
from machina.core.provider.audit import redact_url_credentials
from machina.core.serialization import (
    embedding_to_str,
    payload_to_msgpack,
    row_to_chunk,
)
from machina.db.connection import db
from machina.memory.authority import is_catalog_or_echo
from machina.memory.redaction import redact_secrets
from machina.observability.diagnostics import record_failure
from machina.observability.instrumentation import instrumented, log_event
from machina.rag.chunker import Chunk, RecursiveCharacterTextSplitter, normalize_language
from machina.rag.embedder import Embedder, OpenAIEmbedder, build_embedder
from machina.rag.loaders import Document, FileLoader
from machina.rag.reranker import IdentityReranker, Reranker
from machina.rag.search import DOCUMENT_TYPES, HybridSearch, SearchResult

logger = logging.getLogger(__name__)

#: Fail-closed marker: text that could not be redacted is stored as this
#: marker instead of the raw value (never persist a possibly-secret string
#: when the detector itself fails).
REDACTION_FAILED = "[REDACTION_FAILED]"


def _redact_or_failed(text: object) -> str:
    """Redact *text*, or fail closed with a marker.

    A redactor exception must never fall back to the raw input — that would
    silently persist secrets exactly when suppression is broken. The failed
    value becomes :data:`REDACTION_FAILED` and the failure is recorded.
    """
    try:
        return redact_secrets(str(text)).text
    except Exception as e:
        record_failure("pipeline._redact_or_failed", e)
        return REDACTION_FAILED


def _redact_source(source: object) -> str:
    """Redact a document source for storage and audit emission (R5-Nb).

    ``_redact_or_failed`` covers token-shaped and DB-URL secrets via the
    shared ``redact_secrets``, but it is blind to URI userinfo passwords
    (``scheme://user:password@``) and to credential query/fragment values of
    any length (``?api_key=short1``). The shared
    :func:`~machina.core.provider.audit.redact_url_credentials` closes that
    remainder, so source metadata persisted in ``context_chunks`` and emitted
    to the audit log carries zero credentials. One authoritative
    implementation is reused by every source path — never the raw regexes.
    Fail closed: any exception from either stage yields
    :data:`REDACTION_FAILED`, never the raw source.
    """
    try:
        return redact_url_credentials(_redact_or_failed(source))
    except Exception as e:
        record_failure("pipeline._redact_source", e)
        return REDACTION_FAILED


def _redact_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Apply structured redaction to metadata values (AH-AUDIT-012).

    Provenance identifiers (source/chunk_index/embedding_*) are set by the
    pipeline itself after this runs, so they stay intact. Secret-bearing
    values — including nested dicts/lists — are scrubbed before storage.
    Keys are preserved as-is to keep index management stable. Fail closed:
    a value the redactor cannot process is replaced by the marker, never
    stored raw.
    """
    if not metadata:
        return {}
    from machina.memory.redaction import _default_redactor

    redacted: dict[str, Any] = {}
    for key, value in metadata.items():
        try:
            cleaned, _ = _default_redactor._redact_value(value)
            redacted[str(key)] = cleaned
        except Exception as e:
            record_failure("pipeline._redact_metadata", e)
            redacted[str(key)] = REDACTION_FAILED
    return redacted


@dataclass
class RAGConfig:
    """Configuration for the RAG pipeline."""

    chunk_size: int = 512
    chunk_overlap: int = 76
    top_k: int = 10
    retrieval_top_k: int = 50
    rrf_k: int = 60
    # Per-branch RRF fusion weights (measured on LoCoMo; neutral = 1.0).
    # ``rrf_keyword_weight`` scales the PostgreSQL FTS branch,
    # ``rrf_dense_weight`` scales the dense branch, so a weak dense model
    # cannot drag the fused ranking below keyword-only quality.
    rrf_keyword_weight: float = 1.0
    rrf_dense_weight: float = 1.0
    enable_embeddings: bool = True
    enable_reranking: bool = True
    enable_hybrid_search: bool = True
    # Admission bound for one indexed document (MCH-008). Enforced by the
    # loader before and during the descriptor read; an oversized input is
    # refused as a whole, never partially ingested.
    max_document_bytes: int = 8 * 1024 * 1024
    # PDF parsing budgets: extraction cost is page-proportional and the
    # extracted text is joined into one document before chunking.
    max_pdf_pages: int = 200
    max_pdf_text_bytes: int = 8 * 1024 * 1024


def _default_rag_config() -> RAGConfig:
    """RAGConfig with process-level tuning (RRF weights) read from config.

    Explicit ``RAGConfig(...)`` constructions stay deterministic; only the
    default path inherits the measured process-wide fusion weights.
    """
    config = RAGConfig()
    try:
        from machina.core.config import config as _cfg

        config.rrf_keyword_weight = float(_cfg.get("rrf_keyword_weight"))
        config.rrf_dense_weight = float(_cfg.get("rrf_dense_weight"))
    except Exception as e:
        logger.debug("RRF weights unavailable, defaults kept: %s", e)
    return config


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
    # Embedding-space verification (AH-AUDIT-035) is cached per
    # (session, model, scope) with a bounded TTL. The TTL is the accepted
    # cross-instance staleness policy for *ordinary* verdict reuse: a writer
    # in another process cannot invalidate this instance's dict, so a cached
    # verdict may live at most this long. Un-cached verification always runs
    # in the same REPEATABLE READ transaction as the dense query, so the
    # check and its use observe one snapshot (MCH-009/F-28).
    _embedding_space_cache_ttl: float = 300.0
    _embedding_space_cache_max_size: int = 256

    def __init__(
        self,
        embedder: Embedder | None = None,
        chunker: RecursiveCharacterTextSplitter | None = None,
        reranker: Reranker | None = None,
        search: HybridSearch | None = None,
        loader: FileLoader | None = None,
        config: RAGConfig | None = None,
    ) -> None:
        self._config = config or _default_rag_config()
        self._cache_generation = 0
        self._search_cache: dict[tuple, tuple[float, list[SearchResult]]] = {}
        self._embedding_space_cache: dict[tuple, tuple[float, str]] = {}
        # AH-AUDIT-032: keyword-capable pipeline with no embedder. Dense
        # clients initialize lazily: absent keys yield an explicit
        # keyword-only pipeline (NULL embeddings, honest mode reporting),
        # never a construction crash before the keyword fallback.
        if not self._config.enable_embeddings:
            self._embedder: Embedder | None = None
        elif embedder is not None:
            self._embedder = embedder
        else:
            # Config-driven selection: OpenAI-compatible endpoint, local
            # model2vec runtime, or None for honest keyword-only operation.
            # build_embedder never raises; it logs the degradation.
            self._embedder = build_embedder()
        self._chunker = chunker or RecursiveCharacterTextSplitter(
            chunk_size=self._config.chunk_size,
            chunk_overlap=self._config.chunk_overlap,
        )
        self._reranker = reranker or IdentityReranker()
        self._search = search or HybridSearch(
            rrf_k=self._config.rrf_k,
            top_k=self._config.retrieval_top_k,
            final_top_k=self._config.top_k,
            dense_weight=self._config.rrf_dense_weight,
            keyword_weight=self._config.rrf_keyword_weight,
        )
        self._loader = loader or FileLoader(
            max_document_bytes=self._config.max_document_bytes,
            max_pdf_pages=self._config.max_pdf_pages,
            max_pdf_text_bytes=self._config.max_pdf_text_bytes,
        )

    @property
    def config(self) -> RAGConfig:
        return self._config

    def reranker_identity(self) -> dict[str, str]:
        """Actual reranker identity for honest status (AH-AUDIT-036).

        Derived from the instantiated component's ``backend`` marker, never
        from key presence alone. A present credential is not evidence the
        component is selected or functioning; a passthrough exposes the
        reason it fell back.
        """
        try:
            reranker = self._reranker
            name = type(reranker).__name__
            backend = getattr(reranker, "backend", "") or (
                "cohere" if "Cohere" in name else "passthrough"
            )
            if backend == "cohere":
                return {"reranker": "cohere", "detail": getattr(reranker, "_model", "")}
            if backend == "passthrough":
                detail = getattr(reranker, "reason", "") or name
                return {"reranker": "passthrough", "detail": detail}
            if backend == "unknown":
                return {"reranker": "unknown", "detail": name}
            return {"reranker": backend, "detail": getattr(reranker, "model_name", "")}
        except Exception:
            return {"reranker": "unknown", "detail": ""}

    @instrumented("rag", "index_document")
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
        # Load document. Filesystem reads/parsing are blocking; run them off
        # the event loop so cancellation/lease callbacks stay responsive
        # (MCH-008). The loader itself enforces byte/page budgets.
        if isinstance(source, Document):
            doc = source
        else:
            doc = await asyncio.to_thread(self._loader.load, source)

        audit_log(
            "rag_index_start",
            session_id=str(session_id),
            # R5-Nb (finding 18 remainder): the same shared source redactor
            # used for storage, so neither the log line nor the persisted
            # audit row carries a userinfo password or credential query value.
            source=_redact_source(doc.source),
            doc_type=doc.doc_type,
        )

        # Redact the COMPLETE document before splitting (R03): a multiline
        # secret (e.g. a PEM block) whose delimiters land in different
        # chunks is invisible to per-chunk redaction, so its body would
        # reach the embedder and storage. Whole-document redaction sees
        # both delimiters; the per-chunk pass below stays as additional
        # protection. The in-memory copy is redacted, the caller's Document
        # is left untouched.
        doc = replace(doc, content=_redact_or_failed(doc.content))

        # Chunk
        chunks = self._chunk_document(doc, metadata)

        # Embed in batch (keyword-only when no embedder: NULL embeddings,
        # honest mode reporting — AH-AUDIT-032). Never fabricate zero-vector
        # embeddings for unavailable providers. Redaction is fail-closed
        # (W8): raw text is never stored when the detector fails.
        texts = [_redact_or_failed(c.text) for c in chunks]
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

        # Store in database (batch). Re-indexing the same document source
        # replaces the previous generation atomically (FM5 W13): delete by
        # (session_id, document_source) and insert in ONE transaction, so a
        # double index keeps a single copy and a failed re-index never
        # leaves a half-deleted document.
        stored_chunks: list[ContextChunk] = []
        records = []
        doc_source = _redact_source(doc.source)
        for chunk, embedding in zip(chunks, embeddings, strict=True):
            # AH-AUDIT-012/035: caller metadata applies FIRST so reserved
            # embedding/source fields cannot be overwritten by malicious or
            # mistaken keys; all outward-facing metadata values pass
            # structured redaction while provenance identifiers stay intact.
            chunk_meta = {
                **_redact_metadata(metadata),
                **_redact_metadata(chunk.metadata),
                "source": doc_source,
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
                "source": doc_source,
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
                    doc_source,
                )
            )

        insert_sql = """
            INSERT INTO context_chunks
                (session_id, agent_id, chunk_type, payload_msgpack, token_count, embedding, search_text, document_source)
            SELECT t.session_id, t.agent_id, t.chunk_type, t.payload_msgpack, t.token_count, t.embedding, t.search_text, t.document_source
            FROM unnest($1::uuid[], $2::text[], $3::text[], $4::bytea[], $5::int[], $6::text[]::vector[], $7::text[], $8::text[])
                AS t(session_id, agent_id, chunk_type, payload_msgpack, token_count, embedding, search_text, document_source)
            RETURNING id, session_id, agent_id, chunk_type, payload_msgpack, token_count, embedding, created_at, accessed_at, document_source
        """
        insert_args = (
            [r[0] for r in records],  # session_id array
            [r[1] for r in records],  # agent_id array
            [r[2] for r in records],  # chunk_type array
            [r[3] for r in records],  # payload_msgpack array
            [r[4] for r in records],  # token_count array
            [r[5] for r in records],  # embedding array
            [r[6] for r in records],  # search_text array
            [r[7] for r in records],  # document_source array
        )
        delete_sql = "DELETE FROM context_chunks WHERE session_id = $1 AND document_source = $2"

        async def _replace_and_insert(executor: Any) -> Any:
            # Serialize replacements of the same (session, source) across
            # connections/processes. Inside the surrounding transaction each
            # competing transaction cannot observe the others' uncommitted
            # INSERTs, so un-serialized DELETE+INSERT runs interleave and every
            # caller commits its own copy of the document. The transaction-
            # scoped advisory lock is released on commit/rollback. An EMPTY
            # replacement (R4-088) deletes the previous generation and inserts
            # nothing — never an early return that leaves stale chunks behind.
            await executor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                f"rag_document_replace:{session_id}:{doc_source}",
            )
            await executor.execute(delete_sql, session_id, doc_source)
            if not records:
                return []
            return await executor.fetch(insert_sql, *insert_args)

        rows: Any = []
        attempted = False
        acquire = getattr(db, "acquire", None)
        if callable(acquire):
            try:
                cm = acquire()
            except (TypeError, AttributeError):
                cm = None
            if cm is not None and not hasattr(cm, "__aenter__"):
                # AsyncMock doubles return a bare coroutine: close it and
                # use the non-transactional compatibility path.
                close = getattr(cm, "close", None)
                if callable(close):
                    close()
                cm = None
            if cm is not None:
                async with cm as conn:
                    attempted = True
                    transaction = getattr(conn, "transaction", None)
                    if callable(transaction):
                        async with transaction():
                            rows = await _replace_and_insert(conn)
                    else:
                        rows = await _replace_and_insert(conn)
        if not attempted:
            # Test doubles without an acquire/transaction protocol.
            if records:
                rows = await db.fetch(insert_sql, *insert_args)
            else:
                await db.execute(delete_sql, session_id, doc_source)
        stored_chunks = [self._row_to_chunk(r) for r in rows]
        log_event(
            "rag.index_document.summary",
            session_id=str(session_id),
            agent_id=str(agent_id),
            doc_type=doc.doc_type,
            chunks=len(chunks),
            stored=len(stored_chunks),
            embedded=sum(1 for embedding in embeddings if embedding),
            embedder=str(embed_model_name),
        )

        audit_log(
            "rag_index_complete",
            session_id=str(session_id),
            # R5-Nb: same shared source redactor as storage and rag_index_start.
            source=_redact_source(doc.source),
            chunks_stored=len(stored_chunks),
        )

        self._invalidate_search_cache(session_id)
        return stored_chunks

    @instrumented("rag", "search")
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
        if not query.strip():
            return []
        scope = tuple(chunk_types) if chunk_types else DOCUMENT_TYPES
        k = self._config.top_k if top_k is None else top_k
        if type(k) is not int or k < 1:
            raise ValueError("top_k must be a positive integer")

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
        generation = self._cache_generation
        now = time.monotonic()
        if cache_key in self._search_cache:
            cached_time, cached_results = self._search_cache[cache_key]
            if now - cached_time < self._search_cache_ttl:
                logger.debug("RAG search cache hit for session %s", session_id)
                log_event(
                    "rag.search.cache_hit",
                    level="debug",
                    session_id=str(session_id),
                    top_k=k,
                    returned=len(cached_results[:k]),
                )
                return copy.deepcopy(cached_results[:k])
            else:
                # Expired
                del self._search_cache[cache_key]

        # Embed query, or fall back to keyword-only search when no embedding
        # provider is available (LP-10: basic operation needs no second paid
        # key). The fallback mode is recorded for honest status reporting.
        query_embedding = None
        embedding_failed = False
        if self._embedder is None:
            logger.warning("no embedder configured, keyword-only search")
        else:
            try:
                query_embedding = await self._embedder.embed(query)
            except Exception as e:
                embedding_failed = True
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
            for result in results:
                result.retrieval_degraded = embedding_failed
            return await self._finish_search(
                results,
                query,
                session_id,
                k,
                rerank,
                cache_key,
                now,
                embed_model,
                "keyword-only",
                generation,
            )
        # Embedding-space identity (AH-AUDIT-035): dimension AND model must
        # match the stored dense space. Different same-dimension models
        # compare incorrectly — fail explicitly with migration guidance
        # instead of returning corrupt similarity. Mixed keyword-only rows
        # (NULL embeddings) are unaffected.
        try:
            probe = embedding_to_str(query_embedding)
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

        if self._embedding_space_verdict_cached(session_id, str(embed_model), scope):
            # Verdict is fresh; a single statement needs no snapshot wrapper,
            # but still uses one acquired connection when the executor is a
            # pool (a bare test double may expose only ``fetch``).
            async with self._snapshot_connection(require_transaction=False) as (
                executor,
                concurrent,
            ):
                results = await self._dense_retrieval(
                    executor, concurrent, session_id, query_embedding, query, k, scope
                )
        else:
            # One REPEATABLE READ transaction/connection for verification AND
            # candidate retrieval (MCH-009/F-28): a concurrent cross-process
            # reindex cannot swap the embedding space between the two.
            async with self._snapshot_connection() as (executor, concurrent):
                await self._enforce_embedding_space(
                    session_id, str(embed_model), scope, executor=executor
                )
                results = await self._dense_retrieval(
                    executor, concurrent, session_id, query_embedding, query, k, scope
                )
        return await self._finish_search(
            results,
            query,
            session_id,
            k,
            rerank,
            cache_key,
            now,
            embed_model,
            "hybrid" if self._config.enable_hybrid_search else "dense",
            generation,
        )

    async def _dense_retrieval(
        self,
        executor: Any,
        concurrent: bool,
        session_id: uuid.UUID,
        query_embedding: list[float],
        query: str,
        k: int,
        scope: tuple[str, ...],
    ) -> list[SearchResult]:
        """Run the configured dense retrieval branches on *executor*.

        ``concurrent`` is False whenever *executor* is a single acquired
        connection: asyncpg permits only one operation at a time per
        connection, and the snapshot transaction forbids a second one.
        """
        if self._config.enable_hybrid_search:
            return await self._search.search(
                session_id=session_id,
                query_embedding=query_embedding,
                query_text=query,
                db=executor,
                top_k=k * 2,  # Retrieve more for reranking
                chunk_types=scope,
                concurrent=concurrent,
            )
        return await self._search.search_dense(
            session_id=session_id,
            query_embedding=query_embedding,
            db=executor,
            top_k=k * 2,
            chunk_types=scope,
        )

    @asynccontextmanager
    async def _snapshot_connection(self, *, require_transaction: bool = True):
        """Yield ``(executor, concurrent)`` for verification + dense search.

        A real ``Database`` (or asyncpg Pool) yields one acquired connection
        inside ``REPEATABLE READ`` so the embedding-space verdict and the
        vector query observe the same snapshot. Doubles that expose no
        transaction protocol fall back to the existing single-connection or
        direct-double path; only a direct ``db`` executor may run both
        retrieval branches concurrently. With ``require_transaction=False``
        (a cached verdict) the connection is still acquired but no transaction
        is opened.
        """
        acquire = getattr(db, "acquire", None)
        cm = None
        # A ``Database`` that was never connected (and whose ``fetch`` a test
        # patched directly) has no pool; use the direct-double path instead.
        if getattr(db, "connected", None) is False:
            acquire = None
        if callable(acquire):
            try:
                cm = acquire()
            except (TypeError, AttributeError):
                cm = None
            if cm is not None and not hasattr(cm, "__aenter__"):
                close = getattr(cm, "close", None)
                if callable(close):
                    close()
                cm = None
        if cm is None:
            yield db, True
            return

        async with cm as conn:
            if not require_transaction:
                yield conn, False
                return
            transaction = getattr(conn, "transaction", None)
            txn = None
            if callable(transaction):
                try:
                    txn = transaction(isolation="repeatable_read")
                except TypeError:
                    txn = None
                if txn is not None and not hasattr(txn, "__aenter__"):
                    close = getattr(txn, "close", None)
                    if callable(close):
                        close()
                    txn = None
            if txn is None:
                yield conn, False
                return
            async with txn:
                yield conn, False

    async def _enforce_embedding_space(
        self,
        session_id: uuid.UUID,
        query_model: str,
        chunk_types: tuple[str, ...] = DOCUMENT_TYPES,
        *,
        executor: Any = None,
    ) -> None:
        """Reject cross-model dense comparison (AH-AUDIT-035).

        Compares the query embedding model against the distinct stored models
        for scoped chunks with non-NULL embeddings in this session, in ONE
        ``SELECT DISTINCT`` query (no Python keyset paging). A mismatch raises
        an explicit error (reindex/migrate); matching or keyword-only indexes
        proceed. Legacy rows without model metadata are treated as the default
        model only when dimensions agree. Database or metadata errors must not
        allow an unverified dense comparison.

        *executor* is the connection used for the pending dense query, so both
        statements can share one REPEATABLE READ snapshot. Verified verdicts
        are cached per ``(session, model, scope)`` with a bounded TTL;
        re-indexing invalidates this instance's entries. Cross-instance
        reindex is covered by the TTL (``_embedding_space_cache_ttl``), the
        documented accepted staleness policy, and a cache miss always
        re-verifies inside the query transaction.

        Note: the schema stores model identity inside ``payload_msgpack``,
        so the DISTINCT projection is the payload itself. A dedicated
        ``embedding_model``/``embedding_dims`` column with an index is the
        deployment-scale form of this query; that schema change is outside
        this module's lane.
        """
        cache_key = (session_id, query_model, tuple(chunk_types))
        generation = self._cache_generation
        if self._embedding_space_verdict_cached(session_id, query_model, chunk_types):
            return

        query_db = executor if executor is not None else db
        rows = await query_db.fetch(
            """SELECT DISTINCT payload_msgpack FROM context_chunks
               WHERE session_id = $1 AND chunk_type = ANY($2::text[])
                 AND embedding IS NOT NULL""",
            session_id,
            list(chunk_types),
        )
        stored: set[str] = set()
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
        # Verified: cache the verdict (bounded, TTL) so cold searches do not
        # rescan on every call (FM5 W15).
        if generation != self._cache_generation:
            return
        self._embedding_space_cache[cache_key] = (time.monotonic(), query_model)
        if len(self._embedding_space_cache) > self._embedding_space_cache_max_size:
            oldest_key = min(
                self._embedding_space_cache,
                key=lambda k: self._embedding_space_cache[k][0],
            )
            del self._embedding_space_cache[oldest_key]

    def _embedding_space_verdict_cached(
        self,
        session_id: uuid.UUID,
        query_model: str,
        chunk_types: tuple[str, ...] = DOCUMENT_TYPES,
    ) -> bool:
        """Whether a fresh (session, model, scope) verdict is cached."""
        cache_key = (session_id, query_model, tuple(chunk_types))
        cached = self._embedding_space_cache.get(cache_key)
        if cached is None:
            return False
        if time.monotonic() - cached[0] < self._embedding_space_cache_ttl:
            return True
        del self._embedding_space_cache[cache_key]
        return False

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
        retrieval_mode: str = "unknown",
        generation: int | None = None,
    ) -> list[SearchResult]:
        for result in results:
            result.retrieval_mode = retrieval_mode
        # Rerank
        if rerank and self._config.enable_reranking and len(results) > k:
            documents = [r.chunk.payload.get("text", "") for r in results]
            try:
                rerank_results = await self._reranker.rerank(query, documents, top_k=k)
                seen = set()
                for rr in rerank_results:
                    if (
                        type(rr.index) is not int
                        or not 0 <= rr.index < len(results)
                        or rr.index in seen
                        or (
                            rr.score is not None
                            and (type(rr.score) not in (int, float) or not math.isfinite(rr.score))
                        )
                    ):
                        raise ValueError("Invalid reranker result")
                    seen.add(rr.index)
                if not rerank_results:
                    raise ValueError("Empty reranker response")
                # None is an explicit passthrough/failure signal. Validate the
                # entire response before mutating any candidate or score.
                if all(rr.score is not None for rr in rerank_results):
                    reranked = []
                    for rr in rerank_results[:k]:
                        original = results[rr.index]
                        original.score = rr.score
                        original.rerank_score = rr.score
                        original.rerank_status = "applied"
                        reranked.append(original)
                    results = reranked
                else:
                    for result in results:
                        result.rerank_status = (
                            "passthrough"
                            if isinstance(self._reranker, IdentityReranker)
                            else "fallback"
                        )
            except Exception as e:
                for result in results:
                    result.rerank_status = "fallback"
                audit_log("rag_rerank_fallback", error=type(e).__name__)
                logger.warning("Invalid reranking; preserving retrieval ranking: %s", e)

        final_results = self._demote_catalog(results)[:k]

        # Own the full snapshot so callers cannot mutate cached result objects
        # or the nested chunk payloads through fresh results or cache hits.
        # Never cache empty results: a delete+reindex inside the TTL would
        # otherwise keep serving stale emptiness.
        if final_results and generation == self._cache_generation:
            self._search_cache[cache_key] = (now, copy.deepcopy(final_results))
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
        log_event(
            "rag.search.summary",
            session_id=str(session_id),
            top_k=k,
            candidates=len(results),
            returned=len(final_results),
            rerank=bool(rerank),
            retrieval_mode=retrieval_mode,
            embedder=str(embed_model),
            query_chars=len(query),
        )

        return final_results

    @staticmethod
    def _demote_catalog(results: list[SearchResult]) -> list[SearchResult]:
        """Stable-demote catalog/echo results after fusion/rerank (FM5 W4).

        RRF fusion is rank-only and the reranker is optional, so neither
        carries content authority. Catalog listings and prior-recall echoes
        are moved after real content while preserving relative order within
        each group; evidence is demoted, never dropped.
        """
        if len(results) <= 1:
            return results
        favored: list[SearchResult] = []
        demoted: list[SearchResult] = []
        for result in results:
            try:
                text = result.chunk.payload.get("text", "")
                chunk_type = result.chunk.chunk_type
            except Exception:
                text, chunk_type = "", None
            if is_catalog_or_echo(text, chunk_type):
                demoted.append(result)
            else:
                favored.append(result)
        return favored + demoted if demoted else results

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
            # Fail-closed redaction (W8): legacy rows are re-scrubbed before
            # they are embedded or indexed again; raw text is never stored.
            chunks_to_embed.append((row, _redact_or_failed(text), payload))

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
            # Fail-closed: legacy payload text is re-scrubbed too, so a raw
            # secret cannot survive in the stored payload (W8).
            if "text" in payload:
                payload["text"] = text
            if "content" in payload:
                payload["content"] = text
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
        self._cache_generation += 1
        for key in [k for k in self._search_cache if k[0] == session_id]:
            self._search_cache.pop(key, None)
        # Indexed content changed: drop cached embedding-space verdicts too.
        for key in [k for k in self._embedding_space_cache if k[0] == session_id]:
            self._embedding_space_cache.pop(key, None)

    def _chunk_document(
        self,
        doc: Document,
        metadata: dict[str, Any] | None = None,
    ) -> list[Chunk]:
        """Chunk a document based on its type."""
        if doc.doc_type == "markdown":
            return self._chunker.split_markdown(doc.content, metadata)
        elif doc.doc_type == "code":
            lang = normalize_language(doc.metadata.get("extension", ""))
            return self._chunker.split_code(doc.content, language=lang, metadata=metadata)
        else:
            return self._chunker.split_text(doc.content, metadata)

    def _row_to_chunk(self, row: Any) -> ContextChunk:
        """Convert a database row to a ContextChunk."""
        return row_to_chunk(row)
