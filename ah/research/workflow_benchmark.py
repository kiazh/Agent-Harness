"""Reproducible local latency comparison for the approval recovery trial."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import statistics
import time
import uuid
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

from ah.db.connection import Database
from ah.research.workflow_trial import GraphApprovalTrial, NativeApprovalTrial


def _summary(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    return {
        "meanMs": round(statistics.mean(samples), 3),
        "p95Ms": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 3),
    }


async def compare(dsn: str, iterations: int = 5) -> dict:
    """Run approved database effects under both backends in an isolated DB."""
    if not 1 <= iterations <= 100:
        raise ValueError("iterations must be between 1 and 100")
    database = Database(dsn=dsn)
    await database.connect()
    session_id = uuid.uuid4()
    agent_id = f"workflow-benchmark-{uuid.uuid4()}"
    await database.execute(
        "INSERT INTO sessions (id, title, agent_id) VALUES ($1, $2, $3)",
        session_id,
        "workflow benchmark",
        agent_id,
    )
    native_ids: list[uuid.UUID] = []
    graph_ids: list[uuid.UUID] = []
    native_samples: list[float] = []
    graph_samples: list[float] = []
    checkpoint_rows = 0
    try:
        native = NativeApprovalTrial(database)
        await native.setup()
        for _ in range(iterations):
            run_id = uuid.uuid4()
            native_ids.append(run_id)
            start = time.perf_counter()
            assert await native.start(run_id, session_id, agent_id, "database effect") == "pending"
            assert await native.resume(run_id, agent_id, "approve") == "complete"
            native_samples.append((time.perf_counter() - start) * 1000)
        async with GraphApprovalTrial(database, dsn) as graph:
            for _ in range(iterations):
                run_id = uuid.uuid4()
                graph_ids.append(run_id)
                start = time.perf_counter()
                assert (
                    await graph.start(run_id, session_id, agent_id, "database effect") == "pending"
                )
                assert await graph.resume(run_id, agent_id, "approve") == "complete"
                graph_samples.append((time.perf_counter() - start) * 1000)
            checkpoint_rows = int(
                await database.fetchval(
                    "SELECT COUNT(*) FROM checkpoints WHERE thread_id = ANY($1::text[])",
                    [str(run_id) for run_id in graph_ids],
                )
            )
            for run_id in graph_ids:
                await graph.delete_thread(run_id)
        return {
            "measuredAt": datetime.now(UTC).isoformat(),
            "python": platform.python_version(),
            "platform": platform.platform(),
            "langgraphVersion": version("langgraph"),
            "langgraphCheckpointPostgres": version("langgraph-checkpoint-postgres"),
            "iterations": iterations,
            "native": _summary(native_samples),
            "langgraph": _summary(graph_samples),
            "graphCheckpointRows": checkpoint_rows,
            "scope": "approval plus idempotent PostgreSQL insert; no LLM, tool gateway, or streaming",
        }
    finally:
        if native_ids or graph_ids:
            await database.execute(
                "DELETE FROM workflow_trial_effects WHERE run_id = ANY($1::uuid[])",
                native_ids + graph_ids,
            )
        if native_ids:
            await database.execute(
                "DELETE FROM workflow_trial_runs WHERE id = ANY($1::uuid[])", native_ids
            )
        await database.execute("DELETE FROM sessions WHERE id = $1", session_id)
        await database.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    dsn = os.environ.get("AGENT_HARNESS_TEST_DATABASE_URL")
    if not dsn:
        parser.error("AGENT_HARNESS_TEST_DATABASE_URL is required")
    if platform.system() == "Windows":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    result = asyncio.run(compare(dsn, args.iterations))
    output = json.dumps(result, indent=2)
    if args.output:
        args.output.write_text(output + "\n", encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
