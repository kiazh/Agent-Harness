"""Query embedding service with honest fallback (Phase C).

Generates dense query embeddings when an embedding provider is available;
returns None otherwise so callers use keyword-only retrieval and report the
fallback accurately. Never raises for missing keys — degradation, not crash.

The embedder is process-wide shared and cached (``get_shared_embedder``):
local model weights load once and are reused across queries instead of
reloading per call.
"""

from __future__ import annotations

import logging

from machina.observability.diagnostics import record_failure

logger = logging.getLogger(__name__)


def _shared_embedder():
    """Resolve the process-wide embedder; None when unavailable."""
    from machina.rag.embedder import get_shared_embedder

    return get_shared_embedder()


async def embed_query(text: str) -> list[float] | None:
    """Embed *text* for retrieval, or None when unavailable (keyword fallback)."""
    if not text or not text.strip():
        return None
    try:
        embedder = _shared_embedder()
    except Exception as e:
        logger.debug("embedding unavailable: %s", e)
        return None
    if embedder is None:
        return None
    try:
        return await embedder.embed(text[:2000])
    except Exception as e:
        logger.debug("embedding failed, keyword fallback: %s", e)
        record_failure("embeddings.embed_query", e)
        return None


async def embed_texts(texts: list[str]) -> list[list[float]] | None:
    """Embed a batch through the shared embedder; None when unavailable.

    Used by indexing/backfill paths so stored vectors and query vectors
    come from the same enforced embedding space.
    """
    if not texts:
        return []
    try:
        embedder = _shared_embedder()
    except Exception as e:
        logger.debug("embedding unavailable: %s", e)
        return None
    if embedder is None:
        return None
    try:
        return await embedder.embed_batch([text[:2000] for text in texts])
    except Exception as e:
        logger.debug("batch embedding failed: %s", e)
        record_failure("embeddings.embed_texts", e)
        return None


def embedding_status() -> dict[str, str]:
    """Report embedding availability for /status (never claims healthy falsely)."""
    try:
        from machina.rag.embedder import resolve_embedding_settings

        settings = resolve_embedding_settings()
        if settings.kind != "none":
            return {
                "mode": "dense+sparse",
                "backend": settings.kind,
                "model": settings.model,
                "reason": "",
            }
        reason = settings.reason or "no embedding provider configured"
    except Exception as _boundary_error:
        # Optional fallback preserves the primary outcome; report no payload.
        record_failure("embeddings.embedding_status", _boundary_error)
        reason = "embedding backend lookup failed"
    return {
        "mode": "keyword-only",
        "reason": reason,
        "next": (
            "install the local runtime (pip install 'Machina[embeddings-local]') "
            "or set OPENAI_API_KEY for dense retrieval"
        ),
    }
