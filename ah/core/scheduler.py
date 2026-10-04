"""Scheduled jobs: heartbeats and interval tasks (Phase 6a).

A *job* re-runs a prompt on a session every ``interval_seconds``. Two kinds:

- ``interval``  — run the prompt as a normal agent turn on its session.
- ``heartbeat`` — a nudge: the prompt defaults to "continue your current goal",
  used to re-engage an idle session.

:class:`JobStore` persists jobs in the ``jobs`` table; :class:`JobRunner` polls
for due jobs and runs them one at a time. Claiming a due row is atomic
(``UPDATE ... WHERE next_run_at <= now()`` with ``FOR UPDATE SKIP LOCKED``) so
two runners never execute the same job.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from ah.core.config import config
from ah.core.cron import next_cron_time
from ah.db.connection import db, parse_command_count

__all__ = ["Job", "JobStore", "JobRunner", "job_store", "DEFAULT_HEARTBEAT_PROMPT"]

logger = logging.getLogger(__name__)

DEFAULT_HEARTBEAT_PROMPT = "Continue working toward your current goal. If it is done, say so."
MIN_INTERVAL_SECONDS = 10
RUN_LEASE_SECONDS = 300


@dataclass
class Job:
    id: uuid.UUID
    name: str
    kind: str  # "heartbeat" | "interval"
    session_id: uuid.UUID | None
    agent_name: str
    prompt: str
    interval_seconds: int
    enabled: bool
    status: str
    last_run_at: datetime | None
    next_run_at: datetime
    last_error: str | None
    run_count: int
    cron_expression: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "name": self.name,
            "kind": self.kind,
            "sessionId": str(self.session_id) if self.session_id else None,
            "agent": self.agent_name,
            "prompt": self.prompt,
            "intervalSeconds": self.interval_seconds,
            "enabled": self.enabled,
            "status": self.status,
            "lastRunAt": self.last_run_at.isoformat() if self.last_run_at else None,
            "nextRunAt": self.next_run_at.isoformat() if self.next_run_at else None,
            "lastError": self.last_error,
            "runCount": self.run_count,
            "cronExpression": self.cron_expression,
        }


_COLUMNS = (
    "id, name, kind, session_id, agent_name, prompt, interval_seconds, enabled, "
    "status, last_run_at, next_run_at, last_error, run_count, cron_expression"
)


def _row_to_job(row: Any) -> Job:
    return Job(
        id=row["id"],
        name=row["name"],
        kind=row["kind"],
        session_id=row["session_id"],
        agent_name=row["agent_name"],
        prompt=row["prompt"],
        interval_seconds=row["interval_seconds"],
        enabled=row["enabled"],
        status=row["status"],
        last_run_at=row["last_run_at"],
        next_run_at=row["next_run_at"],
        last_error=row["last_error"],
        run_count=row["run_count"],
        cron_expression=row["cron_expression"],
    )


class JobStore:
    """CRUD for scheduled jobs."""

    async def create(
        self,
        *,
        name: str,
        kind: str,
        session_id: uuid.UUID,
        prompt: str,
        interval_seconds: int,
        agent_name: str = "harness",
        cron_expression: str | None = None,
    ) -> Job:
        if kind not in ("heartbeat", "interval", "cron"):
            raise ValueError("kind must be 'heartbeat', 'interval', or 'cron'")
        if kind != "cron" and interval_seconds < MIN_INTERVAL_SECONDS:
            raise ValueError(f"interval must be at least {MIN_INTERVAL_SECONDS} seconds")
        if kind == "cron":
            if not cron_expression:
                raise ValueError("cron_expression is required for cron jobs")
            next_run = next_cron_time(cron_expression, datetime.now(UTC))
        else:
            next_run = datetime.now(UTC) + timedelta(seconds=interval_seconds)
        row = await db.fetchrow(
            f"""
            INSERT INTO jobs (name, kind, session_id, agent_name, prompt, interval_seconds, next_run_at, cron_expression)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING {_COLUMNS}
            """,
            name,
            kind,
            session_id,
            agent_name,
            prompt,
            interval_seconds,
            next_run,
            cron_expression,
        )
        return _row_to_job(row)

    async def get(self, job_id: uuid.UUID) -> Job | None:
        row = await db.fetchrow(f"SELECT {_COLUMNS} FROM jobs WHERE id = $1", job_id)
        return _row_to_job(row) if row else None

    async def list(self, *, session_id: uuid.UUID | None = None, limit: int = 100) -> list[Job]:
        if session_id is not None:
            rows = await db.fetch(
                f"SELECT {_COLUMNS} FROM jobs WHERE session_id = $1 ORDER BY created_at DESC LIMIT $2",
                session_id,
                limit,
            )
        else:
            rows = await db.fetch(
                f"SELECT {_COLUMNS} FROM jobs ORDER BY created_at DESC LIMIT $1", limit
            )
        return [_row_to_job(r) for r in rows]

    async def set_enabled(self, job_id: uuid.UUID, enabled: bool) -> Job | None:
        job = await self.get(job_id)
        if job is None:
            return None
        cron_next = (
            next_cron_time(job.cron_expression, datetime.now(UTC))
            if enabled and job.kind == "cron" and job.cron_expression
            else None
        )
        row = await db.fetchrow(
            f"""
            UPDATE jobs
            SET enabled = $2,
                next_run_at = CASE
                    WHEN $2 AND status <> 'running' AND kind = 'cron'
                    THEN $3
                    WHEN $2 AND status <> 'running'
                    THEN now() + (interval_seconds || ' seconds')::interval
                    ELSE next_run_at
                END
            WHERE id = $1
            RETURNING {_COLUMNS}
            """,
            job_id,
            enabled,
            cron_next,
        )
        return _row_to_job(row) if row else None

    async def delete(self, job_id: uuid.UUID) -> bool:
        result = await db.execute("DELETE FROM jobs WHERE id = $1", job_id)
        return parse_command_count(result) > 0

    async def claim_due(self, now: datetime | None = None) -> Job | None:
        """Atomically take ownership of one due, enabled job, marking it running.

        ``FOR UPDATE SKIP LOCKED`` lets multiple runners coexist without
        double-executing a job.
        """
        # Use the database clock in production. Its timestamp also governs
        # next_run_at, so client/server clock skew cannot hide a due job.
        clock = "$1" if now is not None else "now()"
        lease = "$2" if now is not None else "$1"
        params = (now, RUN_LEASE_SECONDS) if now is not None else (RUN_LEASE_SECONDS,)
        row = await db.fetchrow(
            f"""
            UPDATE jobs
            SET status = 'running', last_run_at = {clock},
                next_run_at = {clock} + ({lease} * interval '1 second')
            WHERE id = (
                SELECT id FROM jobs
                WHERE enabled AND next_run_at <= {clock}
                ORDER BY next_run_at ASC
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            RETURNING {_COLUMNS}
            """,
            *params,
        )
        return _row_to_job(row) if row else None

    async def renew_lease(self, job_id: uuid.UUID) -> None:
        """Keep an active run from being reclaimed while it is executing."""
        await db.execute(
            """
            UPDATE jobs
            SET next_run_at = now() + ($2 * interval '1 second')
            WHERE id = $1 AND status = 'running'
            """,
            job_id,
            RUN_LEASE_SECONDS,
        )

    async def finish(self, job_id: uuid.UUID, *, error: str | None = None) -> None:
        """Record a run outcome and schedule the next run from now."""
        job = await self.get(job_id)
        if job is None:
            return
        next_run = (
            next_cron_time(job.cron_expression, datetime.now(UTC))
            if job.kind == "cron" and job.cron_expression
            else None
        )
        await db.execute(
            """
            UPDATE jobs
            SET status = $2,
                last_error = $3,
                run_count = run_count + 1,
                next_run_at = CASE WHEN kind = 'cron' THEN $4
                    ELSE now() + (interval_seconds || ' seconds')::interval END
            WHERE id = $1
            """,
            job_id,
            "error" if error else "idle",
            error,
            next_run,
        )


job_store = JobStore()


class JobRunner:
    """Background loop that runs due jobs. Started by the gateway or the CLI daemon."""

    def __init__(
        self, store: JobStore | None = None, agent_factory=None, poll_seconds: float = 5.0
    ) -> None:
        self._store = store or job_store
        self._agent_factory = agent_factory  # (agent_name, session_id) -> agent, for tests
        self._poll_seconds = poll_seconds
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping.clear()
            self._task = asyncio.create_task(self._loop(), name="ah-job-runner")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                ran = await self.run_due_once()
            except Exception:
                logger.exception("Job runner poll failed")
                ran = False
            # When a job just ran, loop again immediately to drain the backlog.
            if not ran:
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=self._poll_seconds)
                except TimeoutError:
                    pass

    async def run_due_once(self) -> bool:
        """Claim and run a single due job. Returns True if one ran."""
        job = await self._store.claim_due()
        if job is None:
            return False
        error: str | None = None
        lease_task = asyncio.create_task(self._keep_lease(job.id))
        try:
            await self._execute(job)
        except asyncio.CancelledError:
            error = "cancelled"
            raise
        except Exception as e:
            logger.exception("Job %s (%s) failed", job.name, job.id)
            error = f"{type(e).__name__}: {e}"
        finally:
            lease_task.cancel()
            await asyncio.gather(lease_task, return_exceptions=True)
            await self._store.finish(job.id, error=error)
        return True

    async def _keep_lease(self, job_id: uuid.UUID) -> None:
        while True:
            await asyncio.sleep(RUN_LEASE_SECONDS / 3)
            try:
                await self._store.renew_lease(job_id)
            except Exception:
                logger.exception("Could not renew lease for job %s", job_id)

    async def _execute(self, job: Job) -> None:
        if job.session_id is None:
            raise RuntimeError("job has no session")
        prompt = job.prompt or DEFAULT_HEARTBEAT_PROMPT
        agent = await self._build_agent(job.agent_name)
        await agent.run(job.session_id, prompt, verbose=False)

    async def _build_agent(self, agent_name: str):
        if self._agent_factory is not None:
            return self._agent_factory(agent_name)
        from ah.core.agent import ReActAgent
        from ah.core.agent_def import agent_registry
        from ah.core.provider import get_provider

        definition = await agent_registry.get(agent_name)
        if definition is None:
            raise ValueError(f"no agent named {agent_name!r}")
        return ReActAgent(
            provider=get_provider(
                provider=definition.provider or config.get("provider"),
                model=definition.model or config.get("model"),
            ),
            max_iterations=definition.max_iterations,
            agent_id=agent_name,
            system_prompt=definition.system_prompt or None,
            allowed_tools=definition.tools or None,
        )


job_runner = JobRunner()
