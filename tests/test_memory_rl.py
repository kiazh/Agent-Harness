"""Tests for Gap 4: RL action space + multi-level reward design.

Covers MemoryAction enum, MemoryState, MemoryDecision, and MemoryReward
with step-level, trajectory-level, and consistency rewards.
"""
from __future__ import annotations

import uuid

import pytest

from ah.memory.rl import (
    MemoryAction,
    MemoryDecision,
    MemoryReward,
    MemoryState,
)

# ---------------------------------------------------------------------------
# MemoryAction Enum Tests
# ---------------------------------------------------------------------------


class TestMemoryActionEnum:
    """Tests for the MemoryAction enum."""

    def test_all_actions_defined(self):
        """Verify all six memory actions are defined."""
        expected = {"STORE", "UPDATE", "DELETE", "SUMMARIZE", "RETRIEVE", "NOOP"}
        actual = {a.name for a in MemoryAction}
        assert expected == actual

    def test_action_values(self):
        """Verify action string values."""
        assert MemoryAction.STORE.value == "store"
        assert MemoryAction.UPDATE.value == "update"
        assert MemoryAction.DELETE.value == "delete"
        assert MemoryAction.SUMMARIZE.value == "summarize"
        assert MemoryAction.RETRIEVE.value == "retrieve"
        assert MemoryAction.NOOP.value == "noop"

    def test_action_count(self):
        """Verify exactly 6 actions exist."""
        assert len(MemoryAction) == 6


# ---------------------------------------------------------------------------
# MemoryState Tests
# ---------------------------------------------------------------------------


class TestMemoryState:
    """Tests for MemoryState dataclass."""

    def test_create_basic(self):
        """Test creating a basic MemoryState."""
        state = MemoryState(
            conversation=[{"role": "user", "content": "hello"}],
            current_memory=[],
            task="test task",
            budget_remaining=1000,
        )
        assert state.conversation == [{"role": "user", "content": "hello"}]
        assert state.current_memory == []
        assert state.task == "test task"
        assert state.budget_remaining == 1000

    def test_create_with_memory_entries(self):
        """Test creating MemoryState with existing memory entries."""
        from ah.memory.models import MemoryEntry

        entry = MemoryEntry(
            id=uuid.uuid4(),
            session_id=None,
            agent_id="harness",
            content="Test memory",
            category="fact",
        )
        state = MemoryState(
            conversation=[],
            current_memory=[entry],
            task="test",
            budget_remaining=500,
        )
        assert len(state.current_memory) == 1
        assert state.current_memory[0].content == "Test memory"

    def test_create_empty(self):
        """Test creating an empty MemoryState."""
        state = MemoryState(
            conversation=[],
            current_memory=[],
            task="",
            budget_remaining=0,
        )
        assert state.conversation == []
        assert state.current_memory == []
        assert state.budget_remaining == 0


# ---------------------------------------------------------------------------
# MemoryDecision Tests
# ---------------------------------------------------------------------------


class TestMemoryDecision:
    """Tests for MemoryDecision dataclass."""

    def test_create_store_decision(self):
        """Test creating a STORE decision."""
        decision = MemoryDecision(
            action=MemoryAction.STORE,
            target_id=None,
            content="New fact",
            importance=0.8,
        )
        assert decision.action == MemoryAction.STORE
        assert decision.content == "New fact"
        assert decision.importance == 0.8

    def test_create_delete_decision(self):
        """Test creating a DELETE decision."""
        target = uuid.uuid4()
        decision = MemoryDecision(
            action=MemoryAction.DELETE,
            target_id=target,
            content=None,
            importance=0.0,
        )
        assert decision.action == MemoryAction.DELETE
        assert decision.target_id == target

    def test_create_noop_decision(self):
        """Test creating a NOOP decision."""
        decision = MemoryDecision(
            action=MemoryAction.NOOP,
            target_id=None,
            content=None,
            importance=0.0,
        )
        assert decision.action == MemoryAction.NOOP


# ---------------------------------------------------------------------------
# MemoryReward — Step-Level Tests
# ---------------------------------------------------------------------------


class TestStepReward:
    """Tests for step-level rewards."""

    @pytest.fixture
    def reward(self):
        return MemoryReward()

    def test_step_reward_store(self, reward):
        """Verify STORE action gets relevance score."""
        state = MemoryState(
            conversation=[{"role": "user", "content": "I like Python"}],
            current_memory=[],
            task="store preference",
            budget_remaining=1000,
        )
        outcome = {"relevance": 0.9, "redundancy": 0.1}
        r = reward.compute_step_reward(MemoryAction.STORE, state, outcome)
        assert r > 0
        assert r == pytest.approx(0.8)  # 0.9 - 0.1

    def test_step_reward_delete(self, reward):
        """Verify DELETE action gets staleness score."""
        state = MemoryState(
            conversation=[],
            current_memory=[],
            task="clean up",
            budget_remaining=1000,
        )
        outcome = {"staleness": 0.7}
        r = reward.compute_step_reward(MemoryAction.DELETE, state, outcome)
        assert r > 0
        assert r == pytest.approx(0.7)

    def test_step_reward_noop(self, reward):
        """Verify NOOP action gets zero reward."""
        state = MemoryState(
            conversation=[],
            current_memory=[],
            task="nothing",
            budget_remaining=1000,
        )
        r = reward.compute_step_reward(MemoryAction.NOOP, state, {})
        assert r == 0.0

    def test_step_reward_retrieve(self, reward):
        """Verify RETRIEVE action gets relevance-based reward."""
        state = MemoryState(
            conversation=[{"role": "user", "content": "What do I like?"}],
            current_memory=[],
            task="retrieve preference",
            budget_remaining=1000,
        )
        outcome = {"relevance": 0.85, "redundancy": 0.0}
        r = reward.compute_step_reward(MemoryAction.RETRIEVE, state, outcome)
        assert r > 0
        assert r == pytest.approx(0.85)

    def test_step_reward_update(self, reward):
        """Verify UPDATE action gets relevance-based reward."""
        state = MemoryState(
            conversation=[],
            current_memory=[],
            task="update memory",
            budget_remaining=1000,
        )
        outcome = {"relevance": 0.6, "redundancy": 0.2}
        r = reward.compute_step_reward(MemoryAction.UPDATE, state, outcome)
        assert r == pytest.approx(0.4)

    def test_step_reward_summarize(self, reward):
        """Verify SUMMARIZE action gets relevance-based reward."""
        state = MemoryState(
            conversation=[],
            current_memory=[],
            task="summarize",
            budget_remaining=1000,
        )
        outcome = {"relevance": 0.75, "redundancy": 0.0}
        r = reward.compute_step_reward(MemoryAction.SUMMARIZE, state, outcome)
        assert r == pytest.approx(0.75)


