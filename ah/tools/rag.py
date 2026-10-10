"""RAG tools — index_document and search_documents."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from ah.core.exceptions import ToolError, ValidationError
from ah.core.provider import audit_log
from ah.rag.pipeline import RAGPipeline
from ah.tools.agents import active_agent_scope
from ah.tools.base import registry

logger = logging.getLogger(__name__)

# Global RAG pipeline instance (lazy-initialized)
_rag_pipeline: RAGPipeline | None = None
_rag_pipeline_lock = asyncio.Lock()
# Whether the shared instance was built with Cohere credentials present.
# A mismatch (rotation/appearance/disappearance) rebuilds the pipeline so
# the selected reranker always matches configuration (AH-AUDIT-036).
_rag_pipeline_cohere: bool | None = None


def _cohere_configured() -> bool:
    try:
        from ah.core.config import config as _cfg

        return bool(_cfg.get("cohere_api_key"))
    except Exception:
        return False


def _build_shared_pipeline() -> RAGPipeline:
    """Choose the configured reranker (AH-AUDIT-036).

    Cohere is selected only when a key is present AND its client builds;
    otherwise the pipeline honestly runs passthrough. Explicit disabled
    mode (enable_reranking=False) is honored by callers via config.
    """
    reranker = None
    if _cohere_configured():
        try:
            from ah.rag.reranker import CohereReranker

            reranker = CohereReranker()
        except Exception as e:
            logger.warning("Cohere reranker unavailable, passthrough: %s", e)
            reranker = None
    if reranker is None:
        from ah.rag.reranker import IdentityReranker

        reranker = IdentityReranker()
    return RAGPipeline(reranker=reranker)


async def get_rag_pipeline() -> RAGPipeline:
    """Get or create the global RAG pipeline instance."""
    global _rag_pipeline, _rag_pipeline_cohere
    current = _cohere_configured()
    if _rag_pipeline is None or _rag_pipeline_cohere != current:
        async with _rag_pipeline_lock:
            # Double-check after acquiring lock
            if _rag_pipeline is None or _rag_pipeline_cohere != current:
                _rag_pipeline = _build_shared_pipeline()
                _rag_pipeline_cohere = current
    return _rag_pipeline


def reset_rag_pipeline_for_tests() -> None:
    """Drop the shared instance (tests only)."""
    global _rag_pipeline, _rag_pipeline_cohere
    _rag_pipeline = None
    _rag_pipeline_cohere = None


@registry.register(
    name="index_document",
    description="Index a document into the RAG pipeline for semantic search. Supports .txt, .md, .py, .js, .ts, .json, .yaml, .csv, .html, and more.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file to index"},
            "session_id": {
                "type": "string",
                "description": "Session ID to associate the document with",
            },
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
        raise ValidationError(f"Invalid session_id '{session_id}'") from None

    agent_id, owned_session = await active_agent_scope()
    if sid != owned_session:
        raise ToolError("document session does not belong to the active agent session")

    # AH-019: resolve the owning agent and pass it explicitly so documents
    # are not mislabeled with the pipeline default ("harness").
    pipeline = await get_rag_pipeline()

    try:
        chunks = await pipeline.index_document(
            source=path,
            session_id=sid,
            agent_id=agent_id,
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
        raise ToolError(f"Error indexing document: {e}") from e


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
        raise ValidationError(f"Invalid session_id '{session_id}'") from None

    _, owned_session = await active_agent_scope()
    if sid != owned_session:
        raise ToolError("document session does not belong to the active agent session")

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
        logger.exception("Failed to search documents")
        audit_log(
            "rag_tool_search_documents_error",
            session_id=session_id,
            query=query[:100],
            error=str(e),
        )
        raise ToolError(f"Error searching documents: {e}") from e


registry.declare_effects(
    {
        "index_document": ("fs.read",),
        "search_documents": ("rag.read",),
    }
)
