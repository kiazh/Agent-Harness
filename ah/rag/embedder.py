"""Embedding models — abstract interface with OpenAI text-embedding-3-small."""

from __future__ import annotations

import hashlib
import logging
import os
import threading
from abc import ABC, abstractmethod
from collections import OrderedDict

import httpx

from ah.core.config import config
from ah.core.exceptions import ProviderError
from ah.core.provider import audit_log

logger = logging.getLogger(__name__)


class Embedder(ABC):
    """Abstract interface for text embedding models."""

    @abstractmethod
    async def embed(self, text: str) -> list[float]:
        """Embed a single text string into a vector."""
        raise NotImplementedError

    @abstractmethod
    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of text strings into vectors."""
        raise NotImplementedError

    @property
    @abstractmethod
    def dimensions(self) -> int:
        """Return the dimensionality of the embedding vectors."""
        raise NotImplementedError

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Return the model name."""
        raise NotImplementedError


class OpenAIEmbedder(Embedder):
    """OpenAI text-embedding-3-small embedder with LRU cache and batch support.

    Uses the OpenAI embeddings API (also compatible with OpenRouter).
    Default model: text-embedding-3-small (1536 dimensions).
    """

    DEFAULT_MODEL = "text-embedding-3-small"
    DEFAULT_BASE_URL = "https://api.openai.com/v1"
    DEFAULT_CACHE_SIZE = 1024
    DEFAULT_BATCH_SIZE = 100

    MODEL_DIMENSIONS: dict[str, int] = {
        "text-embedding-3-small": 1536,
        "text-embedding-3-large": 3072,
        "text-embedding-ada-002": 1536,
    }

    def __init__(
        self,
        api_key: str | None = None,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        cache_size: int = DEFAULT_CACHE_SIZE,
        batch_size: int = DEFAULT_BATCH_SIZE,
        timeout: float = 30.0,
        dimensions: int | None = None,
    ) -> None:
        self.api_key = api_key or config.get("openai_api_key") or ""
        if not self.api_key:
            # Fall back to OpenRouter if no OpenAI key
            self.api_key = config.get("openrouter_api_key") or ""
            if self.api_key:
                base_url = "https://openrouter.ai/api/v1"
                logger.info("Using OpenRouter for embeddings (no OPENAI_API_KEY set)")

        if not self.api_key:
            raise ProviderError("No API key found. Set OPENAI_API_KEY or OPENROUTER_API_KEY.")

        self._model = model
        self._base_url = base_url.rstrip("/")
        self._batch_size = batch_size
        self._timeout = timeout
        self._cache_size = cache_size
        env_dim = os.getenv("EMBEDDING_DIMENSIONS")
        if dimensions is not None:
            self._dimensions = dimensions
        elif env_dim and env_dim.isdigit():
            self._dimensions = int(env_dim)
        else:
            self._dimensions = self.MODEL_DIMENSIONS.get(model, 1536)

        # LRU cache: hash(model:base_url:text) → embedding (guarded by lock)
        self._cache: OrderedDict[str, list[float]] = OrderedDict()
        self._cache_lock = threading.Lock()

        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )

    @property
    def dimensions(self) -> int:
        # Fallback for tests constructing via __new__ without __init__.
        return getattr(self, "_dimensions", 1536)

    @property
    def model_name(self) -> str:
        return self._model

    async def embed(self, text: str) -> list[float]:
        """Embed a single text string (with LRU cache)."""
        # Lazily init cache attrs for tests using __new__ without __init__.
        if not hasattr(self, "_cache_lock"):
            import threading as _th
            from collections import OrderedDict as _OD

            self._cache_lock = _th.Lock()  # type: ignore[attr-defined]
            if not hasattr(self, "_cache"):
                self._cache = _OD()  # type: ignore[attr-defined]
            if not hasattr(self, "_cache_size"):
                self._cache_size = 1024  # type: ignore[attr-defined]
        if not text:
            return [0.0] * self.dimensions

        cache_key = self._cache_key(text)
        with self._cache_lock:  # type: ignore[attr-defined]
            if cache_key in self._cache:
                # Move to end (most recently used)
                self._cache.move_to_end(cache_key)
                return list(self._cache[cache_key])

        result = await self._embed_uncached([text])
        embedding = result[0] if result else [0.0] * self.dimensions

        # Cache the result
        with self._cache_lock:
            self._cache[cache_key] = embedding
            if len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)  # Evict LRU

        return list(embedding)

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts, using cache where possible."""
        if not texts:
            return []
        if not hasattr(self, "_cache_lock"):
            import threading as _th2
            from collections import OrderedDict as _OD2

            self._cache_lock = _th2.Lock()  # type: ignore[attr-defined]
            if not hasattr(self, "_cache"):
                self._cache = _OD2()  # type: ignore[attr-defined]
            if not hasattr(self, "_cache_size"):
                self._cache_size = 1024  # type: ignore[attr-defined]
            if not hasattr(self, "_batch_size"):
                self._batch_size = 100  # type: ignore[attr-defined]

        results: list[list[float] | None] = [None] * len(texts)
        uncached_indices: list[int] = []
        uncached_texts: list[str] = []

        # Check cache first
        for i, text in enumerate(texts):
            if not text:
                results[i] = [0.0] * self.dimensions
                continue
            cache_key = self._cache_key(text)
            with self._cache_lock:
                if cache_key in self._cache:
                    self._cache.move_to_end(cache_key)
                    results[i] = list(self._cache[cache_key])
                else:
                    uncached_indices.append(i)
                    uncached_texts.append(text)

        # Embed uncached texts in batches
        if uncached_texts:
            for batch_start in range(0, len(uncached_texts), self._batch_size):
                batch_end = min(batch_start + self._batch_size, len(uncached_texts))
                batch_texts = uncached_texts[batch_start:batch_end]
                batch_indices = uncached_indices[batch_start:batch_end]

                batch_embeddings = await self._embed_uncached(batch_texts)

                for idx, embedding in zip(batch_indices, batch_embeddings, strict=True):
                    results[idx] = list(embedding)
                    # Cache
                    cache_key = self._cache_key(texts[idx])
                    with self._cache_lock:
                        self._cache[cache_key] = list(embedding)
                        if len(self._cache) > self._cache_size:
                            self._cache.popitem(last=False)

        # Fill any remaining None with zero vectors
        for i in range(len(results)):
            if results[i] is None:
                results[i] = [0.0] * self.dimensions

        return results  # type: ignore[return-value]

    async def _embed_uncached(self, texts: list[str]) -> list[list[float]]:
        """Call the embedding API for a list of texts."""
        if not texts:
            return []

        audit_log(
            "rag_embed_start",
            model=self._model,
            count=len(texts),
            provider="openai",
        )

        try:
            resp = await self._client.post(
                "/embeddings",
                json={"model": self._model, "input": texts},
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            audit_log("rag_embed_error", model=self._model, error=str(e))
            logger.error("Embedding API call failed: %s", e)
            raise

        # Sort by index to maintain order — validate index field
        items = data.get("data", [])
        for item in items:
            if "index" not in item:
                raise ValueError("Embedding API response missing 'index' field")
        items.sort(key=lambda x: x["index"])
        embeddings = [item["embedding"] for item in items]

        audit_log(
            "rag_embed_complete",
            model=self._model,
            count=len(embeddings),
        )

        return embeddings

    def _cache_key(self, text: str) -> str:
        """Generate a cache key for a text string (namespaced by model + base_url)."""
        model = getattr(self, "_model", "text-embedding-3-small")
        base = getattr(self, "_base_url", "https://api.openai.com/v1")
        scoped = f"{model}:{base}:{text}"
        return hashlib.sha256(scoped.encode("utf-8")).hexdigest()

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()

    @property
    def cache_size(self) -> int:
        """Current number of cached embeddings."""
        return len(self._cache)
