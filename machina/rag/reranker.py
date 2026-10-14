"""Reranking — cross-encoder reranker interface.

Backends: Cohere (managed API, opt-in), local ONNX cross-encoders via
fastembed (zero-API, local-first), a local llama.cpp ``/v1/rerank`` HTTP
server, and the passthrough identity fallback. Local backends never run at
import or construction unless explicitly built; failures degrade to
``score=None`` passthrough, so the pipeline can report an honest ``fallback``
status without losing retrieval order.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib as _importlib
import importlib.util as _importlib_util
import logging
import math
import sys as _sys
import threading
from abc import ABC, abstractmethod
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


@dataclass
class RerankResult:
    """A reranked result; None signals passthrough, without a learned score."""

    index: int
    score: float | None
    text: str = ""


class Reranker(ABC):
    """Abstract interface for cross-encoder rerankers."""

    #: Honest component identity for status reporting (values match
    #: ``RAGPipeline.reranker_identity()``): "cohere", "local-onnx",
    #: "local-http", "passthrough" or "unknown" for foreign implementations.
    backend: str = "unknown"

    @abstractmethod
    async def rerank(
        self,
        query: str,
        documents: list[str],
        top_k: int = 10,
    ) -> list[RerankResult]:
        """Rerank documents by relevance to the query.

        Returns the top_k documents sorted by relevance (highest first).
        """
        raise NotImplementedError

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Return the reranker model name."""
        raise NotImplementedError


class CohereReranker(Reranker):
    """Cohere Rerank 3.5 — managed API reranker.

    Fast (~85ms for 100 docs), supports English + multilingual.
    Default model: rerank-v3.5
    """

    backend = "cohere"

    DEFAULT_MODEL = "rerank-v3.5"
    DEFAULT_BASE_URL = "https://api.cohere.com/v2"
    DEFAULT_TIMEOUT = 30.0

    def __init__(
        self,
        api_key: str | None = None,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.api_key = (
            api_key or _core_symbol("machina.core.config", "config").get("cohere_api_key") or ""
        )
        if not self.api_key:
            raise _core_symbol("machina.core.exceptions", "ProviderError")("COHERE_API_KEY not set")
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )

    @property
    def model_name(self) -> str:
        return self._model

    async def rerank(
        self,
        query: str,
        documents: list[str],
        top_k: int = 10,
    ) -> list[RerankResult]:
        """Rerank documents using Cohere Rerank API.

        Returns top_k results sorted by relevance score (highest first).
        """
        if not documents:
            return []

        # Cohere has a max of 100 documents per request
        if len(documents) > 100:
            logger.warning(
                "Cohere Rerank supports max 100 documents, truncating from %d",
                len(documents),
            )
            documents = documents[:100]

        _core_symbol("machina.core.provider", "audit_log")(
            "rag_rerank_start",
            model=self._model,
            query=query[:100],
            doc_count=len(documents),
            top_k=top_k,
        )

        try:
            resp = await self._client.post(
                "/rerank",
                json={
                    "model": self._model,
                    "query": query,
                    "documents": documents,
                    "top_n": min(top_k, len(documents)),
                },
            )
            resp.raise_for_status()
            data = resp.json()
            results = []
            seen = set()
            for item in data["results"]:
                index, score = item["index"], item["relevance_score"]
                if (
                    type(index) is not int
                    or not 0 <= index < len(documents)
                    or index in seen
                    or type(score) not in (int, float)
                    or not math.isfinite(score)
                ):
                    raise ValueError("Invalid reranker index or score")
                seen.add(index)
                results.append(RerankResult(index, float(score), documents[index]))
            if not results:
                raise ValueError("Empty reranker response")
            results.sort(key=lambda r: r.score, reverse=True)
        except Exception as e:
            _core_symbol("machina.core.provider", "audit_log")(
                "rag_rerank_error", model=self._model, error=str(e)
            )
            logger.warning("Reranking unavailable; preserving retrieval ranking: %s", e)
            return [
                RerankResult(index=i, score=None, text=doc)
                for i, doc in enumerate(documents[:top_k])
            ]

        _core_symbol("machina.core.provider", "audit_log")(
            "rag_rerank_complete",
            model=self._model,
            result_count=len(results),
        )

        return results[:top_k]

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()


