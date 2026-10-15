"""Reproducible local latency comparison for the approval recovery trial."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import statistics
import sys
import time
import uuid
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

from machina.db.connection import Database
from machina.research.workflow_trial import GraphApprovalTrial, NativeApprovalTrial


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
            # Explicit checks, never `assert`: -O strips asserts and would
            # silently skip the measured work (fabricated timings).
            status = await native.start(run_id, session_id, agent_id, "database effect")
            if status != "pending":
                raise RuntimeError(f"native start returned {status!r}, expected 'pending'")
            status = await native.resume(run_id, agent_id, "approve")
            if status != "complete":
                raise RuntimeError(f"native resume returned {status!r}, expected 'complete'")
            native_samples.append((time.perf_counter() - start) * 1000)
        async with GraphApprovalTrial(database, dsn) as graph:
            for _ in range(iterations):
                run_id = uuid.uuid4()
                graph_ids.append(run_id)
                start = time.perf_counter()
                status = await graph.start(run_id, session_id, agent_id, "database effect")
                if status != "pending":
                    raise RuntimeError(f"langgraph start returned {status!r}, expected 'pending'")
                status = await graph.resume(run_id, agent_id, "approve")
                if status != "complete":
                    raise RuntimeError(f"langgraph resume returned {status!r}, expected 'complete'")
                graph_samples.append((time.perf_counter() - start) * 1000)
            checkpoint_rows = await graph.checkpoint_count(graph_ids)
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
        # One failed delete must not skip the remaining cleanup steps or mask
        # the error that triggered cleanup. Every step runs; if anything
        # failed, surface it after the full cleanup (as a note when an
        # original error is already propagating).
        in_flight = sys.exc_info()[1]
        cleanup_errors: list[str] = []
        if native_ids or graph_ids:
            try:
                await database.execute(
                    "DELETE FROM workflow_trial_effects WHERE run_id = ANY($1::uuid[])",
                    native_ids + graph_ids,
                )
            except Exception as exc:
                cleanup_errors.append(f"effects: {type(exc).__name__}: {exc}")
        if native_ids:
            try:
                await database.execute(
                    "DELETE FROM workflow_trial_runs WHERE id = ANY($1::uuid[])", native_ids
                )
            except Exception as exc:
                cleanup_errors.append(f"trial runs: {type(exc).__name__}: {exc}")
        try:
            await database.execute("DELETE FROM sessions WHERE id = $1", session_id)
        except Exception as exc:
            cleanup_errors.append(f"session: {type(exc).__name__}: {exc}")
        try:
            await database.close()
        except Exception as exc:
            cleanup_errors.append(f"close: {type(exc).__name__}: {exc}")
        if cleanup_errors:
            message = "workflow benchmark cleanup failed: " + "; ".join(cleanup_errors)
            if in_flight is None:
                raise RuntimeError(message)
            in_flight.add_note(message)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    dsn = os.environ.get("MACHINA_TEST_DATABASE_URL")
    if not dsn:
        parser.error("MACHINA_TEST_DATABASE_URL is required")
    if platform.system() == "Windows":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    result = asyncio.run(compare(dsn, args.iterations))
    output = json.dumps(result, indent=2)
    if args.output:
        args.output.write_text(output + "\n", encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
