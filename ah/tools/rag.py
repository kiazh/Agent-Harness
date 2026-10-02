"""RAG tools — index_document and search_documents."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from ah.core.provider import audit_log
from ah.rag.pipeline import RAGPipeline
from ah.tools.base import registry

logger = logging.getLogger(__name__)

# Global RAG pipeline instance (lazy-initialized)
_rag_pipeline: RAGPipeline | None = None
_rag_pipeline_lock = asyncio.Lock()


async def get_rag_pipeline() -> RAGPipeline:
    """Get or create the global RAG pipeline instance."""
    global _rag_pipeline
    if _rag_pipeline is None:
        async with _rag_pipeline_lock:
            # Double-check after acquiring lock
            if _rag_pipeline is None:
                _rag_pipeline = RAGPipeline()
    return _rag_pipeline


def set_rag_pipeline(pipeline: RAGPipeline) -> None:
    """Set the global RAG pipeline instance (for testing or custom config)."""
    global _rag_pipeline
    _rag_pipeline = pipeline


@registry.register(
    name="index_document",
    description="Index a document into the RAG pipeline for semantic search. Supports .txt, .md, .py, .js, .ts, .json, .yaml, .csv, .html, and more.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file to index"},
            "session_id": {"type": "string", "description": "Session ID to associate the document with"},
            "metadata": {"type": "object", "description": "Optional metadata to attach to chunks"},
        },
        "required": ["path", "session_id"],
    },
)
async def index_document(
    path: str,
    session_id: str,
    metadata: dict[str, Any] | None = None,
) -> str:
    """Index a document into the RAG pipeline.

    The document is loaded, chunked, embedded, and stored for semantic search.
    """
    try:
        sid = uuid.UUID(session_id)
    except ValueError:
        raise ValidationError(f"Invalid session_id '{session_id}'")

    pipeline = await get_rag_pipeline()

    try:
        chunks = await pipeline.index_document(
            source=path,
            session_id=sid,
            metadata=metadata,
        )
        audit_log(
            "rag_tool_index_document",
            session_id=session_id,
            path=path,
            chunks_indexed=len(chunks),
        )
        return f"Successfully indexed '{path}' — {len(chunks)} chunks created."
    except Exception as e:
        logger.exception("Failed to index document: %s", path)
        audit_log(
            "rag_tool_index_document_error",
            session_id=session_id,
            path=path,
            error=str(e),
        )
        raise ToolError(f"Error indexing document: {e}")


@registry.register(
    name="search_documents",
    description="Search indexed documents using hybrid search (BM25 + dense vector + RRF fusion). Returns relevant document chunks.",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "session_id": {"type": "string", "description": "Session ID to search within"},
            "top_k": {"type": "integer", "description": "Number of results to return (default: 5)"},
        },
        "required": ["query", "session_id"],
    },
)
async def search_documents(
    query: str,
    session_id: str,
    top_k: int = 5,
) -> str:
    """Search indexed documents using hybrid search.

    Combines BM25 (keyword) and dense vector (semantic) search with RRF fusion.
    """
    try:
        sid = uuid.UUID(session_id)
    except ValueError:
        raise ValidationError(f"Invalid session_id '{session_id}'")

    pipeline = await get_rag_pipeline()

    try:
        results = await pipeline.search(
            query=query,
            session_id=sid,
            top_k=top_k,
        )

        if not results:
            return f"No results found for '{query}'"

        lines = [f"Search results for '{query}' ({len(results)} results):"]
        for i, result in enumerate(results, 1):
            text = result.chunk.payload.get("text", "")
            source = result.chunk.payload.get("metadata", {}).get("source", "unknown")
            score = result.score
            preview = text[:200].replace("\n", " ")
            lines.append(f"\n{i}. [{score:.4f}] {source}")
            lines.append(f"   {preview}...")

        audit_log(
            "rag_tool_search_documents",
            session_id=session_id,
            query=query[:100],
            results_count=len(results),
        )

        return "\n".join(lines)
    except Exception as e:
        logger.exception("Failed to search documents: %s", query)
        audit_log(
            "rag_tool_search_documents_error",
            session_id=session_id,
            query=query[:100],
            error=str(e),
        )
        raise ToolError(f"Error searching documents: {e}")
