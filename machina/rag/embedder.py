"""Embedding models — abstract interface with OpenAI-compatible and local backends."""

from __future__ import annotations

import asyncio
import hashlib
import importlib as _importlib
import logging
import os
import sys as _sys
import threading
from abc import ABC, abstractmethod
from collections import OrderedDict
from dataclasses import dataclass

import httpx


# SEAM: leaf-core seam for machina.core (config, exceptions ProviderError,
# provider audit_log). Deferred via sys.modules/importlib so this module imports
# with machina.core blocked; resolves to the same objects when core is present.
# No logic change.
def _core_symbol(module: str, name: str):
    mod = _sys.modules.get(module)
    if mod is None:
        mod = _importlib.import_module(module)
    return getattr(mod, name)


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
        # An explicitly configured base_url (llama.cpp / Ollama / LM Studio)
        # is honored as-is: the OpenRouter fallback below must not silently
        # redirect it, and no key is required for such local servers.
        default_endpoint = (base_url or self.DEFAULT_BASE_URL).rstrip("/") == self.DEFAULT_BASE_URL
        self.api_key = (
            api_key or _core_symbol("machina.core.config", "config").get("openai_api_key") or ""
        )
        if not self.api_key and default_endpoint:
            # Fall back to OpenRouter if no OpenAI key
            self.api_key = (
                _core_symbol("machina.core.config", "config").get("openrouter_api_key") or ""
            )
            if self.api_key:
                base_url = "https://openrouter.ai/api/v1"
                logger.info("Using OpenRouter for embeddings (no OPENAI_API_KEY set)")

        if not self.api_key and default_endpoint:
            raise _core_symbol("machina.core.exceptions", "ProviderError")(
                "No API key found. Set OPENAI_API_KEY or OPENROUTER_API_KEY."
            )

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

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=headers,
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

        _core_symbol("machina.core.provider", "audit_log")(
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
            _core_symbol("machina.core.provider", "audit_log")(
                "rag_embed_error", model=self._model, error=str(e)
            )
            logger.error("Embedding API call failed: %s", e)
            raise

        # Sort by index to maintain order — validate index field
        items = data.get("data", [])
        for item in items:
            if "index" not in item:
                raise ValueError("Embedding API response missing 'index' field")
        items.sort(key=lambda x: x["index"])
        embeddings = [item["embedding"] for item in items]

        _core_symbol("machina.core.provider", "audit_log")(
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


def _zero_pad_rows(rows: list[list[float]], width: int) -> list[list[float]]:
    """Zero-pad shorter vectors so they fit the fixed storage width.

    Zero padding leaves cosine similarity unchanged, so dense search works
    against the fixed ``vector(1536)`` schema without a migration.
    """
    for row in rows:
        if len(row) < width:
            row.extend([0.0] * (width - len(row)))
    return rows


class LocalEmbedder(Embedder):
    """Zero-API local embeddings via model2vec static models (pure NumPy).

    ``model2vec`` is imported and the model weights are loaded lazily on the
    first embed call — never at import or construction — and the loaded
    model is reused for every later call on the instance. ``close()``
    unloads the weights; the next call reloads them from the local HF cache.

    Storage note: machina's pgvector columns are fixed ``vector(1536)``.
    Static potion models emit far fewer dimensions, so vectors are
    zero-padded to ``EMBEDDING_DIMENSIONS`` (default 1536). Zero padding
    leaves cosine similarity unchanged, so dense search works without a
    schema migration; ``native_dimensions`` reports the model's real width.
    """

    DEFAULT_MODEL = "minishlab/potion-base-8M"
    DEFAULT_PAD_DIMENSIONS = 1536
    DEFAULT_BATCH_SIZE = 64
    # Known native widths: lets dimensions/native_dimensions answer without
    # forcing a model download. Unknown ids fall back to the loaded model.
    NATIVE_DIMENSIONS: dict[str, int] = {
        "minishlab/potion-base-2M": 64,
        "minishlab/potion-base-4M": 128,
        "minishlab/potion-base-8M": 256,
        "minishlab/potion-base-32M": 512,
        "minishlab/potion-retrieval-32M": 512,
        "minishlab/potion-multilingual-128M": 256,
    }

    def __init__(
        self,
        model: str | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
        pad_to: int | None = None,
    ) -> None:
        self._model_id = (model or "").strip() or self.DEFAULT_MODEL
        self._batch_size = batch_size
        env_pad = os.getenv("EMBEDDING_DIMENSIONS", "")
        if pad_to is None:
            pad_to = int(env_pad) if env_pad.isdigit() else self.DEFAULT_PAD_DIMENSIONS
        self._pad_to = pad_to
        self._model = None
        self._native_dimensions: int | None = None
        self._dimensions: int | None = None
        self._load_lock = threading.Lock()

    @property
    def model_name(self) -> str:
        return self._model_id

    @property
    def native_dimensions(self) -> int:
        """The model's real output width (before storage padding)."""
        if self._native_dimensions is None:
            known = self.NATIVE_DIMENSIONS.get(self._model_id)
            if known is None:
                known = int(getattr(self._load_model(), "dim"))
            self._native_dimensions = known
        return self._native_dimensions

    @property
    def dimensions(self) -> int:
        """Effective (stored) width: padded when the schema is wider."""
        if self._dimensions is None:
            native = self.native_dimensions
            self._dimensions = self._pad_to if self._pad_to >= native else native
        return self._dimensions

    def _load_model(self):
        """Import/load model2vec and the weights once (thread-safe)."""
        if self._model is None:
            with self._load_lock:
                if self._model is None:
                    from model2vec import StaticModel

                    self._model = StaticModel.from_pretrained(self._model_id, force_download=False)
        return self._model

    def _encode(self, texts: list[str]) -> list[list[float]]:
        """Blocking encode (runs on a worker thread) with storage padding."""
        model = self._load_model()
        vectors = model.encode(
            list(texts),
            batch_size=self._batch_size,
            show_progress_bar=False,
            use_multiprocessing=False,
        )
        return _zero_pad_rows(vectors.tolist(), self.dimensions)

    async def embed(self, text: str) -> list[float]:
        """Embed a single text (model weights load lazily on first call)."""
        if not text:
            return [0.0] * self.dimensions
        return (await self.embed_batch([text]))[0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch; empty strings map to zero vectors, like the API backends."""
        if not texts:
            return []
        results: list[list[float] | None] = [None] * len(texts)
        pending: list[str] = []
        pending_indices: list[int] = []
        for index, text in enumerate(texts):
            if text:
                pending.append(text)
                pending_indices.append(index)
            else:
                results[index] = [0.0] * self.dimensions
        if pending:
            _core_symbol("machina.core.provider", "audit_log")(
                "rag_embed_start", model=self._model_id, count=len(pending), provider="local"
            )
            try:
                encoded = await asyncio.to_thread(self._encode, pending)
            except Exception as e:
                _core_symbol("machina.core.provider", "audit_log")(
                    "rag_embed_error", model=self._model_id, error=str(e)
                )
                logger.error("Local embedding call failed: %s", e)
                raise
            for index, vector in zip(pending_indices, encoded, strict=True):
                results[index] = vector
            _core_symbol("machina.core.provider", "audit_log")(
                "rag_embed_complete", model=self._model_id, count=len(encoded)
            )
        return results  # type: ignore[return-value]

    async def close(self) -> None:
        """Unload the static model; the next embed call reloads from cache."""
        with self._load_lock:
            self._model = None


class OnnxEmbedder(Embedder):
    """Zero-API local embeddings via fastembed ONNX models (no torch).

    Default model: ``BAAI/bge-small-en-v1.5`` (384-d native, MIT-licensed
    ONNX build served by fastembed). ``fastembed`` is imported and the model
    weights load lazily on the first embed call — never at import or
    construction — and the loaded model is reused for every later call on
    the instance. ``close()`` unloads the model; the next call reloads it
    from the local cache.

    Storage note: machina's pgvector columns are fixed ``vector(1536)``, so
    vectors are zero-padded to ``EMBEDDING_DIMENSIONS`` (default 1536);
    ``native_dimensions`` reports the model's real width.
    """

    DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
    DEFAULT_PAD_DIMENSIONS = 1536
    DEFAULT_BATCH_SIZE = 64
    # Known native widths: lets dimensions/native_dimensions answer without
    # forcing a model download. Unknown ids fall back to the loaded model.
    NATIVE_DIMENSIONS: dict[str, int] = {
        "BAAI/bge-small-en-v1.5": 384,
        "BAAI/bge-base-en-v1.5": 768,
        "BAAI/bge-large-en-v1.5": 1024,
        "sentence-transformers/all-MiniLM-L6-v2": 384,
        "snowflake/snowflake-arctic-embed-xs": 384,
        "snowflake/snowflake-arctic-embed-s": 384,
        "snowflake/snowflake-arctic-embed-m": 768,
        "jinaai/jina-embeddings-v2-small-en": 512,
    }

    def __init__(
        self,
        model: str | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
        pad_to: int | None = None,
        cache_dir: str | None = None,
        threads: int | None = None,
    ) -> None:
        self._model_id = (model or "").strip() or self.DEFAULT_MODEL
        self._batch_size = batch_size
        self._cache_dir = cache_dir
        self._threads = threads
        env_pad = os.getenv("EMBEDDING_DIMENSIONS", "")
        if pad_to is None:
            pad_to = int(env_pad) if env_pad.isdigit() else self.DEFAULT_PAD_DIMENSIONS
        self._pad_to = pad_to
        self._model = None
        self._native_dimensions: int | None = None
        self._dimensions: int | None = None
        self._load_lock = threading.Lock()

    @property
    def model_name(self) -> str:
        return self._model_id

    @property
    def native_dimensions(self) -> int:
        """The model's real output width (before storage padding)."""
        if self._native_dimensions is None:
            known = self.NATIVE_DIMENSIONS.get(self._model_id)
            if known is None:
                model = self._load_model()
                known = int(model.embedding_size) if model.embedding_size else None
                if not known:
                    raise ValueError(f"cannot determine embedding width for {self._model_id!r}")
            self._native_dimensions = known
        return self._native_dimensions

    @property
    def dimensions(self) -> int:
        """Effective (stored) width: padded when the schema is wider."""
        if self._dimensions is None:
            native = self.native_dimensions
            self._dimensions = self._pad_to if self._pad_to >= native else native
        return self._dimensions

    def _load_model(self):
        """Import/load fastembed and the ONNX weights once (thread-safe)."""
        if self._model is None:
            with self._load_lock:
                if self._model is None:
                    from fastembed import TextEmbedding

                    kwargs: dict = {}
                    if self._cache_dir:
                        kwargs["cache_dir"] = self._cache_dir
                    if self._threads:
                        kwargs["threads"] = self._threads
                    self._model = TextEmbedding(self._model_id, **kwargs)
        return self._model

    def _encode(self, texts: list[str]) -> list[list[float]]:
        """Blocking encode (runs on a worker thread) with storage padding."""
        model = self._load_model()
        vectors = [
            vector.tolist() for vector in model.embed(list(texts), batch_size=self._batch_size)
        ]
        return _zero_pad_rows(vectors, self.dimensions)

    async def embed(self, text: str) -> list[float]:
        """Embed a single text (model weights load lazily on first call)."""
        if not text:
            return [0.0] * self.dimensions
        return (await self.embed_batch([text]))[0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch; empty strings map to zero vectors, like the API backends."""
        if not texts:
            return []
        results: list[list[float] | None] = [None] * len(texts)
        pending: list[str] = []
        pending_indices: list[int] = []
        for index, text in enumerate(texts):
            if text:
                pending.append(text)
                pending_indices.append(index)
            else:
                results[index] = [0.0] * self.dimensions
        if pending:
            _core_symbol("machina.core.provider", "audit_log")(
                "rag_embed_start", model=self._model_id, count=len(pending), provider="onnx"
            )
            try:
                encoded = await asyncio.to_thread(self._encode, pending)
            except Exception as e:
                _core_symbol("machina.core.provider", "audit_log")(
                    "rag_embed_error", model=self._model_id, error=str(e)
                )
                logger.error("ONNX embedding call failed: %s", e)
                raise
            for index, vector in zip(pending_indices, encoded, strict=True):
                results[index] = vector
            _core_symbol("machina.core.provider", "audit_log")(
                "rag_embed_complete", model=self._model_id, count=len(encoded)
            )
        return results  # type: ignore[return-value]

    async def close(self) -> None:
        """Unload the ONNX model; the next embed call reloads from cache."""
        with self._load_lock:
            self._model = None


@dataclass(frozen=True)
class EmbeddingSettings:
    """Resolved embedding backend choice (config-only; never loads a model)."""

    kind: str  # "openai" | "onnx" | "local" | "none"
    model: str = ""
    base_url: str = ""
    backend: str = "auto"  # configured embedding_backend value
    reason: str = ""  # why unavailable when kind == "none"


def local_embedder_available() -> bool:
    """Whether the optional model2vec runtime is importable."""
    try:
        from importlib.util import find_spec

        return find_spec("model2vec") is not None
    except Exception:
        return False


def onnx_embedder_available() -> bool:
    """Whether the optional fastembed runtime (in-process ONNX) is importable."""
    try:
        from importlib.util import find_spec

        return find_spec("fastembed") is not None
    except Exception:
        return False


def _openai_api_model(model: str) -> bool:
    """Whether *model* is an OpenAI-API embedding id (R18).

    These ids cannot be served by the local runtimes: fastembed rejects
    them at load and model2vec cannot fetch them, so ``auto`` must keep an
    existing API configuration on the API backend.
    """
    return model in OpenAIEmbedder.MODEL_DIMENSIONS or model.startswith("text-embedding-")


#: model2vec static models share this publisher prefix. fastembed serves a
#: subset (e.g. minishlab/potion-base-8M); the rest (potion-base-4M) are
#: rejected at load, which is what the compatibility check below catches.
_MODEL2VEC_PREFIX = "minishlab/"


def _fastembed_supports_model(model: str) -> bool | None:
    """fastembed's own supported-model list, or None when unverifiable.

    Reading the list is metadata-only (no weights, no network); the import
    only happens when an explicit local model needs verification.
    """
    try:
        from fastembed import TextEmbedding

        supported = {entry.get("model") for entry in TextEmbedding.list_supported_models()}
    except Exception:
        return None
    return model in supported


def _model_fits_onnx(model: str) -> bool:
    """Whether the fastembed ONNX backend can serve the configured *model* (R18).

    An empty model means the backend default, which always fits. OpenAI API
    ids never fit. Published model2vec ids are checked against fastembed's
    supported list so potion-base-8M (supported) still routes to ONNX while
    potion-base-4M does not. Unknown ids keep the previous precedence
    (fastembed verifies them at load).
    """
    if not model:
        return True
    if _openai_api_model(model):
        return False
    if model.startswith(_MODEL2VEC_PREFIX):
        return _fastembed_supports_model(model) is True
    return True


def _model_fits_local(model: str) -> bool:
    """Whether the model2vec runtime could plausibly serve *model* (R18)."""
    return not _openai_api_model(model)


def _config_value(key: str, default: str = "") -> str:
    try:
        from machina.core.config import config

        value = config.get(key)
        return default if value is None else str(value)
    except Exception:
        return default


def resolve_embedding_settings() -> EmbeddingSettings:
    """Resolve which embedding backend would serve dense retrieval.

    Selection matrix for ``embedding_backend``:

    - ``off``    → none (keyword-only, explicit).
    - ``onnx``   → local fastembed ONNX model when installed, else none.
    - ``local``  → local model2vec static model when installed, else none.
    - ``openai`` → OpenAI-compatible server; a key is required unless an
      explicit ``embedding_base_url`` names a keyless local server.
    - ``auto``   → an explicit ``embedding_base_url`` first (OpenAI-compatible
      local server, keyless allowed); then the local fastembed ONNX runtime;
      then the local model2vec runtime (local-first design rule: embedding
      compute stays on-host); then OpenAI/OpenRouter credentials as a
      fallback; else none.

    An explicit ``embedding_model`` is honored by whichever backend is
    selected — and, under ``auto``, checked for backend compatibility first
    (R18): OpenAI API ids and model2vec ids fastembed cannot serve
    (e.g. ``minishlab/potion-base-4M``) are not forwarded to the ONNX
    runtime, so a previously working model2vec/API configuration does not
    degrade to keyword-only. fastembed-supported ids (e.g.
    ``minishlab/potion-base-8M``) still route to ONNX. Availability is an
    import spec check only — no model loads.
    """
    backend = (_config_value("embedding_backend", "auto") or "auto").strip().lower()
    model = _config_value("embedding_model").strip()
    base_url = _config_value("embedding_base_url").strip()
    if backend not in ("auto", "local", "onnx", "openai", "off"):
        return EmbeddingSettings(
            "none", backend=backend, reason=f"unknown embedding_backend {backend!r}"
        )
    if backend == "off":
        return EmbeddingSettings(
            "none", backend=backend, reason="embedding_backend=off (dense retrieval disabled)"
        )
    if backend == "openai":
        if not (_config_value("openai_api_key") or _config_value("openrouter_api_key") or base_url):
            return EmbeddingSettings(
                "none", backend=backend, reason="embedding_backend=openai but no key or base_url"
            )
        return EmbeddingSettings(
            "openai",
            model=model or OpenAIEmbedder.DEFAULT_MODEL,
            base_url=base_url or OpenAIEmbedder.DEFAULT_BASE_URL,
            backend=backend,
        )
    if backend == "onnx":
        if not onnx_embedder_available():
            return EmbeddingSettings(
                "none",
                model=model or OnnxEmbedder.DEFAULT_MODEL,
                backend=backend,
                reason="embedding_backend=onnx but fastembed is not installed",
            )
        return EmbeddingSettings("onnx", model=model or OnnxEmbedder.DEFAULT_MODEL, backend=backend)
    if backend == "local":
        if not local_embedder_available():
            return EmbeddingSettings(
                "none",
                model=model or LocalEmbedder.DEFAULT_MODEL,
                backend=backend,
                reason="embedding_backend=local but model2vec is not installed",
            )
        return EmbeddingSettings(
            "local", model=model or LocalEmbedder.DEFAULT_MODEL, backend=backend
        )
    # auto
    if base_url:
        return EmbeddingSettings(
            "openai",
            model=model or OpenAIEmbedder.DEFAULT_MODEL,
            base_url=base_url,
            backend=backend,
        )
    if onnx_embedder_available() and _model_fits_onnx(model):
        return EmbeddingSettings("onnx", model=model or OnnxEmbedder.DEFAULT_MODEL, backend=backend)
    if local_embedder_available() and _model_fits_local(model):
        return EmbeddingSettings(
            "local", model=model or LocalEmbedder.DEFAULT_MODEL, backend=backend
        )
    if _config_value("openai_api_key") or _config_value("openrouter_api_key"):
        return EmbeddingSettings(
            "openai",
            model=model or OpenAIEmbedder.DEFAULT_MODEL,
            base_url=OpenAIEmbedder.DEFAULT_BASE_URL,
            backend=backend,
        )
    if model:
        return EmbeddingSettings(
            "none",
            backend=backend,
            reason=(
                f"no embedding backend can serve embedding_model {model!r} "
                "(install the matching runtime or configure credentials)"
            ),
        )
    return EmbeddingSettings("none", backend=backend, reason="no embedding provider configured")


def build_embedder(settings: EmbeddingSettings | None = None) -> Embedder | None:
    """Build the configured embedder, or None for honest keyword-only fallback.

    Never raises for an unavailable provider: callers keep keyword-only
    retrieval and report the degradation honestly.
    """
    resolved = settings or resolve_embedding_settings()
    if resolved.kind == "none":
        logger.info("no embedding backend: %s", resolved.reason)
        return None
    try:
        if resolved.kind == "onnx":
            return OnnxEmbedder(model=resolved.model)
        if resolved.kind == "local":
            return LocalEmbedder(model=resolved.model)
        return OpenAIEmbedder(model=resolved.model, base_url=resolved.base_url)
    except Exception as e:
        logger.warning("embedding backend %r unavailable: %s", resolved.kind, e)
        return None


_shared_embedder: Embedder | None = None
_shared_embedder_identity: tuple | None = None
_shared_embedder_lock = threading.Lock()


def _embedder_identity(settings: EmbeddingSettings) -> tuple[str, str, str, str]:
    """Identity of the resolved backend; key digest catches credential rotation."""
    key_digest = ""
    if settings.kind == "openai":
        credential = _config_value("openai_api_key") or _config_value("openrouter_api_key")
        if credential:
            key_digest = hashlib.sha256(credential.encode("utf-8")).hexdigest()[:16]
    return (settings.kind, settings.model, settings.base_url, key_digest)


async def _close_quietly(embedder: Embedder) -> None:
    try:
        await embedder.close()
    except Exception as _boundary_error:
        from machina.observability.diagnostics import record_failure

        record_failure("rag.embedder.close", _boundary_error)


def _schedule_close(embedder: Embedder) -> None:
    """Close a rotated-out embedder on the running loop, if there is one."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(_close_quietly(embedder))


def get_shared_embedder() -> Embedder | None:
    """Process-wide cached embedder (built once; rebuilt on config change).

    Local model weights load once and are reused across queries instead of
    reloading per call. Unavailable backends are cached as None so repeated
    calls stay cheap; a configuration or credential change rebuilds the
    instance and the replaced one is closed. Use
    :func:`close_shared_embedder` at shutdown.
    """
    global _shared_embedder, _shared_embedder_identity
    settings = resolve_embedding_settings()
    identity = _embedder_identity(settings)
    with _shared_embedder_lock:
        if _shared_embedder_identity == identity:
            return _shared_embedder
        previous = _shared_embedder
        _shared_embedder = build_embedder(settings)
        _shared_embedder_identity = identity
    if previous is not None:
        _schedule_close(previous)
    return _shared_embedder


async def close_shared_embedder() -> None:
    """Drop and close the shared embedder (shutdown seam)."""
    global _shared_embedder, _shared_embedder_identity
    with _shared_embedder_lock:
        embedder, _shared_embedder = _shared_embedder, None
        _shared_embedder_identity = None
    if embedder is not None:
        await _close_quietly(embedder)
