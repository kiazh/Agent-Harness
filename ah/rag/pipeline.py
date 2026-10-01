"""RAG pipeline — ingestion, retrieval, and generation orchestration."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

import msgpack

from ah.core.models import ContextChunk
from ah.core.provider import audit_log
from ah.db.connection import db
from ah.rag.chunker import Chunk, RecursiveCharacterTextSplitter
from ah.rag.embedder import Embedder, OpenAIEmbedder
from ah.rag.loaders import Document, FileLoader
from ah.rag.reranker import IdentityReranker, Reranker
from ah.rag.search import HybridSearch, SearchResult

logger = logging.getLogger(__name__)


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
        self._embedder = embedder or OpenAIEmbedder()
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

        # Embed in batch
        texts = [c.text for c in chunks]
        embeddings = await self._embedder.embed_batch(texts)

        # Store in database
        stored_chunks: list[ContextChunk] = []
        for chunk, embedding in zip(chunks, embeddings):
            chunk_meta = {
                **chunk.metadata,
                "source": doc.source,
                "doc_type": doc.doc_type,
                "chunk_index": chunk.index,
            }
            if metadata:
                chunk_meta.update(metadata)

            # Build search_text for FTS
            search_text = chunk.text

            # Store in context_chunks
            payload = {
                "text": chunk.text,
                "metadata": chunk_meta,
                "source": doc.source,
            }
            payload_msgpack = msgpack.packb(payload, use_bin_type=True)
            embedding_str = "[" + ",".join(str(x) for x in embedding) + "]"

            row = await db.fetchrow(
                """
                INSERT INTO context_chunks
                    (session_id, agent_id, chunk_type, payload_msgpack, token_count, embedding, search_text)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING id, session_id, agent_id, chunk_type, payload_msgpack, token_count, embedding, created_at, accessed_at
                """,
                session_id,
                agent_id,
                "document",
                payload_msgpack,
                chunk.token_count,
                embedding_str,
                search_text,
            )

            stored_chunks.append(self._row_to_chunk(row))

        audit_log(
            "rag_index_complete",
            session_id=str(session_id),
            source=doc.source,
            chunks_stored=len(stored_chunks),
        )

        return stored_chunks

    async def search(
        self,
        query: str,
        session_id: uuid.UUID,
        top_k: int | None = None,
        rerank: bool = True,
    ) -> list[SearchResult]:
        """Search the RAG index: query → retrieve → rerank → top-k.

        Args:
            query: Search query text.
            session_id: Session to search within.
            top_k: Number of results to return (default: config.top_k).
            rerank: Whether to apply cross-encoder reranking.

        Returns:
            List of SearchResult objects sorted by relevance.
        """
        k = top_k or self._config.top_k

        audit_log(
            "rag_search_start",
            session_id=str(session_id),
            query=query[:100],
            top_k=k,
        )

        # Embed query
        query_embedding = await self._embedder.embed(query)

        # Hybrid search (BM25 + dense + RRF)
        if self._config.enable_hybrid_search:
            results = await self._search.search(
                session_id=session_id,
                query_embedding=query_embedding,
                query_text=query,
                db=db,
                top_k=k * 2,  # Retrieve more for reranking
            )
        else:
            # Dense-only search
            results = await self._search.search(
                session_id=session_id,
                query_embedding=query_embedding,
                query_text=query,
                db=db,
                top_k=k * 2,
            )

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
    ) -> list[ContextChunk]:
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
            chunks_to_embed.append((row, text))

        # Embed in batch
        texts = [t for _, t in chunks_to_embed]
        embeddings = await self._embedder.embed_batch(texts)

        # Update rows with embeddings
        updated = []
        for (row, _), embedding in zip(chunks_to_embed, embeddings):
            embedding_str = "[" + ",".join(str(x) for x in embedding) + "]"
            await db.execute(
                """
                UPDATE context_chunks
                SET embedding = $1, search_text = $2
                WHERE id = $3
                """,
                embedding_str,
                row.get("search_text", ""),
                row["id"],
            )
            updated.append(row["id"])

        audit_log(
            "rag_index_context_complete",
            session_id=str(session_id),
            chunks_embedded=len(updated),
        )

        return updated

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
