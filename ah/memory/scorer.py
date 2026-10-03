"""Importance scoring — multi-factor model for memory entries.

Implements the research-backed heuristic:
    V(m) = Σ wᵢ × fᵢ(m)

Factors: category base importance, explicit importance, recency boost,
access frequency boost.
"""

from __future__ import annotations

from ah.memory.models import MemoryEntry, age_days

# Category-based base importance weights (from research)
CATEGORY_WEIGHTS: dict[str, float] = {
    "preference": 0.9,
    "decision": 0.8,
    "fact": 0.7,
    "event": 0.5,
    "transient": 0.2,
}

# Recency decay window in days
RECENCY_WINDOW_DAYS = 30.0

# Access frequency normalization
FREQUENCY_NORMALIZATION = 10.0


class ImportanceScorer:
    """Heuristic importance scoring for memory entries.

    Score is in [0, 1]. Higher means more important.
    """

    def score(self, memory: MemoryEntry) -> float:
        """Return importance score in [0, 1].

        Combines:
        - Category base importance (dominant factor)
        - Explicit importance (user said "remember this")
        - Recency boost (decays over 30 days)
        - Access frequency boost (more accesses = more important)
        """
        scores: list[float] = []

        # Category-based base importance
        category_weight = CATEGORY_WEIGHTS.get(memory.category, 0.5)
        scores.append(category_weight)

        # User explicit importance (if user said "remember this")
        if memory.explicitly_important:
            scores.append(1.0)

        # Recency boost (decays over RECENCY_WINDOW_DAYS)
        days_old = int(age_days(memory.created_at))
        recency_score = max(0.0, 1.0 - days_old / RECENCY_WINDOW_DAYS)
        scores.append(recency_score * 0.3)

        # Access frequency boost
        freq_score = min(1.0, memory.access_count / FREQUENCY_NORMALIZATION)
        scores.append(freq_score * 0.2)

        return min(1.0, max(0.0, sum(scores) / len(scores)))

    def score_with_breakdown(self, memory: MemoryEntry) -> dict[str, float]:
        """Return importance score with factor breakdown for debugging."""
        category_weight = CATEGORY_WEIGHTS.get(memory.category, 0.5)
        days_old = int(age_days(memory.created_at))
        recency_score = max(0.0, 1.0 - days_old / RECENCY_WINDOW_DAYS)
        freq_score = min(1.0, memory.access_count / FREQUENCY_NORMALIZATION)

        return {
            "category": category_weight,
            "explicit": 1.0 if memory.explicitly_important else 0.0,
            "recency": recency_score * 0.3,
            "frequency": freq_score * 0.2,
            "total": self.score(memory),
        }
