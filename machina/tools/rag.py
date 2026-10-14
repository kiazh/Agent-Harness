"""RAG tools — index_document and search_documents."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from typing import Any

from machina.core.exceptions import ToolError, ValidationError
from machina.core.provider import audit_log
from machina.rag.pipeline import RAGPipeline
from machina.tools.agents import active_agent_scope
from machina.tools.base import registry

logger = logging.getLogger(__name__)

# Global RAG pipeline instance (lazy-initialized)
_rag_pipeline: RAGPipeline | None = None
_rag_pipeline_lock = asyncio.Lock()
# IDENTITY (digest of the API key), not boolean presence, of the Cohere
# credential the shared instance was built with. Rotation to another nonempty
# key must rebuild the pipeline; comparing presence alone kept the old client
# (and its old key) serving all later requests (AH-AUDIT-036).
_rag_pipeline_identity: str | None = None


def _cohere_configured() -> bool:
    try:
        from machina.core.config import config as _cfg

        return bool(_cfg.get("cohere_api_key"))
    except Exception:
        return False


def _cohere_key_digest(key: str | None) -> str | None:
    """Opaque credential identity; None when no key is configured."""
    if not key:
        return None
    return hashlib.sha256(str(key).encode("utf-8")).hexdigest()


def _current_cohere_identity() -> str | None:
    """Identity of the Cohere credential currently configured (read fresh)."""
    try:
        from machina.core.config import config as _cfg

        return _cohere_key_digest(_cfg.get("cohere_api_key"))
    except Exception:
        return None


def _built_cohere_identity(pipeline: RAGPipeline | None) -> str | None:
    """Identity of the credential the pipeline's reranker was BUILT with."""
    reranker = getattr(pipeline, "_reranker", None)
    return _cohere_key_digest(getattr(reranker, "api_key", None))


def _build_shared_pipeline() -> RAGPipeline:
    """Choose the configured reranker (AH-AUDIT-036).

    Cohere is selected only when a key is present AND its client builds;
    otherwise the pipeline honestly runs passthrough. Explicit disabled
    mode (enable_reranking=False) is honored by callers via config.
    """
    reranker = None
    if _cohere_configured():
        try:
            from machina.rag.reranker import CohereReranker

            reranker = CohereReranker()
        except Exception as e:
            logger.warning("Cohere reranker unavailable, passthrough: %s", e, exc_info=True)
            reranker = None
    if reranker is None:
        from machina.rag.reranker import IdentityReranker

        reranker = IdentityReranker()
    return RAGPipeline(reranker=reranker)


async def get_rag_pipeline() -> RAGPipeline:
    """Get or create the global RAG pipeline instance.

    Rebuilds when the Cohere credential IDENTITY changes (rotation to a
    different nonempty key, appearance, or disappearance), so the reranker
    client always matches the configured key. Reuses when unchanged.
    """
    global _rag_pipeline, _rag_pipeline_identity
    current = _current_cohere_identity()
    if _rag_pipeline is None or _rag_pipeline_identity != current:
        async with _rag_pipeline_lock:
            # Double-check after acquiring lock
            if _rag_pipeline is None or _rag_pipeline_identity != current:
                _rag_pipeline = _build_shared_pipeline()
                # Record the identity the reranker actually bound to. When the
                # build fell back without binding a key, keep the configured
                # identity so a failing client is not rebuilt on every
                # request; a real rotation changes `current` and still
                # forces the rebuild.
                built = _built_cohere_identity(_rag_pipeline)
                _rag_pipeline_identity = built if built is not None else current
    return _rag_pipeline


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
    description="Search indexed documents using hybrid search (PostgreSQL full-text ranking + dense vector + RRF fusion). Returns relevant document chunks.",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "session_id": {
                "type": "string",
                "description": "Session ID to search within; omit (or pass 'current') for the active session",
            },
            "top_k": {
                "type": "integer",
                "minimum": 1,
                "maximum": 10,
                "description": "Number of results to return (1–10, default: 5)",
            },
        },
        "required": ["query"],
    },
)
async def search_documents(
    query: str,
    session_id: str = "",
    top_k: int = 5,
) -> str:
    """Search indexed documents using hybrid search.

    Combines PostgreSQL full-text (keyword) and dense vector (semantic) search with RRF fusion.
    ``session_id`` defaults to the caller's active session; ``current`` and
    ``active`` are accepted aliases. Explicit IDs must belong to the active
    session — cross-session reads stay rejected.
    """
    _, owned_session = await active_agent_scope()
    target = str(session_id or "").strip()
    if target and target.lower() not in ("current", "active"):
        try:
            sid = uuid.UUID(target)
        except ValueError:
            raise ValidationError(f"Invalid session_id '{session_id}'") from None
        if sid != owned_session:
            raise ToolError("document session does not belong to the active agent session")
    else:
        sid = owned_session

    if type(top_k) is not int or not 1 <= top_k <= 10:
        raise ValidationError("top_k must be an integer between 1 and 10")

    pipeline = await get_rag_pipeline()

    try:
        results = await pipeline.search(
            query=query,
            session_id=sid,
            top_k=top_k,
        )

        if not results:
            return f"No results found for '{query}'"

        results = results[:top_k]
        lines = [f"Search results for '{query[:200]}' ({len(results)} results):"]
        # Share the evidence budget across all hits so later passages survive
        # the agent's bounded tool message. Keep line structure and provenance.
        headers = []
        for i, result in enumerate(results, 1):
            source = str(result.chunk.payload.get("metadata", {}).get("source", "unknown"))[:240]
            ranking = "reranker" if result.rerank_score is not None else "retrieval"
            headers.append(
                f"\n{i}. [{result.score:.4f}; {ranking}] {source} (chunk {result.chunk.id})"
            )
        # Include headers, newlines and truncation markers in the total budget.
        remaining = 8500 - len(lines[0]) - sum(map(len, headers)) - 100 * len(results)
        passage_limit = min(2000, max(0, remaining // len(results)))
        for result, header in zip(results, headers, strict=True):
            text = result.chunk.payload.get("text", "")
            lines.extend([header, text[:passage_limit]])
            if len(text) > passage_limit:
                lines.append(f"[passage truncated: {len(text) - passage_limit} characters omitted]")

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
