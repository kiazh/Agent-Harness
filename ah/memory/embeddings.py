"""Query embedding service with honest fallback (Phase C).

Generates dense query embeddings when an embedding provider is available;
returns None otherwise so callers use keyword-only retrieval and report the
fallback accurately. Never raises for missing keys — degradation, not crash.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def embed_query(text: str) -> list[float] | None:
    """Embed *text* for retrieval, or None when unavailable (keyword fallback)."""
    if not text or not text.strip():
        return None
    try:
        from ah.rag.embedder import OpenAIEmbedder

        embedder = OpenAIEmbedder()
    except Exception as e:
        logger.debug("embedding unavailable: %s", e)
        return None
    try:
        return await embedder.embed(text[:2000])
    except Exception as e:
        logger.debug("embedding failed, keyword fallback: %s", e)
        try:
            await embedder.close()
        except Exception:
            pass
        return None
    finally:
        try:
            await embedder.close()
        except Exception:
            pass


def embedding_status() -> dict[str, str]:
    """Report embedding availability for /status (never claims healthy falsely)."""
    try:
        from ah.core.config import config

        if config.get("openai_api_key") or config.get("openrouter_api_key"):
            return {"mode": "dense+sparse", "reason": ""}
    except Exception:
        pass
    return {
        "mode": "keyword-only",
        "reason": "no embedding provider configured",
        "next": "set OPENAI_API_KEY for dense retrieval",
    }
