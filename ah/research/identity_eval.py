"""Longitudinal identity-drift injection evaluation on isolated agent IDs."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ah.db.connection import db
from ah.memory.identity import AgentBelief, IdentityGate
from ah.memory.redaction import SecretRedactor


@dataclass(frozen=True)
class DriftMetrics:
    scenarios: int
    transitions: int
    injected_transitions: int
    attack_rejection_rate: float
    benign_acceptance_rate: float
    quarantined_memories: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_identity_scenarios(path: str | Path) -> list[dict[str, Any]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list) or not data:
        raise ValueError("identity scenarios must be a non-empty JSON array")
    for index, scenario in enumerate(data):
        if not isinstance(scenario, dict) or not isinstance(scenario.get("baseline"), dict):
            raise ValueError(f"scenario {index} needs a baseline")
        if not isinstance(scenario.get("steps"), list) or not scenario["steps"]:
            raise ValueError(f"scenario {index} needs steps")
        for step in scenario["steps"]:
            if (
                not isinstance(step, dict)
                or not isinstance(step.get("proposed"), dict)
                or type(step.get("injected")) is not bool
                or not isinstance(step.get("content"), str)
            ):
                raise ValueError(f"scenario {index} has an invalid step")
    return data


def _belief(agent_id: str, data: dict[str, Any]) -> AgentBelief:
    return AgentBelief(
        agent_id=agent_id,
        known_facts=data.get("known_facts", {}),
        traits=data.get("traits", {}),
        values=data.get("values", {}),
    )


async def evaluate_identity_scenarios(
    scenarios: list[dict[str, Any]], *, threshold: float = 0.5
) -> DriftMetrics:
    """Replay labeled proposals and clean all benchmark rows afterward."""
    gate = IdentityGate()
    redactor = SecretRedactor()
    attacks = rejected_attacks = benign = accepted_benign = quarantined = transitions = 0
    for scenario in scenarios:
        agent_id = f"identity-benchmark-{uuid.uuid4()}"
        try:
            await gate.save_beliefs(_belief(agent_id, scenario["baseline"]))
            for step in scenario["steps"]:
                memory_id = await db.fetchval(
                    "INSERT INTO memories (agent_id, content, category) "
                    "VALUES ($1, $2, 'fact') RETURNING id",
                    agent_id,
                    redactor.redact(step["content"]).text,
                )
                result = await gate.observe_transition(
                    _belief(agent_id, step["proposed"]),
                    causal_memory_ids=[memory_id],
                    threshold=threshold,
                )
                transitions += 1
                quarantined += result.quarantined_count
                if step["injected"]:
                    attacks += 1
                    rejected_attacks += not result.accepted
                else:
                    benign += 1
                    accepted_benign += result.accepted
        finally:
            await db.execute("DELETE FROM agent_belief_history WHERE agent_id = $1", agent_id)
            await db.execute("DELETE FROM memories WHERE agent_id = $1", agent_id)
            await db.execute("DELETE FROM agent_beliefs WHERE agent_id = $1", agent_id)
            await db.execute("DELETE FROM memory_provenance WHERE source_agent = $1", agent_id)
            await db.execute(
                "DELETE FROM shared_memory_deliveries WHERE recipient_agent = $1", agent_id
            )
            await db.execute("DELETE FROM learning_reviews WHERE agent_id = $1", agent_id)
    return DriftMetrics(
        scenarios=len(scenarios),
        transitions=transitions,
        injected_transitions=attacks,
        attack_rejection_rate=rejected_attacks / attacks if attacks else 0.0,
        benign_acceptance_rate=accepted_benign / benign if benign else 0.0,
        quarantined_memories=quarantined,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate labeled identity drift scenarios")
    parser.add_argument("scenarios", help="JSON array of baseline and proposed belief sequences")
    parser.add_argument(
        "--test-db", action="store_true", help="Use AGENT_HARNESS_TEST_DATABASE_URL"
    )
    parser.add_argument("--db-url", help="Initialized PostgreSQL DSN")
    parser.add_argument("--threshold", type=float, default=0.5)
    args = parser.parse_args()
    if args.db_url and args.test_db:
        parser.error("use either --db-url or --test-db")
    if not args.db_url and not args.test_db:
        parser.error("--db-url or --test-db is required")
    database_url = args.db_url
    if args.test_db:
        import ah.core.config  # noqa: F401 - loads local test environment

        database_url = os.environ.get("AGENT_HARNESS_TEST_DATABASE_URL")
    if not database_url:
        parser.error("AGENT_HARNESS_TEST_DATABASE_URL is required with --test-db")

    async def run() -> DriftMetrics:
        db.dsn = database_url
        await db.connect()
        try:
            return await evaluate_identity_scenarios(
                load_identity_scenarios(args.scenarios), threshold=args.threshold
            )
        finally:
            await db.close()

    print(json.dumps(asyncio.run(run()).to_dict(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
