"""RAG tools — index_document and search_documents."""

from __future__ import annotations

import asyncio
import logging
import uuid
import weakref
from dataclasses import dataclass
from itertools import count
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
# Lifecycle state is keyed by a per-generation token, never by id(pipeline):
# CPython may reuse an object address after GC, which made a stale release
# decrement (or close) an unrelated newer generation. ``_pipeline_users`` is
# generation-key -> live lease count; ``_pipeline_leases`` is lease-token ->
# (pipeline, generation-key); ``_retired_pipelines`` is generation-key ->
# pipeline awaiting its last release.
_pipeline_users: dict[int, int] = {}
_pipeline_leases: dict[int, tuple[RAGPipeline, int]] = {}
_retired_pipelines: dict[int, RAGPipeline] = {}
_pipeline_generation_counter = count(1)
_pipeline_lease_counter = count(1)
_pipeline_key_fallback: weakref.WeakKeyDictionary[Any, int] = weakref.WeakKeyDictionary()
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


@dataclass(frozen=True)
class RAGLease:
    """One retained use of a shared RAG generation.

    The token, not the pipeline object, is the lifecycle key: releasing the
    same lease twice is a no-op, and a stale lease can never decrement a newer
    generation that happens to reuse the pipeline's address.
    """

    pipeline: RAGPipeline
    token: int

    def __getattr__(self, name: str) -> Any:
        return getattr(self.pipeline, name)


def _pipeline_generation_key(pipeline: RAGPipeline) -> int:
    """Stable generation key; never ``id(pipeline)``.

    Attach a monotonic token to the instance. For an exotic object that
    refuses attributes, use a weak-keyed fallback; both remain stable while
    the object lives and are never reused after GC.
    """
    key = getattr(pipeline, "_rag_generation_key", None)
    if type(key) is int:
        return key
    key = next(_pipeline_generation_counter)
    try:
        setattr(pipeline, "_rag_generation_key", key)
    except (AttributeError, TypeError):
        try:
            return _pipeline_key_fallback[pipeline]
        except (KeyError, TypeError):
            _pipeline_key_fallback[pipeline] = key
    return key


def _lease_token_for(lease: RAGLease | RAGPipeline) -> int | None:
    """Resolve a lease token, accepting a raw pipeline for compatibility."""
    if isinstance(lease, RAGLease):
        return lease.token
    for token, (pipeline, _key) in reversed(_pipeline_leases.items()):
        if pipeline is lease:
            return token
    return None


async def _close_pipeline_quietly(pipeline: RAGPipeline) -> None:
    """Close a replaced or released pipeline's owned clients (F08, H19).

    The shared pipeline builds and owns an embedder (and reranker) per
    generation; without this the superseded instances — and the credential
    they bound — were never released. Each close is isolated so one failure
    cannot abort the rebuild/release or mask the other, and a failure is only
    logged (never surfaced), matching the shared-embedder close seam. The
    closed marker makes every close path idempotent: a retired generation can
    be reached by both a rebuild and a release, and must close only once.
    """
    if getattr(pipeline, "_rag_closed", False):
        return
    try:
        setattr(pipeline, "_rag_closed", True)
    except (AttributeError, TypeError):
        pass
    for name in ("_embedder", "_reranker"):
        component = getattr(pipeline, name, None)
        close = getattr(component, "close", None)
        if not callable(close):
            continue
        try:
            await close()
        except Exception as e:
            logger.warning("failed to close replaced RAG %s: %s", name, e, exc_info=True)


async def _select_pipeline_locked() -> RAGPipeline:
    """Return the current generation, rebuilding when its identity changes.

    The caller must hold ``_rag_pipeline_lock``. Selection and the subsequent
    lease increment happen in the same critical section; otherwise a rebuild
    could observe zero users after the caller selected the old pipeline but
    before it retained it, and close the pipeline the caller is about to use.
    """
    global _rag_pipeline, _rag_pipeline_identity
    current = _current_pipeline_identity()
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
            previous_key = _pipeline_generation_key(previous)
            if _pipeline_users.get(previous_key, 0):
                _retired_pipelines[previous_key] = previous
            else:
                await _core_symbol("machina.core.background_tasks", "await_joined")(
                    _close_pipeline_quietly(previous)
                )
    pipeline = _rag_pipeline
    assert pipeline is not None
    return pipeline


