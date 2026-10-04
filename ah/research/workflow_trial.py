"""Isolated native-vs-LangGraph approval recovery trial.

The side effect is a PostgreSQL row keyed by workflow ID. This proves
idempotency for a transactional database action, not arbitrary external tools.
LangGraph remains an optional research dependency and is imported lazily.
"""

from __future__ import annotations

import uuid
from typing import Any, TypedDict

_SETUP_SQL = """
CREATE TABLE IF NOT EXISTS workflow_trial_runs (
    id UUID PRIMARY KEY,
    session_id UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    agent_id TEXT NOT NULL,
    action TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'complete', 'denied', 'cancelled')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS workflow_trial_effects (
    run_id UUID PRIMARY KEY,
    action TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

_DECISIONS = {"approve", "deny", "cancel"}


class NativeApprovalTrial:
    def __init__(self, database: Any) -> None:
        self.db = database

    async def setup(self) -> None:
        async with self.db.acquire() as conn:
            await conn.execute(_SETUP_SQL)

    async def _check_owner(self, session_id: uuid.UUID, agent_id: str) -> None:
        owner = await self.db.fetchval("SELECT agent_id FROM sessions WHERE id = $1", session_id)
        if owner is None:
            raise LookupError("session not found")
        if owner != agent_id:
            raise PermissionError("session belongs to another agent")

    async def start(
        self, run_id: uuid.UUID, session_id: uuid.UUID, agent_id: str, action: str
    ) -> str:
        await self._check_owner(session_id, agent_id)
        if not action.strip() or len(action) > 1000:
            raise ValueError("action must contain 1-1000 characters")
        row = await self.db.fetchrow(
            """INSERT INTO workflow_trial_runs (id, session_id, agent_id, action, status)
               VALUES ($1, $2, $3, $4, 'pending')
               ON CONFLICT (id) DO NOTHING RETURNING status""",
            run_id,
            session_id,
            agent_id,
            action,
        )
        if row is not None:
            return row["status"]
        existing = await self.db.fetchrow(
            "SELECT session_id, agent_id, status FROM workflow_trial_runs WHERE id = $1", run_id
        )
        if existing["session_id"] != session_id or existing["agent_id"] != agent_id:
            raise PermissionError("workflow belongs to another session or agent")
        return existing["status"]

    async def resume(self, run_id: uuid.UUID, agent_id: str, decision: str) -> str:
        if decision not in _DECISIONS:
            raise ValueError("decision must be approve, deny, or cancel")
        async with self.db.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT * FROM workflow_trial_runs WHERE id = $1 FOR UPDATE", run_id
                )
                if row is None:
                    raise LookupError("workflow not found")
                if row["agent_id"] != agent_id:
                    raise PermissionError("workflow belongs to another agent")
                if row["status"] != "pending":
                    return row["status"]
                if decision == "approve":
                    await conn.execute(
                        """INSERT INTO workflow_trial_effects (run_id, action)
                           VALUES ($1, $2) ON CONFLICT (run_id) DO NOTHING""",
                        run_id,
                        row["action"],
                    )
                    status = "complete"
                else:
                    status = "denied" if decision == "deny" else "cancelled"
                await conn.execute(
                    "UPDATE workflow_trial_runs SET status = $2, decided_at = now() WHERE id = $1",
                    run_id,
                    status,
                )
                return status

    async def effect_count(self, run_id: uuid.UUID) -> int:
        return int(
            await self.db.fetchval(
                "SELECT COUNT(*) FROM workflow_trial_effects WHERE run_id = $1", run_id
            )
        )


class _GraphState(TypedDict, total=False):
    run_id: str
    session_id: str
    agent_id: str
    action: str
    decision: str
    status: str


class GraphApprovalTrial(NativeApprovalTrial):
    """Same approval case using LangGraph's durable PostgreSQL checkpointer."""

    def __init__(self, database: Any, dsn: str) -> None:
        super().__init__(database)
        self.dsn = dsn
        self._saver_context: Any = None
        self._saver: Any = None
        self.graph: Any = None

    async def __aenter__(self) -> GraphApprovalTrial:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from langgraph.graph import END, START, StateGraph
        from langgraph.types import interrupt

        await self.setup()
        self._saver_context = AsyncPostgresSaver.from_conn_string(self.dsn)
        self._saver = await self._saver_context.__aenter__()
        await self._saver.setup()

        def approval(state: _GraphState) -> _GraphState:
            decision = interrupt({"runId": state["run_id"], "action": state["action"]})
            if decision not in _DECISIONS:
                raise ValueError("invalid approval decision")
            return {
                "decision": decision,
                "status": "approved"
                if decision == "approve"
                else ("denied" if decision == "deny" else "cancelled"),
            }

        async def execute(state: _GraphState) -> _GraphState:
            await self.db.execute(
                """INSERT INTO workflow_trial_effects (run_id, action)
                   VALUES ($1, $2) ON CONFLICT (run_id) DO NOTHING""",
                uuid.UUID(state["run_id"]),
                state["action"],
            )
            return {"status": "complete"}

        builder = StateGraph(_GraphState)
        builder.add_node("approval", approval)
        builder.add_node("execute", execute)
        builder.add_edge(START, "approval")
        builder.add_conditional_edges(
            "approval",
            lambda state: "execute" if state["decision"] == "approve" else "end",
            {"execute": "execute", "end": END},
        )
        builder.add_edge("execute", END)
        self.graph = builder.compile(checkpointer=self._saver)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self._saver_context.__aexit__(exc_type, exc, tb)

    @staticmethod
    def _config(run_id: uuid.UUID) -> dict[str, Any]:
        return {"configurable": {"thread_id": str(run_id)}}

    async def start(
        self, run_id: uuid.UUID, session_id: uuid.UUID, agent_id: str, action: str
    ) -> str:
        await self._check_owner(session_id, agent_id)
        if not action.strip() or len(action) > 1000:
            raise ValueError("action must contain 1-1000 characters")
        current = await self.graph.aget_state(self._config(run_id))
        if current.values:
            if current.values.get("agent_id") != agent_id or current.values.get(
                "session_id"
            ) != str(session_id):
                raise PermissionError("workflow belongs to another session or agent")
            return current.values.get("status", "pending")
        await self.graph.ainvoke(
            {
                "run_id": str(run_id),
                "session_id": str(session_id),
                "agent_id": agent_id,
                "action": action,
                "status": "pending",
            },
            config=self._config(run_id),
            durability="sync",
        )
        return "pending"

    async def resume(self, run_id: uuid.UUID, agent_id: str, decision: str) -> str:
        from langgraph.types import Command

        if decision not in _DECISIONS:
            raise ValueError("decision must be approve, deny, or cancel")
        snapshot = await self.graph.aget_state(self._config(run_id))
        if not snapshot.values:
            raise LookupError("workflow not found")
        if snapshot.values["agent_id"] != agent_id:
            raise PermissionError("workflow belongs to another agent")
        await self._check_owner(uuid.UUID(snapshot.values["session_id"]), agent_id)
        if not snapshot.next:
            return snapshot.values["status"]
        result = await self.graph.ainvoke(
            Command(resume=decision), config=self._config(run_id), durability="sync"
        )
        return result["status"]

    async def delete_thread(self, run_id: uuid.UUID) -> None:
        await self._saver.adelete_thread(str(run_id))