class HttpReranker(Reranker):
    """Local HTTP reranker — llama.cpp ``llama-server --reranking``.

    Posts to the Cohere/Jina-shaped ``/v1/rerank`` endpoint (falling back to
    ``/rerank`` on 404) exposed by llama.cpp with a reranker model such as
    ``bge-reranker-v2-m3``. No credentials: the endpoint is expected to be
    loopback/local. An unreachable or misbehaving server degrades to
    passthrough (``score=None``) with an audit event, exactly like Cohere.
    """

    backend = "local-http"
    DEFAULT_TIMEOUT = 10.0

    def __init__(
        self,
        url: str,
        model: str = "",
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        url = str(url or "").strip()
        if not url:
            raise ValueError("HttpReranker requires a non-empty base URL")
        self._url = url.rstrip("/")
        self._model = model
        self._timeout = timeout
        self._client = httpx.AsyncClient(
            base_url=self._url,
            headers={"Content-Type": "application/json"},
            timeout=timeout,
        )

    @property
    def model_name(self) -> str:
        return self._model or "local-http"

    async def rerank(
        self,
        query: str,
        documents: list[str],
        top_k: int = 10,
    ) -> list[RerankResult]:
        """Rerank documents through the local HTTP endpoint."""
        if not documents:
            return []

        # Bound the payload like Cohere; local servers are fast but should
        # not receive unbounded candidate lists.
        if len(documents) > 100:
            logger.warning(
                "Local HTTP rerank capped at 100 documents, truncating from %d",
                len(documents),
            )
            documents = documents[:100]

        _core_symbol("machina.core.provider", "audit_log")(
            "rag_rerank_start",
            model=self.model_name,
            query=query[:100],
            doc_count=len(documents),
            top_k=top_k,
        )

        try:
            payload = {
                "query": query,
                "documents": documents,
                "top_n": min(top_k, len(documents)),
            }
            if self._model:
                payload["model"] = self._model
            resp = await self._client.post("/v1/rerank", json=payload)
            if resp.status_code == 404:
                # Older llama.cpp builds expose /rerank only.
                resp = await self._client.post("/rerank", json=payload)
            resp.raise_for_status()
            data = resp.json()
            results = []
            seen = set()
            for item in data["results"]:
                index = item["index"]
                # llama.cpp has used both spellings across builds.
                score = item.get("relevance_score", item.get("score"))
                if (
                    type(index) is not int
                    or not 0 <= index < len(documents)
                    or index in seen
                    or type(score) not in (int, float)
                    or not math.isfinite(score)
                ):
                    raise ValueError("Invalid reranker index or score")
                seen.add(index)
                results.append(RerankResult(index, float(score), documents[index]))
            if not results:
                raise ValueError("Empty reranker response")
            results.sort(key=lambda r: r.score, reverse=True)
        except Exception as e:
            _core_symbol("machina.core.provider", "audit_log")(
                "rag_rerank_error", model=self.model_name, error=str(e)
            )
            logger.warning("Reranking unavailable; preserving retrieval ranking: %s", e)
            return [
                RerankResult(index=i, score=None, text=doc)
                for i, doc in enumerate(documents[:top_k])
            ]

        _core_symbol("machina.core.provider", "audit_log")(
            "rag_rerank_complete",
            model=self.model_name,
            result_count=len(results),
        )

        return results[:top_k]

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()


class LocalOnnxReranker(Reranker):
    """In-process ONNX cross-encoder reranker (fastembed, no torch).

    Default model: ``Xenova/ms-marco-MiniLM-L-12-v2`` (Apache-2.0, ~120 MB
    ONNX). The ``fastembed`` runtime and the model weights load lazily on the
    first rerank call — never at import or construction — and the loaded
    model is reused for every later call on the instance. Construction is
    cheap and network-free; a missing runtime/model surfaces as a passthrough
    (``score=None``) fallback with an audit event, keeping retrieval honest
    and ordered. Multilingual upgrade path without code changes:
    ``MACHINA_RERANKER_LOCAL_MODEL=BAAI/bge-reranker-v2-m3-int8`` (~0.6 GB).
    """

    backend = "local-onnx"
    DEFAULT_MODEL = "Xenova/ms-marco-MiniLM-L-12-v2"
    DEFAULT_BATCH_SIZE = 64

    def __init__(
        self,
        model: str | None = None,
        cache_dir: str | None = None,
        threads: int | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        self._model_id = (model or "").strip() or self.DEFAULT_MODEL
        self._cache_dir = cache_dir
        self._threads = threads
        self._batch_size = batch_size
        self._model = None
        self._load_lock = threading.Lock()

    @property
    def model_name(self) -> str:
        return self._model_id

    def _load_model(self):
        """Import/load fastembed and the ONNX weights once (thread-safe)."""
        if self._model is None:
            with self._load_lock:
                if self._model is None:
                    from fastembed.rerank.cross_encoder import TextCrossEncoder

                    kwargs: dict = {}
                    if self._cache_dir:
                        kwargs["cache_dir"] = self._cache_dir
                    if self._threads:
                        kwargs["threads"] = self._threads
                    self._model = TextCrossEncoder(self._model_id, **kwargs)
        return self._model

    def _rerank_sync(self, query: str, documents: list[str], top_k: int) -> list[RerankResult]:
        """Blocking rerank (runs on a worker thread); validates before sorting."""
        model = self._load_model()
        scores = list(model.rerank(query, documents, batch_size=self._batch_size))
        if len(scores) != len(documents):
            raise ValueError(
                f"reranker returned {len(scores)} scores for {len(documents)} documents"
            )
        for score in scores:
            if type(score) not in (int, float) or not math.isfinite(score):
                raise ValueError("Invalid reranker score")
        order = sorted(range(len(documents)), key=lambda i: scores[i], reverse=True)[:top_k]
        return [RerankResult(i, float(scores[i]), documents[i]) for i in order]

    async def rerank(
        self,
        query: str,
        documents: list[str],
        top_k: int = 10,
    ) -> list[RerankResult]:
        """Rerank documents with the local ONNX cross-encoder."""
        if not documents:
            return []

        if len(documents) > 100:
            logger.warning(
                "Local ONNX rerank capped at 100 documents, truncating from %d",
                len(documents),
            )
            documents = documents[:100]

        _core_symbol("machina.core.provider", "audit_log")(
            "rag_rerank_start",
            model=self._model_id,
            query=query[:100],
            doc_count=len(documents),
            top_k=top_k,
        )

        try:
            results = await asyncio.to_thread(
                self._rerank_sync, query, documents, min(top_k, len(documents))
            )
        except Exception as e:
            _core_symbol("machina.core.provider", "audit_log")(
                "rag_rerank_error", model=self._model_id, error=str(e)
            )
            logger.warning("Local reranking unavailable; preserving retrieval ranking: %s", e)
            return [
                RerankResult(index=i, score=None, text=doc)
                for i, doc in enumerate(documents[:top_k])
            ]

        _core_symbol("machina.core.provider", "audit_log")(
            "rag_rerank_complete",
            model=self._model_id,
            result_count=len(results),
        )

        return results[:top_k]

    async def close(self) -> None:
        """Unload the ONNX model; the next rerank call reloads from cache."""
        with self._load_lock:
            self._model = None


class IdentityReranker(Reranker):
    """No-op reranker that returns documents in original order.

    Used for testing, and as the honest fallback whenever no reranker
    backend is configured/available. ``reason`` records why reranking is
    off (empty for deliberate test/no-op construction).
    """

    backend = "passthrough"

    def __init__(self, reason: str = "") -> None:
        self._reason = reason

    @property
    def reason(self) -> str:
        return self._reason

    @property
    def model_name(self) -> str:
        return "identity"

    async def rerank(
        self,
        query: str,
        documents: list[str],
        top_k: int = 10,
    ) -> list[RerankResult]:
        """Return documents in original order (no reranking)."""
        return [
            RerankResult(index=i, score=None, text=doc) for i, doc in enumerate(documents[:top_k])
        ]


@dataclass(frozen=True)
class RerankerSettings:
    """Resolved reranker backend choice (config-only; never loads a model)."""

    kind: str  # "cohere" | "local" | "http" | "none"
    model: str = ""
    url: str = ""
    backend: str = "auto"  # configured reranker_backend value
    reason: str = ""  # why unavailable when kind == "none"


def reranker_available() -> bool:
    """Whether the optional fastembed runtime (local ONNX) is importable."""
    try:
        return _importlib_util.find_spec("fastembed") is not None
    except Exception:
        return False


def _config_value(key: str, default: str = "") -> str:
    try:
        from machina.core.config import config

        value = config.get(key)
        return default if value is None else str(value)
    except Exception:
        return default


def resolve_reranker_selection() -> RerankerSettings:
    """Resolve which reranker backend would serve retrieval reranking.

    Selection matrix for ``reranker_backend``:

    - ``none``   → passthrough (explicit).
    - ``cohere`` → Cohere API; requires ``cohere_api_key``, else none.
    - ``local``  → fastembed ONNX cross-encoder when installed, else none.
    - ``http``   → local llama.cpp ``/v1/rerank``; requires
      ``reranker_http_url``, else none.
    - ``auto``   → local ONNX first (local-first design rule: zero-API
      compute stays on-host), then a configured Cohere key; else none.

    Availability is an import spec check only — no model loads.
    """
    backend = (_config_value("reranker_backend", "auto") or "auto").strip().lower()
    model = _config_value("reranker_local_model").strip()
    url = _config_value("reranker_http_url").strip()
    http_model = _config_value("reranker_http_model").strip()
    if backend not in ("auto", "cohere", "local", "http", "none"):
        return RerankerSettings(
            "none", backend=backend, reason=f"unknown reranker_backend {backend!r}"
        )
    if backend == "none":
        return RerankerSettings(
            "none", backend=backend, reason="reranker_backend=none (passthrough)"
        )
    if backend == "cohere":
        if not _config_value("cohere_api_key"):
            return RerankerSettings(
                "none",
                backend=backend,
                reason="reranker_backend=cohere but COHERE_API_KEY is not set",
            )
        return RerankerSettings("cohere", model=CohereReranker.DEFAULT_MODEL, backend=backend)
    if backend == "local":
        if not reranker_available():
            return RerankerSettings(
                "none",
                model=model or LocalOnnxReranker.DEFAULT_MODEL,
                backend=backend,
                reason="reranker_backend=local but fastembed is not installed",
            )
        return RerankerSettings(
            "local", model=model or LocalOnnxReranker.DEFAULT_MODEL, backend=backend
        )
    if backend == "http":
        if not url:
            return RerankerSettings(
                "none",
                backend=backend,
                reason="reranker_backend=http but reranker_http_url is empty",
            )
        return RerankerSettings("http", model=http_model, url=url, backend=backend)
    # auto: local-first (zero-API design rule), then the Cohere key.
    if reranker_available():
        return RerankerSettings(
            "local", model=model or LocalOnnxReranker.DEFAULT_MODEL, backend=backend
        )
    if _config_value("cohere_api_key"):
        return RerankerSettings("cohere", model=CohereReranker.DEFAULT_MODEL, backend=backend)
    return RerankerSettings(
        "none",
        backend=backend,
        reason=(
            "no reranker backend available (install 'Machina[rerank]' for "
            "local ONNX reranking or set COHERE_API_KEY)"
        ),
    )


def build_reranker(settings: RerankerSettings | None = None) -> Reranker:
    """Build the configured reranker, or ``IdentityReranker`` as fallback.

    Never raises for an unavailable backend: callers keep retrieval order
    and status reporting stays honest (the passthrough reason is recorded).
    """
    try:
        resolved = settings or resolve_reranker_selection()
    except Exception as e:
        logger.warning("reranker selection failed: %s", e, exc_info=True)
        return IdentityReranker(reason=f"selection failed: {e}")
    if resolved.kind == "none":
        logger.info("no reranker backend: %s", resolved.reason)
        return IdentityReranker(reason=resolved.reason)
    try:
        if resolved.kind == "local":
            return LocalOnnxReranker(model=resolved.model)
        if resolved.kind == "http":
            return HttpReranker(url=resolved.url, model=resolved.model)
        return CohereReranker()
    except Exception as e:
        logger.warning("reranker backend %r unavailable: %s", resolved.kind, e, exc_info=True)
        return IdentityReranker(reason=f"{resolved.kind} unavailable: {e}")


def reranker_selection_identity(settings: RerankerSettings) -> tuple[str, str, str, str]:
    """Identity of the resolved backend; key digest catches credential rotation.

    The URL is normalized (trailing slash stripped) so identity comparison
    stays stable against the instance's own normalization.
    """
    key_digest = ""
    if settings.kind == "cohere":
        credential = _config_value("cohere_api_key")
        if credential:
            key_digest = hashlib.sha256(credential.encode("utf-8")).hexdigest()[:16]
    return (settings.kind, settings.model, settings.url.rstrip("/"), key_digest)