async def get_rag_pipeline() -> RAGPipeline:
    """Get or create the global RAG pipeline instance.

    Rebuilds when the pipeline IDENTITY changes: the reranker selection it
    was built with (backend/model/endpoint/timeout or a rotated Cohere key),
    the resolved embedding selection (backend/model/endpoint, a rotated or
    newly supplied embedding credential, or a keyword-only <-> dense
    availability change), or the RRF fusion weights. Reuses when unchanged;
    the superseded pipeline's owned clients are closed only after every
    outstanding lease is released.
    """
    async with _rag_pipeline_lock:
        return await _select_pipeline_locked()


async def acquire_rag_pipeline() -> RAGLease:
    """Retain the shared generation and return a release token for it."""
    async with _rag_pipeline_lock:
        pipeline = await _select_pipeline_locked()
        key = _pipeline_generation_key(pipeline)
        _pipeline_users[key] = _pipeline_users.get(key, 0) + 1
        token = next(_pipeline_lease_counter)
        _pipeline_leases[token] = (pipeline, key)
        return RAGLease(pipeline=pipeline, token=token)


async def release_rag_pipeline(lease: RAGLease | RAGPipeline) -> None:
    """Release one lease; close a retired generation after its last user.

    The token makes this idempotent: a duplicate release is ignored instead of
    stealing a later consumer's count. A raw pipeline is accepted for
    compatibility and releases its most recent outstanding lease.
    """
    token = _lease_token_for(lease)
    if token is None:
        return
    retired: RAGPipeline | None = None
    async with _rag_pipeline_lock:
        entry = _pipeline_leases.pop(token, None)
        if entry is None:
            return
        _pipeline, key = entry
        users = _pipeline_users.get(key, 0)
        if users > 1:
            _pipeline_users[key] = users - 1
        elif users == 1:
            del _pipeline_users[key]
            retired = _retired_pipelines.pop(key, None)
        # users == 0 means the lifecycle state was reset while this token was
        # outstanding; do not close an unrelated generation on a stale token.
    if retired is not None:
        await _core_symbol("machina.core.background_tasks", "await_joined")(
            _close_pipeline_quietly(retired)
        )


async def close_active_rag_pipeline() -> None:
    """Release the active shared generation through the one lifecycle helper (H19).

    Runtime-shutdown seam. The previous inline close covered only the active
    generation's embedder, so an HTTP/Cohere reranker client could outlive
    runtime shutdown/restart; it also left the closed generation cached, so a
    restart reused clients that were already closed. Every component the
    active generation owns (embedder and reranker) is released through the
    same isolated per-component close used for replaced generations, and the
    recorded generation identity is reset so the next request builds fresh
    clients instead of reusing the released ones.

    If leases are still outstanding, retire the generation instead of closing
    it under its users; the last release closes it.
    """
    global _rag_pipeline, _rag_pipeline_identity
    pipeline: RAGPipeline | None
    async with _rag_pipeline_lock:
        pipeline = _rag_pipeline
        _rag_pipeline = None
        _rag_pipeline_identity = None
        if pipeline is not None:
            key = _pipeline_generation_key(pipeline)
            if _pipeline_users.get(key, 0):
                _retired_pipelines[key] = pipeline
                pipeline = None
    if pipeline is not None:
        await _close_pipeline_quietly(pipeline)


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
    pipeline = await acquire_rag_pipeline()

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
    finally:
        await release_rag_pipeline(pipeline)


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

    pipeline = await acquire_rag_pipeline()

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
    finally:
        await release_rag_pipeline(pipeline)


registry.declare_effects(
    {
        "index_document": ("fs.read",),
        "search_documents": ("rag.read",),
    }
)
