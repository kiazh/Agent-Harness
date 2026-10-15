"""RAG tools — index_document and search_documents."""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any

from machina.core.exceptions import ToolError, ValidationError
from machina.core.provider import audit_log
from machina.rag.pipeline import RAGPipeline
from machina.tools.agents import active_agent_scope
from machina.tools.base import _core_symbol, registry

logger = logging.getLogger(__name__)


def _execution_workspace() -> Path:
    """Workspace root approved for this tool call (R4-026).

    ``execution_context`` is the one canonical resolver; the permission
    broker normalized the approved target against the same root. Bound
    through the established ``machina.tools.base`` leaf-core seam so the
    tools package core-import ratchet does not grow for one symbol.
    """
    execution_context = _core_symbol("machina.core.execution_context", "execution_context")
    return execution_context("index_document").workspace


# Global RAG pipeline instance (lazy-initialized)
_rag_pipeline: RAGPipeline | None = None
_rag_pipeline_lock = asyncio.Lock()
# IDENTITY of the shared instance: (reranker selection identity,
# resolved embedding selection identity, (keyword RRF weight, dense RRF
# weight)). Rotation of any component — a different reranker
# backend/model/endpoint/timeout, a rotated Cohere key, an embedding
# backend/model/endpoint change, an embedding credential rotation, a
# backend that becomes available/unavailable (keyword-only <-> dense), or an
# RRF weight change — must rebuild the pipeline; comparing presence alone
# kept the old client (and its old key) serving all later requests
# (AH-AUDIT-036, R17, R19, F08). The embedding component delegates to the
# shared embedder's identity helper so both agree on the credential digest.
_rag_pipeline_identity: (
    tuple[tuple[str, str, str, str, float], tuple[str, str, str, str], tuple[float, float]] | None
) = None


def _current_reranker_identity() -> tuple[str, str, str, str, float]:
    """Resolved reranker selection identity (fresh read; no model loads)."""
    try:
        from machina.rag.reranker import (
            reranker_selection_identity,
            resolve_reranker_selection,
        )

        return reranker_selection_identity(resolve_reranker_selection())
    except Exception:
        return ("none", "", "", "", 0.0)


def _built_reranker_identity(
    pipeline: RAGPipeline | None,
) -> tuple[str, str, str, str, float] | None:
    """Identity of the reranker the pipeline actually BOUND to.

    Shares the configured-identity calculation so both sides compare
    directly (R17); None means passthrough (or a foreign implementation):
    the configured identity stays recorded so a failing build is not
    retried on every request.
    """
    try:
        from machina.rag.reranker import bound_reranker_identity

        return bound_reranker_identity(getattr(pipeline, "_reranker", None))
    except Exception:
        return None


def _current_embedding_identity() -> tuple[str, str, str, str]:
    """Resolved embedding selection identity (kind, model, base_url, digest).

    Delegates to the shared embedder's :func:`~machina.rag.embedder._embedder_identity`
    so the pipeline and the process-wide embedder agree on ONE nonsecret
    credential digest (F08: do not duplicate the digest). The resolved
    ``kind`` also captures effective availability, so a keyword-only pipeline
    rebuilds to dense when a key or local runtime appears — even though the
    configured backend/model/base_url text is unchanged.
    """
    try:
        from machina.rag.embedder import _embedder_identity, resolve_embedding_settings

        return _embedder_identity(resolve_embedding_settings())
    except Exception:
        return ("none", "", "", "")


def _current_weight_identity() -> tuple[float, float]:
    """Configured RRF fusion weights (R19: read only at construction)."""
    try:
        from machina.core.config import config as _cfg

        return (float(_cfg.get("rrf_keyword_weight")), float(_cfg.get("rrf_dense_weight")))
    except Exception:
        return (1.0, 1.0)


