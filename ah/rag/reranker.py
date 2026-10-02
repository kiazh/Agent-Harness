"""Reranking — cross-encoder reranker interface with Cohere implementation."""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import httpx

from ah.core.config import config
from ah.core.exceptions import ProviderError
from ah.core.provider import audit_log

logger = logging.getLogger(__name__)


@dataclass
class RerankResult:
    """A reranked result with relevance score."""

    index: int
    score: float
    text: str = ""


class Reranker(ABC):
    """Abstract interface for cross-encoder rerankers."""

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
        self.api_key = api_key or config.get("cohere_api_key") or ""
        if not self.api_key:
            raise ProviderError("COHERE_API_KEY not set")
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

        audit_log(
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
        except Exception as e:
            audit_log("rag_rerank_error", model=self._model, error=str(e))
            logger.error("Rerank API call failed: %s", e)
            # Fallback: return un-reranked results
            return [
                RerankResult(index=i, score=1.0 / (i + 1), text=doc)
                for i, doc in enumerate(documents[:top_k])
            ]

        results = []
        for item in data.get("results", []):
            results.append(RerankResult(
                index=item["index"],
                score=item["relevance_score"],
                text=documents[item["index"]],
            ))

        # Sort by score descending
        results.sort(key=lambda r: r.score, reverse=True)

        audit_log(
            "rag_rerank_complete",
            model=self._model,
            result_count=len(results),
        )

        return results[:top_k]

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()


class IdentityReranker(Reranker):
    """No-op reranker that returns documents in original order.

    Useful for testing or when reranking is not available.
    """

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
            RerankResult(index=i, score=1.0 / (i + 1), text=doc)
            for i, doc in enumerate(documents[:top_k])
        ]