# ---------------------------------------------------------------------------
# MemoryReward — Trajectory-Level Tests
# ---------------------------------------------------------------------------


class TestTrajectoryReward:
    """Tests for trajectory-level rewards."""

    @pytest.fixture
    def reward(self):
        return MemoryReward()

    def test_trajectory_reward_success(self, reward):
        """Verify task success gives positive reward."""
        decisions = [
            MemoryDecision(
                action=MemoryAction.STORE,
                target_id=None,
                content="fact",
                importance=0.5,
            )
        ]
        r = reward.compute_trajectory_reward(task_success=True, memory_operations=decisions)
        assert r > 0
        assert r == pytest.approx(1.0)

    def test_trajectory_reward_failure(self, reward):
        """Verify task failure gives negative reward."""
        decisions = [
            MemoryDecision(
                action=MemoryAction.STORE,
                target_id=None,
                content="fact",
                importance=0.5,
            )
        ]
        r = reward.compute_trajectory_reward(task_success=False, memory_operations=decisions)
        assert r < 0
        assert r == pytest.approx(-0.1)

    def test_trajectory_reward_success_with_multiple_ops(self, reward):
        """Verify trajectory reward is independent of operation count."""
        decisions = [
            MemoryDecision(action=MemoryAction.STORE, content="a", importance=0.5),
            MemoryDecision(action=MemoryAction.STORE, content="b", importance=0.5),
            MemoryDecision(action=MemoryAction.DELETE, target_id=uuid.uuid4()),
        ]
        r = reward.compute_trajectory_reward(task_success=True, memory_operations=decisions)
        assert r == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# MemoryReward — Consistency Tests
# ---------------------------------------------------------------------------


class TestConsistencyReward:
    """Tests for persona consistency rewards."""

    @pytest.fixture
    def reward(self):
        return MemoryReward()

    def test_consistency_reward(self, reward):
        """Verify persona consistency gives intermediate reward."""
        r = reward.compute_consistency_reward(persona_consistency=0.8)
        assert r > 0
        assert r < 1.0
        assert r == pytest.approx(0.08)  # 0.8 * 0.1

    def test_consistency_reward_zero(self, reward):
        """Verify zero consistency gives zero reward."""
        r = reward.compute_consistency_reward(persona_consistency=0.0)
        assert r == 0.0

    def test_consistency_reward_perfect(self, reward):
        """Verify perfect consistency gives 0.1 reward."""
        r = reward.compute_consistency_reward(persona_consistency=1.0)
        assert r == pytest.approx(0.1)


# ---------------------------------------------------------------------------
# MemoryReward — Aggregation Tests
# ---------------------------------------------------------------------------


class TestRewardAggregation:
    """Tests for multi-level reward aggregation."""

    @pytest.fixture
    def reward(self):
        return MemoryReward()

    def test_aggregate_rewards(self, reward):
        """Verify multi-level rewards are aggregated correctly."""
        step_rewards = [0.5, 0.3, 0.2]
        trajectory_reward = 1.0
        consistency_reward = 0.08

        total = reward.aggregate_rewards(
            step_rewards=step_rewards,
            trajectory_reward=trajectory_reward,
            consistency_reward=consistency_reward,
        )
        expected = sum(step_rewards) + trajectory_reward + consistency_reward
        assert total == pytest.approx(expected)

    def test_aggregate_rewards_empty_steps(self, reward):
        """Verify aggregation with no step rewards."""
        total = reward.aggregate_rewards(
            step_rewards=[],
            trajectory_reward=1.0,
            consistency_reward=0.05,
        )
        assert total == pytest.approx(1.05)

    def test_aggregate_rewards_negative_trajectory(self, reward):
        """Verify aggregation with failed task."""
        total = reward.aggregate_rewards(
            step_rewards=[0.2, 0.1],
            trajectory_reward=-0.1,
            consistency_reward=0.03,
        )
        assert total == pytest.approx(0.23)

    def test_aggregate_rewards_all_zero(self, reward):
        """Verify aggregation with all zero rewards."""
        total = reward.aggregate_rewards(
            step_rewards=[],
            trajectory_reward=0.0,
            consistency_reward=0.0,
        )
        assert total == 0.0