def _current_pipeline_identity() -> tuple[
    tuple[str, str, str, str, float], tuple[str, str, str, str], tuple[float, float]
]:
    """Identity of everything the shared pipeline is configured from (fresh read)."""
    return (
        _current_reranker_identity(),
        _current_embedding_identity(),
        _current_weight_identity(),
    )


def _build_shared_pipeline() -> RAGPipeline:
    """Choose the configured reranker (AH-AUDIT-036).

    Local ONNX reranking is preferred in ``auto`` (zero-API design rule),
    then a Cohere key, then a configured HTTP endpoint when explicitly
    selected; a missing runtime or failing client builds a passthrough with
    the reason recorded. Explicit disabled mode (``reranker_backend=none``)
    is honored by the resolver.
    """
    try:
        from machina.rag.reranker import build_reranker

        reranker = build_reranker()
    except Exception as e:
        # build_reranker never raises; this boundary keeps construction
        # crash-free even if a future backend violates that contract.
        logger.warning("reranker unavailable, passthrough: %s", e, exc_info=True)
        from machina.rag.reranker import IdentityReranker

        reranker = IdentityReranker(reason=f"build failed: {e}")
    return RAGPipeline(reranker=reranker)


async def _close_pipeline_quietly(pipeline: RAGPipeline) -> None:
    """Close a replaced pipeline's owned embedder/reranker clients (F08).

    The shared pipeline builds and owns an embedder (and reranker) per
    generation; without this the superseded instances — and the credential
    they bound — were never released. Each close is isolated so one failure
    cannot abort the rebuild or mask the other, and a failure is only logged
    (never surfaced), matching the shared-embedder close seam.
    """
    for name in ("_embedder", "_reranker"):
        component = getattr(pipeline, name, None)
        close = getattr(component, "close", None)
        if not callable(close):
            continue
        try:
            await close()
        except Exception as e:
            logger.warning("failed to close replaced RAG %s: %s", name, e, exc_info=True)


async def get_rag_pipeline() -> RAGPipeline:
    """Get or create the global RAG pipeline instance.

    Rebuilds when the pipeline IDENTITY changes: the reranker selection it
    was built with (backend/model/endpoint/timeout or a rotated Cohere key),
    the resolved embedding selection (backend/model/endpoint, a rotated or
    newly supplied embedding credential, or a keyword-only <-> dense
    availability change), or the RRF fusion weights. Reuses when unchanged;
    the superseded pipeline's owned clients are closed.
    """
    global _rag_pipeline, _rag_pipeline_identity
    current = _current_pipeline_identity()
    if _rag_pipeline is None or _rag_pipeline_identity != current:
        async with _rag_pipeline_lock:
            # Double-check after acquiring lock
            if _rag_pipeline is None or _rag_pipeline_identity != current:
                previous = _rag_pipeline
                _rag_pipeline = _build_shared_pipeline()
                # Record the identity the reranker actually bound to. When the
                # build fell back to passthrough without binding a backend,
                # keep the configured identity so a failing runtime is not
                # retried on every request; a real config/key change changes
                # `current` and still forces the rebuild. Embedding and weight
                # identity stay resolved-config-derived: build_embedder
                # degrades to keyword-only without changing it, and the
                # resolved embedding identity already carries the effective
                # availability (F08).
                built = _built_reranker_identity(_rag_pipeline)
                _rag_pipeline_identity = (
                    built if built is not None else current[0],
                    current[1],
                    current[2],
                )
                if previous is not None:
                    await _close_pipeline_quietly(previous)
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

    # R4-026: resolve the target through the same canonical execution-context
    # workspace the broker approved against, so the indexed file is the
    # approved one — never a same-named file under machina_home/cwd.
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = _execution_workspace() / candidate
    resolved_path = str(candidate.resolve())

    # AH-019: resolve the owning agent and pass it explicitly so documents
    # are not mislabeled with the pipeline default ("harness").
    pipeline = await get_rag_pipeline()

    try:
        chunks = await pipeline.index_document(
            source=resolved_path,
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
