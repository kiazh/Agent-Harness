"""Persona-conditioned memory with emotion topology.

Gap 3: Dual-stream memory architecture where factual memories are
interpreted through persona lenses, and retrieval is weighted by
emotional state using Plutchik's wheel of emotions.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ah.db.connection import db
from ah.memory.redaction import redact_secrets

__all__ = [
    "PersonaMemory",
    "EmotionTopology",
    "PersonaMemoryStore",
    "persona_memory_store",
]

logger = logging.getLogger(__name__)


@dataclass
class PersonaMemory:
    """Persona-conditioned interpretation of a factual memory.

    Each persona memory links to a factual memory via fact_id and
    provides an interpretation filtered through that persona's goals,
    values, and emotional state.
    """

    id: uuid.UUID
    fact_id: uuid.UUID
    persona_id: str
    interpretation: str
    emotional_valence: float = 0.0  # -1.0 (negative) to 1.0 (positive)
    emotional_arousal: float = 0.0  # 0.0 (calm) to 1.0 (excited)
    confidence: float = 0.5  # 0.0 to 1.0
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        """Clamp emotional values to valid ranges."""
        self.emotional_valence = max(-1.0, min(1.0, self.emotional_valence))
        self.emotional_arousal = max(0.0, min(1.0, self.emotional_arousal))
        self.confidence = max(0.0, min(1.0, self.confidence))


class EmotionTopology:
    """Maps emotional states to memory retrieval strategies.

    Uses Plutchik's wheel of emotions to weight memory retrieval
    based on the agent's current emotional state.
    """

    # Plutchik's wheel of emotions → retrieval weights
    EMOTION_PROFILES: dict[str, dict[str, float]] = {
        "joy": {
            "recency_weight": 0.8,
            "importance_weight": 0.4,
            "valence_bias": 0.6,
            "arousal_boost": 0.2,
        },
        "sadness": {
            "recency_weight": 0.3,
            "importance_weight": 0.7,
            "valence_bias": -0.4,
            "arousal_boost": -0.3,
        },
        "anger": {
            "recency_weight": 0.6,
            "importance_weight": 0.8,
            "valence_bias": -0.6,
            "arousal_boost": 0.5,
        },
        "fear": {
            "recency_weight": 0.7,
            "importance_weight": 0.6,
            "valence_bias": -0.3,
            "arousal_boost": 0.4,
        },
        "trust": {
            "recency_weight": 0.5,
            "importance_weight": 0.6,
            "valence_bias": 0.5,
            "arousal_boost": -0.2,
        },
        "disgust": {
            "recency_weight": 0.4,
            "importance_weight": 0.7,
            "valence_bias": -0.7,
            "arousal_boost": 0.3,
        },
        "anticipation": {
            "recency_weight": 0.7,
            "importance_weight": 0.5,
            "valence_bias": 0.3,
            "arousal_boost": 0.4,
        },
        "surprise": {
            "recency_weight": 0.9,
            "importance_weight": 0.5,
            "valence_bias": 0.0,
            "arousal_boost": 0.6,
        },
    }

    # Default profile for unknown emotions
    DEFAULT_PROFILE: dict[str, float] = {
        "recency_weight": 0.5,
        "importance_weight": 0.5,
        "valence_bias": 0.0,
        "arousal_boost": 0.0,
    }

    def get_profile(self, emotion: str) -> dict[str, float]:
        """Get the emotion profile for a given emotion."""
        return self.EMOTION_PROFILES.get(emotion.lower(), self.DEFAULT_PROFILE)

    def compute_score(
        self,
        persona_memory: PersonaMemory,
        emotion: str,
        now: datetime | None = None,
    ) -> float:
        """Compute retrieval score for a persona memory given an emotional state.

        Score combines:
        - Base confidence of the persona memory
        - Valence alignment with emotion
        - Arousal alignment with emotion
        - Recency boost
        """
        profile = self.get_profile(emotion)
        now = now or datetime.now(UTC)

        # Base score from confidence
        score = persona_memory.confidence * 0.3

        # Valence alignment: how well the memory's valence matches the emotion's bias
        valence_bias = profile["valence_bias"]
        valence_alignment = 1.0 - abs(persona_memory.emotional_valence - valence_bias)
        score += valence_alignment * 0.3

        # Arousal alignment: how well the memory's arousal matches the emotion's arousal boost
        arousal_boost = profile["arousal_boost"]
        # Normalize arousal_boost from [-1, 1] to [0, 1] for comparison
        target_arousal = (arousal_boost + 1.0) / 2.0
        arousal_alignment = 1.0 - abs(persona_memory.emotional_arousal - target_arousal)
        score += arousal_alignment * 0.2

        # Recency boost
        age_days = (now - persona_memory.created_at).total_seconds() / 86400.0
        recency_score = max(0.0, 1.0 - age_days / 30.0)
        score += recency_score * profile["recency_weight"] * 0.2

        return max(0.0, min(1.0, score))

    def retrieve_for_emotion(
        self,
        memories: list[PersonaMemory],
        emotion: str,
        limit: int = 5,
        now: datetime | None = None,
    ) -> list[tuple[PersonaMemory, float]]:
        """Retrieve persona memories weighted by emotional state.

        Returns list of (PersonaMemory, score) tuples sorted by score descending.
        """
        if not memories:
            return []

        scored = [(mem, self.compute_score(mem, emotion, now)) for mem in memories]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:limit]


class PersonaMemoryStore:
    """CRUD for persona memories stored in PostgreSQL."""

    async def add_persona(
        self,
        fact_id: uuid.UUID,
        persona_id: str,
        interpretation: str,
        emotional_valence: float = 0.0,
        emotional_arousal: float = 0.0,
        confidence: float = 0.5,
    ) -> PersonaMemory:
        """Add a new persona memory."""
        interpretation = redact_secrets(interpretation).text
        emotional_valence = max(-1.0, min(1.0, emotional_valence))
        emotional_arousal = max(0.0, min(1.0, emotional_arousal))
        confidence = max(0.0, min(1.0, confidence))
        row = await db.fetchrow(
            """
            INSERT INTO persona_memories (
                fact_id, persona_id, interpretation,
                emotional_valence, emotional_arousal, confidence
            )
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id, fact_id, persona_id, interpretation,
                      emotional_valence, emotional_arousal, confidence,
                      created_at, updated_at
            """,
            fact_id,
            persona_id,
            interpretation,
            emotional_valence,
            emotional_arousal,
            confidence,
        )
        return self._row_to_persona(row)

    async def get_persona(self, persona_id: uuid.UUID) -> PersonaMemory | None:
        """Get a persona memory by ID."""
        row = await db.fetchrow(
            """
            SELECT id, fact_id, persona_id, interpretation,
                   emotional_valence, emotional_arousal, confidence,
                   created_at, updated_at
            FROM persona_memories WHERE id = $1
            """,
            persona_id,
        )
        if row is None:
            return None
        return self._row_to_persona(row)

    async def search_persona(
        self,
        fact_id: uuid.UUID | None = None,
        persona_id: str | None = None,
        limit: int = 20,
    ) -> list[PersonaMemory]:
        """Search persona memories with optional filters."""
        conditions = []
        params: list[Any] = []
        param_idx = 1

        if fact_id is not None:
            conditions.append(f"fact_id = ${param_idx}")
            params.append(fact_id)
            param_idx += 1

        if persona_id is not None:
            conditions.append(f"persona_id = ${param_idx}")
            params.append(persona_id)
            param_idx += 1

        where_clause = " AND ".join(conditions) if conditions else "TRUE"
        params.append(limit)

        rows = await db.fetch(
            f"""
            SELECT id, fact_id, persona_id, interpretation,
                   emotional_valence, emotional_arousal, confidence,
                   created_at, updated_at
            FROM persona_memories
            WHERE {where_clause}
            ORDER BY confidence DESC, created_at DESC
            LIMIT ${param_idx}
            """,  # nosec B608 # Fixed SQL fragments; external values use bound parameters.
            *params,
        )
        return [self._row_to_persona(row) for row in rows]

    async def search_persona_for_facts(
        self,
        fact_ids: list[uuid.UUID],
        persona_id: str | None = None,
        limit_per_fact: int = 10,
    ) -> list[PersonaMemory]:
        """Fetch up to ``limit_per_fact`` interpretations for each fact in one query."""
        if not fact_ids:
            return []
        rows = await db.fetch(
            """
            WITH ranked AS (
                SELECT id, fact_id, persona_id, interpretation,
                       emotional_valence, emotional_arousal, confidence,
                       created_at, updated_at,
                       row_number() OVER (
                           PARTITION BY fact_id ORDER BY confidence DESC, created_at DESC
                       ) AS row_num
                FROM persona_memories
                WHERE fact_id = ANY($1::uuid[])
                  AND ($2::text IS NULL OR persona_id = $2)
            )
            SELECT id, fact_id, persona_id, interpretation,
                   emotional_valence, emotional_arousal, confidence,
                   created_at, updated_at
            FROM ranked WHERE row_num <= $3
            ORDER BY fact_id, row_num
            """,
            fact_ids,
            persona_id,
            limit_per_fact,
        )
        return [self._row_to_persona(row) for row in rows]

    async def update_persona(
        self,
        persona_id: uuid.UUID,
        interpretation: str | None = None,
        emotional_valence: float | None = None,
        emotional_arousal: float | None = None,
        confidence: float | None = None,
    ) -> PersonaMemory | None:
        """Update a persona memory."""
        # Build dynamic update
        updates = []
        params: list[Any] = []
        param_idx = 1

        if interpretation is not None:
            interpretation = redact_secrets(interpretation).text
            updates.append(f"interpretation = ${param_idx}")
            params.append(interpretation)
            param_idx += 1

        if emotional_valence is not None:
            emotional_valence = max(-1.0, min(1.0, emotional_valence))
            updates.append(f"emotional_valence = ${param_idx}")
            params.append(emotional_valence)
            param_idx += 1

        if emotional_arousal is not None:
            emotional_arousal = max(0.0, min(1.0, emotional_arousal))
            updates.append(f"emotional_arousal = ${param_idx}")
            params.append(emotional_arousal)
            param_idx += 1

        if confidence is not None:
            confidence = max(0.0, min(1.0, confidence))
            updates.append(f"confidence = ${param_idx}")
            params.append(confidence)
            param_idx += 1

        if not updates:
            return await self.get_persona(persona_id)

        updates.append("updated_at = now()")
        params.append(persona_id)

        row = await db.fetchrow(
            f"""
            UPDATE persona_memories
            SET {", ".join(updates)}
            WHERE id = ${param_idx}
            RETURNING id, fact_id, persona_id, interpretation,
                      emotional_valence, emotional_arousal, confidence,
                      created_at, updated_at
            """,  # nosec B608 # Fixed SQL fragments; external values use bound parameters.
            *params,
        )
        if row is None:
            return None
        return self._row_to_persona(row)

    async def delete_persona(self, persona_id: uuid.UUID) -> bool:
        """Delete a persona memory by ID."""
        result = await db.execute(
            "DELETE FROM persona_memories WHERE id = $1",
            persona_id,
        )
        return result != "DELETE 0"

    def _row_to_persona(self, row: Any) -> PersonaMemory:
        """Convert a database row to a PersonaMemory."""
        return PersonaMemory(
            id=row["id"],
            fact_id=row["fact_id"],
            persona_id=row["persona_id"],
            interpretation=row["interpretation"],
            emotional_valence=row["emotional_valence"],
            emotional_arousal=row["emotional_arousal"],
            confidence=row["confidence"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


# Global singleton
persona_memory_store = PersonaMemoryStore()
