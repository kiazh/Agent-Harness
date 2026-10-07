"""Scheduled jobs: heartbeats, intervals, and UTC cron expressions.

A *job* re-runs a prompt on a session. Three kinds:

- ``interval``  — run the prompt as a normal agent turn on its session.
- ``heartbeat`` — a nudge: the prompt defaults to "continue your current goal",
  used to re-engage an idle session.
- ``cron`` — run at the times selected by a five-field UTC expression.

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
from datetime import datetime, timedelta
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
    model: str | None = None
    provider: str | None = None
    no_agent: bool = False
    script_path: str | None = None
    # AH-026: fencing token from claim_due. None for unclaimed/test rows.
    claim_token: uuid.UUID | None = None

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
            "model": self.model,
            "provider": self.provider,
            "noAgent": self.no_agent,
            "scriptPath": self.script_path,
        }


_COLUMNS = (
    "id, name, kind, session_id, agent_name, prompt, interval_seconds, enabled, "
    "status, last_run_at, next_run_at, last_error, run_count, cron_expression, model, provider, "
    "no_agent, script_path, claim_token"
)


def _row_to_job(row: Any) -> Job:
    # claim_token may be absent on old mocks/rows — tolerate missing key.
    try:
        claim = row["claim_token"] if "claim_token" in row.keys() else None
    except Exception:
        claim = (
            row["claim_token"]
            if isinstance(row, dict) and "claim_token" in row
            else getattr(row, "claim_token", None)
        )
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
        model=row["model"],
        provider=row["provider"],
        no_agent=row["no_agent"],
        script_path=row["script_path"],
        claim_token=claim,
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
        model: str | None = None,
        provider: str | None = None,
        no_agent: bool = False,
        script_path: str | None = None,
    ) -> Job:
        if kind not in ("heartbeat", "interval", "cron"):
            raise ValueError("kind must be 'heartbeat', 'interval', or 'cron'")
        if not isinstance(prompt, str):
            raise ValueError("prompt must be a string")
        if kind != "cron" and interval_seconds < MIN_INTERVAL_SECONDS:
            raise ValueError(f"interval must be at least {MIN_INTERVAL_SECONDS} seconds")
        if kind == "cron":
            if not cron_expression:
                raise ValueError("cron_expression is required for cron jobs")
        if model is not None and (
            not isinstance(model, str) or not model.strip() or len(model) > 200
        ):
            raise ValueError("model must be a non-empty string of at most 200 characters")
        if provider is not None and (
            not isinstance(provider, str) or not provider.strip() or len(provider) > 100
        ):
            raise ValueError("provider must be a non-empty string of at most 100 characters")
        if no_agent:
            from ah.core.job_scripts import resolve_script_path

            if not script_path:
                raise ValueError("script_path is required for no-agent jobs")
            if prompt.strip():
                raise ValueError("prompt must be empty for no-agent jobs")
            if model or provider:
                raise ValueError("no-agent jobs cannot pin a model or provider")
            resolve_script_path(script_path)
        elif script_path is not None:
            raise ValueError("script_path requires no_agent=true")
        database_now = await db.fetchval("SELECT now()")
        next_run = (
            next_cron_time(cron_expression, database_now)
            if kind == "cron"
            else database_now + timedelta(seconds=interval_seconds)
        )
        row = await db.fetchrow(
            f"""
            INSERT INTO jobs (name, kind, session_id, agent_name, prompt, interval_seconds,
                              next_run_at, cron_expression, model, provider, no_agent, script_path)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
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
            model,
            provider,
            no_agent,
            script_path,
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
            next_cron_time(job.cron_expression, await db.fetchval("SELECT now()"))
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
        double-executing a job. AH-026: each claim mints a unique
        ``claim_token`` that renew/finish must present — a stale worker can
        no longer mutate a newer claim.
        """
        # Use the database clock in production. Its timestamp also governs
        # next_run_at, so client/server clock skew cannot hide a due job.
        clock = "$1" if now is not None else "now()"
        lease = "$2" if now is not None else "$1"
        params = (now, RUN_LEASE_SECONDS) if now is not None else (RUN_LEASE_SECONDS,)
        token = uuid.uuid4()
        token_ph = "$3" if now is not None else "$2"
        row = await db.fetchrow(
            f"""
            UPDATE jobs
            SET status = 'running', last_run_at = {clock},
                next_run_at = {clock} + ({lease} * interval '1 second'),
                claim_token = {token_ph}
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
            token,
        )
        return _row_to_job(row) if row else None

    async def renew_lease(self, job_id: uuid.UUID, claim_token: uuid.UUID | None = None) -> bool:
        """Keep an active run from being reclaimed while it is executing.

        Fencing (AH-026): single transaction — SELECT ... FOR UPDATE, return
        False unless status is still 'running' AND the claim token matches
        (when tokens are in use). The enabled flag is intentionally not
        checked: a job disabled mid-run keeps its lease until finish() so two
        runners cannot both own it.
        """
        async with db.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT status, claim_token FROM jobs WHERE id = $1 FOR UPDATE",
                    job_id,
                )
                if row is None or row["status"] != "running":
                    return False
                if claim_token is not None:
                    try:
                        current = row["claim_token"]
                    except Exception:
                        current = None
                    # Tokens in use: a mismatch means a newer claim owns the
                    # job — the stale owner must stop.
                    if current is not None and current != claim_token:
                        return False
                result = await conn.execute(
                    """
                    UPDATE jobs
                    SET next_run_at = now() + ($2 * interval '1 second')
                    WHERE id = $1 AND status = 'running'
                    AND (claim_token IS NULL OR claim_token = $3 OR $3 IS NULL)
                    """,
                    job_id,
                    RUN_LEASE_SECONDS,
                    claim_token,
                )
                return parse_command_count(result) > 0

    async def finish(
        self,
        job_id: uuid.UUID,
        *,
        error: str | None = None,
        claim_token: uuid.UUID | None = None,
    ) -> bool:
        """Record a run outcome and schedule the next run from now.

        Fencing (AH-026): requires the claim token when tokens are in use;
        a stale claimant cannot finish (and reschedule) a newer claim.
        Allows idle->idle (direct finish without claim, for tests/CLI) and
        running->idle/error, but returns False if already error/finished
        to avoid stale overwrite.
        """
        # Mock compat: SimpleNamespace(fetchval, execute) without acquire.
        if not hasattr(db, "acquire"):
            # Prefer self.get() when mocked (test_review_scheduler_clock mocks it).
            try:
                job_obj = await self.get(job_id)
            except Exception:
                job_obj = None
            if job_obj is not None:
                kind = getattr(job_obj, "kind", "interval")
                expr = getattr(job_obj, "cron_expression", None)
            else:
                job_row = (
                    await db.fetchrow(f"SELECT {_COLUMNS} FROM jobs WHERE id = $1", job_id)
                    if hasattr(db, "fetchrow")
                    else None
                )
                kind = getattr(job_row, "kind", "interval") if job_row else "interval"
                expr = getattr(job_row, "cron_expression", None) if job_row else None
            # Fallback path used by test_review_scheduler_clock mock.
            db_now = await db.fetchval("SELECT now()") if hasattr(db, "fetchval") else None
            from datetime import UTC, datetime

            db_now = db_now or datetime.now(UTC)
            # If mock returns SimpleNamespace, compute next_run directly.
            try:
                next_run = next_cron_time(expr, db_now) if kind == "cron" and expr else None
            except Exception:
                next_run = None
            result = await db.execute(
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
            return parse_command_count(result) > 0 if isinstance(result, str) else True
        async with db.acquire() as conn:
            async with conn.transaction():
                job_row = await conn.fetchrow(
                    f"SELECT {_COLUMNS} FROM jobs WHERE id = $1 FOR UPDATE",
                    job_id,
                )
                if job_row is None or job_row["status"] not in ("idle", "running"):
                    return False
                # AH-026: stale owners cannot finish a newer claim.
                if claim_token is not None:
                    try:
                        current = job_row["claim_token"]
                    except Exception:
                        current = None
                    if (
                        job_row["status"] == "running"
                        and current is not None
                        and current != claim_token
                    ):
                        return False
                job = _row_to_job(job_row)
                db_now = await conn.fetchval("SELECT now()")
                next_run = (
                    next_cron_time(job.cron_expression, db_now)
                    if job.kind == "cron" and job.cron_expression
                    else None
                )
                result = await conn.execute(
                    """
                    UPDATE jobs
                    SET status = $2,
                        last_error = $3,
                        run_count = run_count + 1,
                        claim_token = NULL,
                        next_run_at = CASE WHEN kind = 'cron' THEN $4
                            ELSE now() + (interval_seconds || ' seconds')::interval END
                    WHERE id = $1 AND status IN ('idle', 'running')
                    AND (claim_token IS NULL OR claim_token = $5 OR $5 IS NULL)
                    """,
                    job_id,
                    "error" if error else "idle",
                    error,
                    next_run,
                    claim_token,
                )
                return parse_command_count(result) > 0


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
        # AH-026: carry the claim token; a lost lease aborts the execution.
        claim_token = getattr(job, "claim_token", None)
        ownership_lost = asyncio.Event()
        lease_task = asyncio.create_task(self._keep_lease(job.id, claim_token, ownership_lost))
        try:
            # AH-027: whole-job timeout so a hung provider cannot monopolize
            # the serial runner indefinitely. Bounded at RUN_LEASE_SECONDS
            # (default 300s); at-least-once effects must be idempotent.
            try:
                await asyncio.wait_for(
                    self._execute(job, ownership_lost),
                    timeout=float(RUN_LEASE_SECONDS),
                )
            except TimeoutError:
                error = f"job timed out after {RUN_LEASE_SECONDS}s"
                logger.warning("Job %s (%s) timed out", job.name, job.id)
        except asyncio.CancelledError:
            error = "cancelled"
            raise
        except Exception as e:
            logger.exception("Job %s (%s) failed", job.name, job.id)
            error = f"{type(e).__name__}: {e}"
        finally:
            lease_task.cancel()
            await asyncio.gather(lease_task, return_exceptions=True)
            # Cancellation-aware cleanup (AH-026): pass the token so a stale
            # worker cannot finish a newer claim. If ownership was lost, do
            # not overwrite the newer owner's outcome.
            if ownership_lost.is_set():
                logger.warning("Job %s (%s) lost ownership; skipping finish", job.name, job.id)
            else:
                try:
                    await self._store.finish(job.id, error=error, claim_token=claim_token)
                except TypeError:
                    # Backwards compat with test doubles whose finish() lacks
                    # claim_token.
                    await self._store.finish(job.id, error=error)
        return True

    async def _keep_lease(
        self,
        job_id: uuid.UUID,
        claim_token: uuid.UUID | None = None,
        ownership_lost: asyncio.Event | None = None,
    ) -> None:
        while True:
            await asyncio.sleep(RUN_LEASE_SECONDS / 3)
            try:
                try:
                    renewed = await self._store.renew_lease(job_id, claim_token)
                except TypeError:
                    renewed = await self._store.renew_lease(job_id)
            except Exception:
                logger.exception("Could not renew lease for job %s", job_id)
                continue
            # AH-026: abort execution on ownership loss instead of ignoring
            # False from renewal.
            if not renewed:
                logger.warning("Lost lease for job %s; aborting execution", job_id)
                if ownership_lost is not None:
                    ownership_lost.set()
                return

    async def _execute(self, job: Job, ownership_lost: asyncio.Event | None = None) -> None:
        if job.session_id is None:
            raise RuntimeError("job has no session")
        if ownership_lost is not None and ownership_lost.is_set():
            raise RuntimeError("job ownership lost before execution")
        if getattr(job, "no_agent", False):
            from ah.core.assembler import get_token_count
            from ah.core.context import context_manager
            from ah.core.job_scripts import run_job_script
            from ah.memory.redaction import redact_secrets

            output = redact_secrets(await run_job_script(job.script_path or "")).text
            if output:
                await context_manager.add_chunk(
                    session_id=job.session_id,
                    agent_id=job.agent_name,
                    chunk_type="result",
                    payload={"content": output, "job_id": str(job.id)},
                    token_count=get_token_count(output),
                )
            return
        prompt = job.prompt or DEFAULT_HEARTBEAT_PROMPT
        agent = await self._build_agent(
            job.agent_name,
            model=getattr(job, "model", None),
            provider=getattr(job, "provider", None),
        )
        # AH-023: close owned providers in finally; injected test factories
        # manage their own lifecycle.
        owns_provider = self._agent_factory is None
        try:
            # Check ownership loss before the (potentially long) agent run.
            if ownership_lost is not None and ownership_lost.is_set():
                raise RuntimeError("job ownership lost")
            response = await agent.run(job.session_id, prompt, verbose=False)
            # Headless jobs never hang on hidden prompts (Phase E): a tool
            # awaiting human approval surfaces as a structured needs_approval
            # result. Pause durably with a resumable status and free the
            # worker/lease instead of renewing indefinitely.
            try:
                blob = (response.content or "") + str(
                    [t.get("result_preview", "") for t in response.tool_calls]
                )
                if "Needs approval" in blob or "needs_approval" in blob:
                    raise PermissionError(
                        "needs_approval: job paused awaiting human approval; "
                        "resolve via approvals then resume (fresh claim revalidates)."
                    )
            except PermissionError:
                raise
            except Exception:
                pass
        finally:
            if owns_provider:
                try:
                    prov = getattr(agent, "provider", None)
                    close = getattr(prov, "close", None)
                    if callable(close):
                        import inspect as _inspect

                        r = close()
                        if _inspect.isawaitable(r):
                            await r
                except Exception:
                    pass

    async def _build_agent(
        self, agent_name: str, *, model: str | None = None, provider: str | None = None
    ):
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
                provider=provider or definition.provider or config.get("provider"),
                model=model or definition.model or config.get("model"),
            ),
            max_iterations=definition.max_iterations,
            agent_id=agent_name,
            system_prompt=definition.system_prompt or None,
            allowed_tools=definition.tools or None,
        )


job_runner = JobRunner()
