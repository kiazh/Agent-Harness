"""Small, reproducible group-relative policy trainer for memory actions.

This trains a contextual six-action policy, not an LLM. It provides a local
baseline and a data path for later model-based GRPO experiments.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from ah.memory.rl import MemoryAction, MemoryState

_HASH_FEATURES = 16
_FEATURE_COUNT = 4 + _HASH_FEATURES
RewardFunction = Callable[[MemoryState, MemoryAction], float]


def state_features(state: MemoryState) -> tuple[float, ...]:
    """Bounded numeric features; stable across processes and Python versions."""
    features = [
        1.0,
        min(len(state.current_memory), 20) / 20,
        min(len(state.conversation), 20) / 20,
        1.0 - min(max(state.budget_remaining, 0), 8000) / 8000,
    ] + [0.0] * _HASH_FEATURES
    words = set(re.findall(r"[a-z0-9]+", state.task.lower()))
    for word in words:
        digest = hashlib.blake2b(word.encode(), digest_size=2).digest()
        bucket = int.from_bytes(digest, "big") % _HASH_FEATURES
        features[4 + bucket] = 1.0
    return tuple(features)


@dataclass
class MemoryPolicy:
    """Contextual softmax policy over valid memory operations."""

    weights: dict[MemoryAction, list[float]] = field(
        default_factory=lambda: {action: [0.0] * _FEATURE_COUNT for action in MemoryAction}
    )

    def available_actions(self, state: MemoryState) -> tuple[MemoryAction, ...]:
        actions = [MemoryAction.STORE, MemoryAction.RETRIEVE, MemoryAction.NOOP]
        if state.current_memory:
            actions.extend((MemoryAction.UPDATE, MemoryAction.DELETE))
        if len(state.conversation) >= 2:
            actions.append(MemoryAction.SUMMARIZE)
        return tuple(actions)

    def probabilities(self, state: MemoryState) -> dict[MemoryAction, float]:
        features = state_features(state)
        actions = self.available_actions(state)
        logits = {a: sum(w * x for w, x in zip(self.weights[a], features)) for a in actions}
        maximum = max(logits.values())
        exp = {a: math.exp(score - maximum) for a, score in logits.items()}
        total = sum(exp.values())
        return {a: value / total for a, value in exp.items()}

    def choose(self, state: MemoryState, *, rng: random.Random | None = None) -> MemoryAction:
        probabilities = self.probabilities(state)
        if rng is None:
            return max(probabilities, key=probabilities.__getitem__)
        draw = rng.random()
        for action, probability in probabilities.items():
            draw -= probability
            if draw <= 0:
                return action
        return next(reversed(probabilities))

    def expected_reward(self, states: Iterable[MemoryState], reward: RewardFunction) -> float:
        values = [
            sum(p * reward(state, action) for action, p in self.probabilities(state).items())
            for state in states
        ]
        if not values:
            raise ValueError("at least one state is required")
        return sum(values) / len(values)

    def save(self, path: str | Path) -> None:
        payload = {action.value: self.weights[action] for action in MemoryAction}
        Path(path).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> MemoryPolicy:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or set(payload) != {a.value for a in MemoryAction}:
            raise ValueError("policy must contain weights for all memory actions")
        weights = {}
        for action in MemoryAction:
            values = payload[action.value]
            if (
                not isinstance(values, list)
                or len(values) != _FEATURE_COUNT
                or any(
                    type(value) not in (int, float) or not math.isfinite(value) for value in values
                )
            ):
                raise ValueError(f"invalid weights for {action.value}")
            weights[action] = [float(value) for value in values]
        return cls(weights=weights)


@dataclass(frozen=True)
class TrainingReport:
    epochs: int
    samples: int
    initial_expected_reward: float
    final_expected_reward: float
    reward_history: tuple[float, ...]


class GroupRelativeTrainer:
    """Train a finite-action policy with grouped rewards and a clipped objective.

    Each group samples actions for the same state, normalizes rewards within
    the group, then applies PPO-style ratio clipping and a reference KL term.
    The reward function must be deterministic for reproducible offline runs.
    """

    def __init__(
        self,
        *,
        group_size: int = 8,
        learning_rate: float = 0.05,
        clip_epsilon: float = 0.2,
        kl_coefficient: float = 0.01,
        seed: int = 0,
    ) -> None:
        if group_size < 2 or learning_rate <= 0 or not 0 < clip_epsilon < 1:
            raise ValueError("invalid training hyperparameters")
        if kl_coefficient < 0:
            raise ValueError("kl_coefficient must be non-negative")
        self.group_size = group_size
        self.learning_rate = learning_rate
        self.clip_epsilon = clip_epsilon
        self.kl_coefficient = kl_coefficient
        self.rng = random.Random(seed)

    def train(
        self,
        policy: MemoryPolicy,
        states: Iterable[MemoryState],
        reward: RewardFunction,
        *,
        epochs: int = 20,
        update_passes: int = 2,
    ) -> TrainingReport:
        states = list(states)
        if not states or epochs < 1 or update_passes < 1:
            raise ValueError("states, epochs, and update_passes must be non-empty")
        initial = policy.expected_reward(states, reward)
        reference = {
            state_index: policy.probabilities(state) for state_index, state in enumerate(states)
        }
        history: list[float] = []
        for _ in range(epochs):
            for index, state in enumerate(states):
                old = policy.probabilities(state)
                actions = [policy.choose(state, rng=self.rng) for _ in range(self.group_size)]
                rewards = [float(reward(state, action)) for action in actions]
                if any(not math.isfinite(value) for value in rewards):
                    raise ValueError("reward must be finite")
                mean = sum(rewards) / self.group_size
                std = math.sqrt(sum((value - mean) ** 2 for value in rewards) / self.group_size)
                if std < 1e-12:
                    continue
                advantages = [(value - mean) / (std + 1e-8) for value in rewards]
                features = state_features(state)
                for _ in range(update_passes):
                    current = policy.probabilities(state)
                    logit_gradient = {action: 0.0 for action in current}
                    for action, advantage in zip(actions, advantages):
                        ratio = current[action] / old[action]
                        if (advantage > 0 and ratio > 1 + self.clip_epsilon) or (
                            advantage < 0 and ratio < 1 - self.clip_epsilon
                        ):
                            continue
                        for candidate in current:
                            logit_gradient[candidate] += (
                                advantage
                                * ratio
                                * (float(candidate == action) - current[candidate])
                                / self.group_size
                            )
                    kl = sum(
                        p * math.log(p / reference[index][action]) for action, p in current.items()
                    )
                    for action, p in current.items():
                        logit_gradient[action] -= (
                            self.kl_coefficient * p * (math.log(p / reference[index][action]) - kl)
                        )
                    for action, gradient in logit_gradient.items():
                        for feature_index, value in enumerate(features):
                            policy.weights[action][feature_index] += (
                                self.learning_rate * gradient * value
                            )
            history.append(policy.expected_reward(states, reward))
        return TrainingReport(
            epochs=epochs,
            samples=epochs * len(states) * self.group_size,
            initial_expected_reward=initial,
            final_expected_reward=history[-1],
            reward_history=tuple(history),
        )
