"""Memory domain models — typed dataclasses for long-term memory."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ah.core.exceptions import ValidationError


@dataclass
class MemoryEntry:
    """A durable long-term memory extracted from conversation.

    Follows the research-backed schema: category-based importance,
    Ebbinghaus decay tracking, and embedding-backed semantic search.
    """

    id: uuid.UUID
    session_id: uuid.UUID | None
    agent_id: str
    content: str
    category: str  # 'preference', 'decision', 'fact', 'event', 'transient'
    importance: float = 0.5
    created_at: datetime = field(default_factory=datetime.utcnow)
    last_accessed: datetime | None = None
    access_count: int = 0
    embedding: list[float] | None = None
    explicitly_important: bool = False
    base_strength: float = 1.0

    def __post_init__(self) -> None:
        """Validate category and clamp importance to [0, 1]."""
        valid_categories = {"preference", "decision", "fact", "event", "transient"}
        if self.category not in valid_categories:
            raise ValidationError(
                f"Invalid category '{self.category}'. Must be one of: {valid_categories}"
            )
        self.importance = max(0.0, min(1.0, self.importance))
        self.base_strength = max(0.0, min(1.0, self.base_strength))


@dataclass
class RetrievedMemory:
    """A memory returned from retrieval, with relevance score."""

    memory: MemoryEntry
    score: float
    source: str = "hybrid"  # "dense", "sparse", or "hybrid"
