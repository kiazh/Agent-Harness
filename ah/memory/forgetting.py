"""Forgetting model — Ebbinghaus exponential decay with importance modulation.

Formula:
    strength(t) = base_strength × e^(-λ_effective × Δt) + access_count × 0.1

Where:
    λ_effective = base_lambda × (1 - importance)
    base_lambda = ln(2) / half_life_days
"""

from __future__ import annotations

import math

from ah.memory.models import MemoryEntry, age_days

# Default half-life in days
DEFAULT_HALF_LIFE_DAYS = 14.0

# Access boost per retrieval
ACCESS_BOOST = 0.1

# Eviction threshold — memories below this strength are candidates for eviction
DEFAULT_EVICTION_THRESHOLD = 0.05


class ForgettingModel:
    """Ebbinghaus-inspired decay with importance modulation.

    High-importance memories decay slowly; low-importance memories decay fast.
    Each access boosts the memory's strength (spaced repetition effect).
    """

    def __init__(self, half_life_days: float = DEFAULT_HALF_LIFE_DAYS) -> None:
        self.base_lambda = math.log(2) / half_life_days  # ln(2) / half_life

    def current_strength(self, memory: MemoryEntry) -> float:
        """Compute current retrieval strength of a memory.

        Combines exponential decay with access count boost.
        """
        reference_time = memory.last_accessed or memory.created_at
        days_since_access = age_days(reference_time)

        # Importance-modulated decay: high importance = slow decay
        importance_factor = 1.0 - memory.importance
        effective_lambda = self.base_lambda * importance_factor

        # Exponential decay component
        decay = math.exp(-effective_lambda * days_since_access)
        decay_component = memory.base_strength * decay

        # Access boost component (spaced repetition)
        access_component = memory.access_count * ACCESS_BOOST

        return decay_component + access_component

    def should_forget(
        self, memory: MemoryEntry, threshold: float = DEFAULT_EVICTION_THRESHOLD
    ) -> bool:
        """Decide whether a memory should be evicted.

        Returns True if the memory's current strength is below the threshold.
        """
        return self.current_strength(memory) < threshold

    def get_decay_rate(self, memory: MemoryEntry) -> float:
        """Get the effective decay rate for a memory (per day)."""
        importance_factor = 1.0 - memory.importance
        return self.base_lambda * importance_factor

    def get_half_life(self, memory: MemoryEntry) -> float:
        """Get the effective half-life of a memory in days.

        For a memory with importance=0.9, half-life is 10× longer than base.
        For a memory with importance=0.1, half-life is 0.9× base.
        """
        decay_rate = self.get_decay_rate(memory)
        if decay_rate <= 0:
            return float("inf")
        return math.log(2) / decay_rate
