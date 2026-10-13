"""Leaf seam — RAG pipeline interfaces (Stream C).

Typing.Protocol interfaces mirroring ``machina.rag.pipeline`` exactly so
core/consumers can depend on this seam instead of importing the
implementation. No redesign: names, parameters, and defaults match
``machina/rag/pipeline.py``; core-coupled types (``ContextChunk``) are
``Any`` here to keep this module importable without ``machina.core``.

Source anchors: machina/rag/pipeline.py: RAGPipeline.__init__ (85-93),
index_document (141-147), search (275-282).
"""

from __future__ import annotations

import uuid
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class RAGPipelineLike(Protocol):
    """Structural mirror of ``RAGPipeline`` consumer seam (pipeline.py)."""

    def __init__(
        self,
        embedder: Any | None = None,
        chunker: Any | None = None,
        reranker: Any | None = None,
        search: Any | None = None,
        loader: Any | None = None,
        config: Any | None = None,
    ) -> None:
        """Mirror of RAGPipeline.__init__ (pipeline.py:85-93)."""
        ...

    async def index_document(
        self,
        source: Any,
        session_id: uuid.UUID,
        agent_id: str = "harness",
        metadata: dict[str, Any] | None = None,
    ) -> list[Any]:
        """Mirror of index_document (pipeline.py:141-147).

        ``source`` is ``str | Document`` and returns
        ``list[ContextChunk]`` in the implementation; ``Any`` here to
        avoid ``machina.core`` at this seam.
        """
        ...

    async def search(
        self,
        query: str,
        session_id: uuid.UUID,
        top_k: int | None = None,
        rerank: bool = True,
        chunk_types: tuple[str, ...] | None = None,
    ) -> list[Any]:
        """Mirror of RAGPipeline.search (pipeline.py:275-282).

        Returns ``list[SearchResult]`` in the implementation.
        """
        ...
