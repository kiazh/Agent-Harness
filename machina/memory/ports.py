"""Leaf seam — memory retriever interfaces (Stream C).

Typing.Protocol interfaces mirroring ``machina.memory.retriever`` exactly
so core/consumers can depend on this seam instead of importing the
implementation. No redesign: names, parameters, and defaults match
``machina/memory/retriever.py``; core-coupled types (``LLMProvider``) and
return rows are ``Any`` here to keep this module importable without
``machina.core``.

Source anchors: machina/memory/retriever.py: MemoryRetriever.__init__
(47-65), retrieve (97-109), retrieve_by_embedding (264-274).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class MemoryRetrieverLike(Protocol):
    """Structural mirror of ``MemoryRetriever`` read seam (retriever.py)."""

    def __init__(
        self,
        store: Any | None = None,
        llm_provider: Any | None = None,
        top_k: int = 5,
        rerank: bool = True,
        persona_store: Any | None = None,
        persona_enabled: bool | None = None,
        default_emotion: str | None = None,
    ) -> None:
        """Mirror of MemoryRetriever.__init__ (retriever.py:47-56)."""
        ...

    async def retrieve(
        self,
        query: str,
        agent_id: str | None = None,
        category: str | None = None,
        date_range: tuple[datetime, datetime] | None = None,
        query_embedding: list[float] | None = None,
        session_id: uuid.UUID | None = None,
        emotion: str | None = None,
        persona_id: str | None = None,
        persona_enabled: bool | None = None,
        default_emotion: str | None = None,
    ) -> list[Any]:
        """Mirror of MemoryRetriever.retrieve (retriever.py:97-109).

        Returns ``list[RetrievedMemory]`` in the implementation; ``Any``
        here to avoid importing memory models through ``machina.core``.
        """
        ...

    async def retrieve_by_embedding(
        self,
        embedding: list[float],
        agent_id: str | None = None,
        category: str | None = None,
        limit: int = 5,
        emotion: str | None = None,
        persona_id: str | None = None,
        persona_enabled: bool | None = None,
        default_emotion: str | None = None,
    ) -> list[Any]:
        """Mirror of retrieve_by_embedding (retriever.py:264-274)."""
        ...