async def _main() -> None:
    """Run one trial stage in its own process against the isolated test DB."""
    import argparse
    import json
    import os

    from ah.db.connection import Database

    parser = argparse.ArgumentParser(description="Isolated approval recovery trial")
    parser.add_argument("--backend", choices=("native", "graph"), required=True)
    parser.add_argument("--command", choices=("start", "resume"), required=True)
    parser.add_argument("--run-id", type=uuid.UUID, required=True)
    parser.add_argument("--session-id", type=uuid.UUID, required=True)
    parser.add_argument("--agent", required=True)
    parser.add_argument("--value", required=True)
    args = parser.parse_args()
    dsn = os.environ.get("AGENT_HARNESS_TEST_DATABASE_URL")
    if not dsn:
        parser.error("AGENT_HARNESS_TEST_DATABASE_URL is required")
    database = Database(dsn=dsn)
    await database.connect()
    try:
        if args.backend == "native":
            trial = NativeApprovalTrial(database)
            await trial.setup()
            if args.command == "start":
                status = await trial.start(args.run_id, args.session_id, args.agent, args.value)
            else:
                status = await trial.resume(args.run_id, args.agent, args.value)
        else:
            async with GraphApprovalTrial(database, dsn) as trial:
                if args.command == "start":
                    status = await trial.start(args.run_id, args.session_id, args.agent, args.value)
                else:
                    status = await trial.resume(args.run_id, args.agent, args.value)
        print(json.dumps({"status": status}))
    finally:
        await database.close()


if __name__ == "__main__":
    import asyncio
    import sys

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(_main())
