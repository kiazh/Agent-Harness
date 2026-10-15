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
from typing import Any

from machina.observability.diagnostics import record_failure

logger = logging.getLogger(__name__)


def _shared_embedder():
    """Resolve the process-wide embedder; None when unavailable."""
    from machina.rag.embedder import get_shared_embedder

    return get_shared_embedder()


def _space_kind(embedder: Any) -> str:
    """Backend label from the embedder class (``OnnxEmbedder`` -> ``onnx``)."""
    name = type(embedder).__name__
    if name.endswith("Embedder"):
        name = name[: -len("Embedder")]
    return name.lower() or "unknown"


def embedding_space_id(embedder: Any | None = None) -> str | None:
    """Stable identifier of the embedding space *embedder* produces (H08).

    Stored memory vectors are only comparable when they come from the same
    space: same backend kind, model, stored width, and — for API backends —
    the same endpoint. The identifier is persisted next to a stored vector at
    write time and used to filter compatible rows at query time, so switching
    models or backends cannot silently rank vectors from two different spaces
    against each other (same dimensions no longer implies compatibility).

    Returns None when no embedder is resolvable (keyword-only operation); the
    caller then keeps the pre-H08 behavior (no tag, no filter). Never raises.
    """
    if embedder is None:
        try:
            embedder = _shared_embedder()
        except Exception as e:
            logger.debug("embedding space unavailable: %s", e)
            return None
        if embedder is None:
            return None
    try:
        kind = _space_kind(embedder)
        model = str(getattr(embedder, "model_name", "") or "")
        try:
            dimensions = int(getattr(embedder, "dimensions", 0) or 0)
        except (TypeError, ValueError):
            dimensions = 0
        endpoint = str(getattr(embedder, "_base_url", "") or "")
    except Exception as e:
        logger.debug("embedding space unresolvable: %s", e)
        return None
    space = f"{kind}:{model}:{dimensions}"
    if endpoint:
        space = f"{space}@{endpoint}"
    return space


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
        vector = await embedder.embed(text[:2000])
    except Exception as e:
        logger.debug("embedding failed, keyword fallback: %s", e)
        record_failure("embeddings.embed_query", e)
        return None
    if not any(vector):
        # Defense in depth (R4-021): a well-formed all-zero vector has no
        # cosine direction and would silently poison dense retrieval, so
        # treat it as an unavailable embedding. (Genuine empty/whitespace
        # inputs already returned None above; the batch path keeps the
        # documented empty-string -> zero-vector mapping.)
        error = ValueError("embedding provider returned an all-zero vector")
        logger.debug("embedding unusable, keyword fallback: %s", error)
        record_failure("embeddings.embed_query", error)
        return None
    return vector


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
            "enable dense retrieval with a local runtime "
            "(pip install 'Machina[embeddings-onnx]' or 'Machina[embeddings-local]' "
            "and set MACHINA_EMBEDDING_BACKEND), or set OPENAI_API_KEY"
        ),
    }
