"""Train and evaluate the finite-action memory policy from scored rollouts.

Input is JSONL with a state and a scored outcome for every action available
in that state. This is a counterfactual offline dataset; absent outcomes are
rejected instead of silently assigned zero reward.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ah.memory.models import MemoryEntry
from ah.memory.policy import GroupRelativeTrainer, MemoryPolicy
from ah.memory.rl import MemoryAction, MemoryDecision, MemoryReward, MemoryState


@dataclass(frozen=True)
class ScoredState:
    state: MemoryState
    outcomes: dict[MemoryAction, dict[str, Any]]


def load_scored_states(path: str | Path) -> list[ScoredState]:
    """Validate and load state-action outcome rows from JSONL."""
    examples = []
    for line_number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            state_data = row["state"]
            if not isinstance(state_data, dict):
                raise ValueError("state must be an object")
            count = state_data.get("current_memory_count", 0)
            if type(count) is not int or count < 0 or count > 1000:
                raise ValueError("current_memory_count must be between 0 and 1000")
            memory = [
                MemoryEntry(
                    id=uuid.uuid5(uuid.NAMESPACE_URL, f"{line_number}:{index}"),
                    session_id=None,
                    agent_id="benchmark",
                    content="",
                    category="fact",
                )
                for index in range(count)
            ]
            state = MemoryState(
                conversation=state_data["conversation"],
                current_memory=memory,
                task=state_data["task"],
                budget_remaining=state_data["budget_remaining"],
            )
            if (
                not isinstance(state.task, str)
                or not isinstance(state.conversation, list)
                or type(state.budget_remaining) is not int
                or state.budget_remaining < 0
            ):
                raise ValueError("invalid state")
            raw_outcomes = row["outcomes"]
            if not isinstance(raw_outcomes, dict):
                raise ValueError("outcomes must be an object")
            outcomes = {MemoryAction(key): value for key, value in raw_outcomes.items()}
            if any(not isinstance(value, dict) for value in outcomes.values()):
                raise ValueError("outcomes must be objects")
            missing = set(MemoryPolicy().available_actions(state)) - outcomes.keys()
            if missing:
                raise ValueError(f"missing outcomes for {sorted(a.value for a in missing)}")
            for outcome in outcomes.values():
                if type(outcome.get("task_success")) is not bool:
                    raise ValueError("task_success must be boolean")
                for score in ("relevance", "redundancy", "staleness"):
                    if score in outcome and (
                        type(outcome[score]) not in (int, float)
                        or not math.isfinite(outcome[score])
                    ):
                        raise ValueError(f"{score} must be a finite number")
                consistency = outcome.get("persona_consistency")
                if (
                    type(consistency) not in (int, float)
                    or not math.isfinite(consistency)
                    or not 0 <= consistency <= 1
                ):
                    raise ValueError("persona_consistency must be between zero and one")
            examples.append(ScoredState(state, outcomes))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid training row {line_number}: {exc}") from exc
    if not examples:
        raise ValueError("training data is empty")
    return examples


def train_from_file(
    data_path: str | Path,
    output_path: str | Path,
    *,
    epochs: int = 40,
    seed: int = 0,
    holdout_fraction: float = 0.2,
) -> dict[str, Any]:
    """Train on scored states and report held-out expected reward."""
    if not 0 <= holdout_fraction < 1:
        raise ValueError("holdout_fraction must be in [0, 1)")
    examples = load_scored_states(data_path)
    random.Random(seed).shuffle(examples)
    holdout_count = min(len(examples) - 1, round(len(examples) * holdout_fraction))
    holdout, train = examples[:holdout_count], examples[holdout_count:]
    by_identity = {id(example.state): example.outcomes for example in examples}
    reward_model = MemoryReward()

    def reward(state: MemoryState, action: MemoryAction) -> float:
        outcome = by_identity[id(state)][action]
        return reward_model.aggregate_rewards(
            [reward_model.compute_step_reward(action, state, outcome)],
            reward_model.compute_trajectory_reward(
                outcome["task_success"], [MemoryDecision(action)]
            ),
            reward_model.compute_consistency_reward(outcome["persona_consistency"]),
        )

    policy = MemoryPolicy()
    initial_holdout = (
        policy.expected_reward([example.state for example in holdout], reward) if holdout else None
    )
    report = GroupRelativeTrainer(seed=seed).train(
        policy, [example.state for example in train], reward, epochs=epochs
    )
    final_holdout = (
        policy.expected_reward([example.state for example in holdout], reward) if holdout else None
    )
    policy.save(output_path)
    return {
        "training": asdict(report),
        "holdout_examples": len(holdout),
        "initial_holdout_expected_reward": initial_holdout,
        "final_holdout_expected_reward": final_holdout,
        "seed": seed,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Train an offline memory-action policy")
    parser.add_argument("data", help="JSONL of state/action counterfactual outcomes")
    parser.add_argument("--output", required=True, help="Output policy JSON file")
    parser.add_argument("--epochs", type=int, default=40)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--holdout", type=float, default=0.2)
    args = parser.parse_args()
    result = train_from_file(
        args.data, args.output, epochs=args.epochs, seed=args.seed, holdout_fraction=args.holdout
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
