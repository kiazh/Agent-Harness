"""Identity gate — AgentBelief, MemoryProvenance, and IdentityGate.

Gap 2: Multi-Agent Shared Memory with Identity Propagation Defense.

Each agent maintains an explicit identity model (beliefs, traits, values).
Before incorporating a shared memory, the IdentityGate validates:
  1. Consistency with existing beliefs
  2. Provenance via signed memory entries
  3. Semantic relevance via keyword overlap

Drift detection compares belief states before/after shared memory access.
Containment quarantines affected memories when drift is detected.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ah.core.provider import audit_log
from ah.db.connection import db
from ah.memory.models import MemoryEntry
from ah.security.secrets import get_secret

__all__ = [
    "AgentBelief",
    "IdentityGate",
    "MemoryProvenance",
    "ValidationResult",
]

logger = logging.getLogger(__name__)

# Simple sentiment words for consistency checking
_POSITIVE_WORDS = {
    "great",
    "good",
    "excellent",
    "amazing",
    "wonderful",
    "fantastic",
    "love",
    "like",
    "enjoy",
    "prefer",
    "best",
    "awesome",
    "brilliant",
    "positive",
    "beneficial",
    "useful",
    "valuable",
    "important",
}
_NEGATIVE_WORDS = {
    "terrible",
    "bad",
    "awful",
    "horrible",
    "hate",
    "dislike",
    "worst",
    "poor",
    "negative",
    "harmful",
    "useless",
    "worthless",
    "stupid",
    "wrong",
    "fail",
    "broken",
    "ugly",
    "disgusting",
}


@dataclass
class AgentBelief:
    """An agent's current belief state for identity consistency checking."""

    agent_id: str
    known_facts: dict[str, float]  # fact -> confidence
    traits: dict[str, float]  # trait -> strength
    values: dict[str, float]  # value -> importance
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict for JSONB storage."""
        return {
            "agent_id": self.agent_id,
            "known_facts": self.known_facts,
            "traits": self.traits,
            "values": self.values,
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentBelief:
        """Deserialize from dict (e.g., from JSONB column)."""
        updated_at = data.get("updated_at")
        if isinstance(updated_at, str):
            updated_at = datetime.fromisoformat(updated_at)
        elif updated_at is None:
            updated_at = datetime.now(UTC)
        return cls(
            agent_id=data["agent_id"],
            known_facts=data.get("known_facts", {}),
            traits=data.get("traits", {}),
            values=data.get("values", {}),
            updated_at=updated_at,
        )


@dataclass
class MemoryProvenance:
    """Signed provenance for a memory entry, enabling lineage tracking."""

    memory_id: uuid.UUID
    source_agent: str
    signature: str
    parent_memory_id: uuid.UUID | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @staticmethod
    def compute_signature(memory_id: uuid.UUID, content: str, source_agent: str) -> str:
        """Compute HMAC-SHA256 signature of memory content.

        Requires a separately configured key. An unkeyed digest does not
        authenticate provenance because anyone can recompute it.
        """
        key = get_secret("AGENT_HARNESS_PROVENANCE_KEY")
        if not key:
            raise ValueError("AGENT_HARNESS_PROVENANCE_KEY is required")
        data = f"{memory_id}:{content}:{source_agent}"
        return hmac.new(key.encode("utf-8"), data.encode("utf-8"), hashlib.sha256).hexdigest()

    def verify(self, memory_id: uuid.UUID, content: str) -> bool:
        """Verify the signature matches the given memory content."""
        try:
            expected = self.compute_signature(memory_id, content, self.source_agent)
        except ValueError:
            return False
        return hmac.compare_digest(self.signature, expected)


@dataclass
class ValidationResult:
    """Result of identity gate validation."""

    is_valid: bool
    confidence: float  # 0.0 to 1.0
    reason: str


@dataclass(frozen=True)
class BeliefTransition:
    """Recorded result of a proposed identity update."""

    agent_id: str
    from_version: int
    to_version: int
    drift_score: float
    accepted: bool
    quarantined_count: int


class IdentityGate:
    """Validates shared memories against an agent's belief model."""

    # Minimum keyword overlap for relevance
    RELEVANCE_THRESHOLD = 0.1
    # Minimum confidence for consistency
    CONSISTENCY_THRESHOLD = 0.3
    # Drift threshold for containment
    DRIFT_THRESHOLD = 0.5

    async def validate_incoming(
        self,
        agent_id: str,
        memory: MemoryEntry,
        provenance: MemoryProvenance | None = None,
    ) -> ValidationResult:
        """Validate an incoming shared memory against the agent's beliefs.

        Checks:
        1. Consistency with existing beliefs (sentiment alignment)
        2. Provenance via signed memory entries
        3. Semantic relevance via keyword overlap
        """
        # Shared memories require authenticated provenance even for an agent
        # with no established beliefs yet.
        if memory.agent_id != agent_id:
            try:
                provenance = provenance or await self._load_provenance(memory.id)
            except Exception as exc:
                logger.warning(
                    "Provenance lookup failed for memory %s (%s)", memory.id, type(exc).__name__
                )
                return ValidationResult(False, 0.0, "Provenance lookup unavailable")
            if (
                provenance is None
                or provenance.source_agent != memory.agent_id
                or not provenance.verify(memory.id, memory.content)
            ):
                return ValidationResult(False, 0.0, "Missing or invalid shared-memory provenance")

        # Load agent beliefs
        try:
            belief = await self._load_beliefs(agent_id)
        except Exception as exc:
            logger.warning("Belief lookup failed for agent %s (%s)", agent_id, type(exc).__name__)
            return ValidationResult(False, 0.0, "Belief lookup unavailable")
        if belief is not None:
            # Reject irrelevant or contradictory content before further lookup.
            relevance = self._compute_relevance(memory.content, belief)
            if relevance < self.RELEVANCE_THRESHOLD:
                return ValidationResult(
                    is_valid=False,
                    confidence=0.0,
                    reason=f"Irrelevant: no overlap with known facts (relevance={relevance:.2f})",
                )

            consistency = self._compute_consistency(memory.content, belief)
            if consistency < self.CONSISTENCY_THRESHOLD:
                return ValidationResult(
                    is_valid=False,
                    confidence=0.0,
                    reason=f"Inconsistent: contradicts existing beliefs (consistency={consistency:.2f})",
                )

        if memory.agent_id == agent_id:
            try:
                provenance = provenance or await self._load_provenance(memory.id)
            except Exception as exc:
                logger.warning(
                    "Provenance lookup failed for memory %s (%s)", memory.id, type(exc).__name__
                )
                return ValidationResult(False, 0.0, "Provenance lookup unavailable")
            if provenance is not None and (
                provenance.source_agent != memory.agent_id
                or not provenance.verify(memory.id, memory.content)
            ):
                return ValidationResult(False, 0.0, "Provenance verification failed")

        if belief is None:
            # No beliefs yet — accept with low confidence after provenance validation.
            return ValidationResult(
                is_valid=True,
                confidence=0.3,
                reason="No existing beliefs; accepting with low confidence",
            )

        # All checks passed
        confidence = (relevance + consistency) / 2.0
        return ValidationResult(
            is_valid=True,
            confidence=confidence,
            reason=f"Consistent and relevant (relevance={relevance:.2f}, consistency={consistency:.2f})",
        )

    def detect_drift(self, before: AgentBelief, after: AgentBelief) -> float:
        """Detect identity drift between two belief states.

        Returns a drift score from 0.0 (no drift) to 1.0 (complete drift).
        """
        drift_scores: list[float] = []

        # Compare known facts
        all_facts = set(before.known_facts) | set(after.known_facts)
        if all_facts:
            fact_drift = sum(
                abs(before.known_facts.get(f, 0.0) - after.known_facts.get(f, 0.0))
                for f in all_facts
            ) / len(all_facts)
            drift_scores.append(fact_drift)

        # Compare traits
        all_traits = set(before.traits) | set(after.traits)
        if all_traits:
            trait_drift = sum(
                abs(before.traits.get(t, 0.0) - after.traits.get(t, 0.0)) for t in all_traits
            ) / len(all_traits)
            drift_scores.append(trait_drift)

        # Compare values
        all_values = set(before.values) | set(after.values)
        if all_values:
            value_drift = sum(
                abs(before.values.get(v, 0.0) - after.values.get(v, 0.0)) for v in all_values
            ) / len(all_values)
            drift_scores.append(value_drift)

        if not drift_scores:
            return 0.0

        return sum(drift_scores) / len(drift_scores)

    async def contain_drift(self, agent_id: str, memory_ids: list[uuid.UUID]) -> bool:
        """Quarantine affected memories when drift is detected.

        Sets quarantined=True on the given memories to prevent further
        propagation of potentially corrupted beliefs.
        """
        if not memory_ids:
            return False

        try:
            await db.execute(
                """
                UPDATE memories
                SET quarantined = TRUE
                WHERE agent_id = $1 AND id = ANY($2::uuid[])
                """,
                agent_id,
                memory_ids,
            )
            audit_log(
                "drift_containment",
                agent_id=agent_id,
                memory_count=len(memory_ids),
            )
            logger.warning(
                "Drift containment: quarantined %d memories for agent %s",
                len(memory_ids),
                agent_id,
            )
            return True
        except Exception as exc:
            logger.error("Drift containment failed: %s", exc)
            return False

    async def _load_beliefs(self, agent_id: str) -> AgentBelief | None:
        """Load agent beliefs from the database."""
        row = await db.fetchrow(
            "SELECT agent_id, belief, version, updated_at FROM agent_beliefs WHERE agent_id = $1",
            agent_id,
        )
        if row is None:
            return None
        belief = row["belief"]
        if isinstance(belief, str):
            belief = json.loads(belief)
        return AgentBelief.from_dict(belief)

    async def save_beliefs(self, belief: AgentBelief) -> None:
        """Persist an explicitly supplied agent identity model."""
        await db.execute(
            """
            INSERT INTO agent_beliefs (agent_id, belief, version, updated_at)
            VALUES ($1, $2::jsonb, 1, now())
            ON CONFLICT (agent_id) DO UPDATE SET
                belief = EXCLUDED.belief,
                version = agent_beliefs.version + 1,
                updated_at = now()
            """,
            belief.agent_id,
            json.dumps(belief.to_dict()),
        )

    async def observe_transition(
        self,
        proposed: AgentBelief,
        *,
        causal_memory_ids: list[uuid.UUID] | None = None,
        threshold: float | None = None,
    ) -> BeliefTransition:
        """Record and gate a belief transition after consuming shared memories.

        A large change is rejected and the cited memories are quarantined in
        the same transaction. Callers must supply causal IDs; this method does
        not infer causality from a change in beliefs.
        """
        limit = self.DRIFT_THRESHOLD if threshold is None else threshold
        if not 0 < limit <= 1:
            raise ValueError("threshold must be in (0, 1]")
        for values in (proposed.known_facts, proposed.traits, proposed.values):
            if any(
                not isinstance(value, (int, float)) or not 0 <= value <= 1
                for value in values.values()
            ):
                raise ValueError("belief strengths must be between zero and one")
        causal_ids = list(dict.fromkeys(causal_memory_ids or []))
        async with db.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT belief, version FROM agent_beliefs WHERE agent_id = $1 FOR UPDATE",
                    proposed.agent_id,
                )
                before = None
                version = 0
                if row is not None:
                    raw = row["belief"]
                    before = AgentBelief.from_dict(json.loads(raw) if isinstance(raw, str) else raw)
                    version = row["version"]
                score = self.detect_drift(before, proposed) if before else 0.0
                contained = before is not None and score >= limit
                quarantined_count = 0
                if contained and causal_ids:
                    result = await conn.execute(
                        "UPDATE memories SET quarantined = TRUE "
                        "WHERE agent_id = $1 AND id = ANY($2::uuid[]) AND quarantined = FALSE",
                        proposed.agent_id,
                        causal_ids,
                    )
                    from ah.db.connection import parse_command_count

                    quarantined_count = parse_command_count(result)
                if not contained:
                    await conn.execute(
                        """
                        INSERT INTO agent_beliefs (agent_id, belief, version, updated_at)
                        VALUES ($1, $2::jsonb, 1, now())
                        ON CONFLICT (agent_id) DO UPDATE SET
                            belief = EXCLUDED.belief,
                            version = agent_beliefs.version + 1,
                            updated_at = now()
                        """,
                        proposed.agent_id,
                        json.dumps(proposed.to_dict()),
                    )
                await conn.execute(
                    """
                    INSERT INTO agent_belief_history
                        (agent_id, from_version, to_version, before_belief,
                         proposed_belief, drift_score, causal_memory_ids, contained)
                    VALUES ($1, $2, $3, $4::jsonb, $5::jsonb, $6, $7::uuid[], $8)
                    """,
                    proposed.agent_id,
                    version,
                    version if contained else version + 1,
                    json.dumps(before.to_dict()) if before else None,
                    json.dumps(proposed.to_dict()),
                    score,
                    causal_ids,
                    contained,
                )
        audit_log(
            "belief_transition",
            agent_id=proposed.agent_id,
            drift_score=score,
            contained=contained,
            quarantined_count=quarantined_count,
        )
        return BeliefTransition(
            agent_id=proposed.agent_id,
            from_version=version,
            to_version=version if contained else version + 1,
            drift_score=score,
            accepted=not contained,
            quarantined_count=quarantined_count,
        )

    async def _load_provenance(self, memory_id: uuid.UUID) -> MemoryProvenance | None:
        """Load memory provenance from the database."""
        row = await db.fetchrow(
            "SELECT memory_id, source_agent, signature, parent_memory_id, created_at FROM memory_provenance WHERE memory_id = $1",
            memory_id,
        )
        if row is None:
            return None
        return MemoryProvenance(
            memory_id=row["memory_id"],
            source_agent=row["source_agent"],
            signature=row["signature"],
            parent_memory_id=row["parent_memory_id"],
            created_at=row["created_at"],
        )

    def _compute_relevance(self, content: str, belief: AgentBelief) -> float:
        """Compute semantic relevance between memory content and agent beliefs.

        Uses simple keyword overlap with known facts.
        """
        content_lower = content.lower()
        if not belief.known_facts:
            return 0.5  # No facts to compare against

        # Check how many known facts appear in the content
        matching_facts = 0
        for fact in belief.known_facts:
            fact_lower = fact.lower()
            if fact_lower in content_lower:
                matching_facts += 1

        return matching_facts / len(belief.known_facts)

    def _compute_consistency(self, content: str, belief: AgentBelief) -> float:
        """Compute consistency between memory content and agent beliefs.

        Checks sentiment alignment: if the content expresses sentiment
        about a known fact, it should align with the fact's confidence.
        """
        content_lower = content.lower()
        content_words = set(content_lower.split())

        if not belief.known_facts:
            return 0.5  # No facts to compare against

        # Find facts mentioned in content
        mentioned_facts = []
        for fact, confidence in belief.known_facts.items():
            if fact.lower() in content_lower:
                mentioned_facts.append((fact, confidence))

        if not mentioned_facts:
            return 0.5  # No mentioned facts, neutral consistency

        # Check sentiment of content
        has_positive = any(w in content_words for w in _POSITIVE_WORDS)
        has_negative = any(w in content_words for w in _NEGATIVE_WORDS)

        if not has_positive and not has_negative:
            return 0.5  # Neutral sentiment

        # For each mentioned fact, check if sentiment aligns with confidence
        consistency_scores = []
        for fact, confidence in mentioned_facts:
            if has_positive and not has_negative:
                # Positive sentiment — should align with high confidence
                consistency_scores.append(confidence)
            elif has_negative and not has_positive:
                # Negative sentiment — should align with low confidence
                consistency_scores.append(1.0 - confidence)
            else:
                # Mixed sentiment — neutral
                consistency_scores.append(0.5)

        return sum(consistency_scores) / len(consistency_scores)


# Global singleton
identity_gate = IdentityGate()
