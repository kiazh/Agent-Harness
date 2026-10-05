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

        Weighted model (fixed weights, explicit bonus preserves explicit wins):
            base = 0.5 * category + 0.3 * recency + 0.2 * frequency
            if explicitly_important: base = 0.5 * base + 0.5 * 1.0
        Clamped to [0, 1]. For identical inputs an explicitly-important
        memory always scores higher (base < 1.0 implies 0.5*base+0.5 > base).
        """
        category_weight = CATEGORY_WEIGHTS.get(memory.category, 0.5)

        # Recency boost (decays over RECENCY_WINDOW_DAYS)
        days_old = age_days(memory.created_at)
        recency_score = max(0.0, 1.0 - days_old / RECENCY_WINDOW_DAYS)

        # Access frequency boost
        freq_score = min(1.0, memory.access_count / FREQUENCY_NORMALIZATION)

        base = 0.5 * category_weight + 0.3 * recency_score + 0.2 * freq_score
        if memory.explicitly_important:
            base = 0.5 * base + 0.5 * 1.0

        return min(1.0, max(0.0, base))

    def score_with_breakdown(self, memory: MemoryEntry) -> dict[str, float]:
        """Return importance score with factor breakdown for debugging."""
        category_weight = CATEGORY_WEIGHTS.get(memory.category, 0.5)
        days_old = age_days(memory.created_at)
        recency_score = max(0.0, 1.0 - days_old / RECENCY_WINDOW_DAYS)
        freq_score = min(1.0, memory.access_count / FREQUENCY_NORMALIZATION)

        cat_c = 0.5 * category_weight
        rec_c = 0.3 * recency_score
        freq_c = 0.2 * freq_score
        if memory.explicitly_important:
            explicit_c = 0.5 * 1.0
            # Blend factor: base halved, so halve the other components too.
            cat_c *= 0.5
            rec_c *= 0.5
            freq_c *= 0.5
        else:
            explicit_c = 0.0

        return {
            "category": cat_c,
            "explicit": explicit_c,
            "recency": rec_c,
            "frequency": freq_c,
            "total": self.score(memory),
        }
