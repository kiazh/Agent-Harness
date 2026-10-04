"""Gap 4: RL action space + multi-level reward design.

Defines MemoryAction enum, MemoryState, MemoryDecision, and MemoryReward
for end-to-end RL training of joint memory management and task performance.

Research basis:
    - Memory-R1 (arXiv:2508.19828): PPO/GRPO for memory ops
    - ReMemR1: RL with Multi-Level Rewards (RLMLR)
    - Agentic Memory (Yu et al., 2026): step-wise GRPO
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ah.memory.models import MemoryEntry


class MemoryAction(Enum):
    """RL action space for memory operations."""

    STORE = "store"  # Add to memory
    UPDATE = "update"  # Update existing memory
    DELETE = "delete"  # Delete memory
    SUMMARIZE = "summarize"  # Consolidate into summary
    RETRIEVE = "retrieve"  # Search and inject
    NOOP = "noop"  # No memory operation


@dataclass
class MemoryState:
    """State representation for RL."""

    conversation: list[dict]  # recent messages
    current_memory: list[MemoryEntry]  # current memory state
    task: str  # current task/goal
    budget_remaining: int  # token budget


@dataclass
class MemoryDecision:
    """A memory operation decision."""

    action: MemoryAction
    target_id: uuid.UUID | None = None  # memory to update/delete
    content: str | None = None  # content to store
    importance: float = 0.0  # importance score if storing


@dataclass
class MemoryReward:
    """Multi-level reward for memory operations.

    Three reward levels:
        1. Step-level (dense): correctness of individual memory operations
        2. Trajectory-level (sparse): task completion
        3. Consistency (intermediate): persona consistency
    """

    # Reward weights
    STEP_WEIGHT: float = field(default=1.0, repr=False)
    TRAJECTORY_WEIGHT: float = field(default=1.0, repr=False)
    CONSISTENCY_WEIGHT: float = field(default=0.1, repr=False)

    def compute_step_reward(
        self,
        action: MemoryAction,
        state: MemoryState,
        outcome: Any,
    ) -> float:
        """Dense step-level reward for memory operation correctness.

        Args:
            action: The memory action that was taken.
            state: The state before the action.
            outcome: Dict with relevance/staleness/redundancy scores.

        Returns:
            Step reward value.
        """
        if action == MemoryAction.STORE:
            relevance = outcome.get("relevance", 0.0) if isinstance(outcome, dict) else 0.0
            redundancy = outcome.get("redundancy", 0.0) if isinstance(outcome, dict) else 0.0
            return relevance - redundancy
        elif action == MemoryAction.DELETE:
            staleness = outcome.get("staleness", 0.0) if isinstance(outcome, dict) else 0.0
            return staleness
        elif action == MemoryAction.UPDATE:
            relevance = outcome.get("relevance", 0.0) if isinstance(outcome, dict) else 0.0
            redundancy = outcome.get("redundancy", 0.0) if isinstance(outcome, dict) else 0.0
            return relevance - redundancy
        elif action == MemoryAction.RETRIEVE:
            relevance = outcome.get("relevance", 0.0) if isinstance(outcome, dict) else 0.0
            redundancy = outcome.get("redundancy", 0.0) if isinstance(outcome, dict) else 0.0
            return relevance - redundancy
        elif action == MemoryAction.SUMMARIZE:
            relevance = outcome.get("relevance", 0.0) if isinstance(outcome, dict) else 0.0
            redundancy = outcome.get("redundancy", 0.0) if isinstance(outcome, dict) else 0.0
            return relevance - redundancy
        elif action == MemoryAction.NOOP:
            return 0.0
        return 0.0

    def compute_trajectory_reward(
        self,
        task_success: bool,
        memory_operations: list[MemoryDecision],
    ) -> float:
        """Sparse trajectory-level reward for task completion.

        Args:
            task_success: Whether the task was completed successfully.
            memory_operations: List of memory decisions made during the trajectory.

        Returns:
            +1.0 for success, -0.1 for failure.
        """
        return 1.0 if task_success else -0.1

    def compute_consistency_reward(
        self,
        persona_consistency: float,
    ) -> float:
        """Intermediate persona-consistency reward.

        Args:
            persona_consistency: Persona consistency score in [0, 1].

        Returns:
            persona_consistency * CONSISTENCY_WEIGHT.
        """
        return persona_consistency * self.CONSISTENCY_WEIGHT

    def aggregate_rewards(
        self,
        step_rewards: list[float],
        trajectory_reward: float,
        consistency_reward: float,
    ) -> float:
        """Aggregate multi-level rewards into a single scalar.

        Args:
            step_rewards: List of step-level rewards.
            trajectory_reward: Trajectory-level reward.
            consistency_reward: Consistency reward.

        Returns:
            Total aggregated reward.
        """
        return (
            sum(step_rewards) * self.STEP_WEIGHT
            + trajectory_reward * self.TRAJECTORY_WEIGHT
            + consistency_reward
        )
