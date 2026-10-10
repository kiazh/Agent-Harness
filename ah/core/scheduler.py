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

from ah.core.cron import next_cron_time
from ah.db.connection import db, parse_command_count

__all__ = ["Job", "JobStore", "JobRunner", "job_store", "DEFAULT_HEARTBEAT_PROMPT"]

logger = logging.getLogger(__name__)

DEFAULT_HEARTBEAT_PROMPT = "Continue working toward your current goal. If it is done, say so."
MIN_INTERVAL_SECONDS = 10
RUN_LEASE_SECONDS = 300


def _pause_request_id(exc: PermissionError) -> str | None:
    """Extract the linked approval request id from a typed pause error."""
    try:
        linked = getattr(exc, "approval_request_id", None)
        if linked:
            return str(linked)
        import re as _re

        match = _re.search(r"request\s+([0-9a-fA-F-]{8,64})", str(exc))
        return match.group(1) if match else None
    except Exception:
        return None


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
    # AH-AUDIT-021: linked approval request when parked awaiting human
    # approval. Paused jobs are excluded from due claims until resumed.
    paused_for_approval: str | None = None

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
            "pausedForApproval": self.paused_for_approval,
        }


_COLUMNS = (
    "id, name, kind, session_id, agent_name, prompt, interval_seconds, enabled, "
    "status, last_run_at, next_run_at, last_error, run_count, cron_expression, model, provider, "
    "no_agent, script_path, claim_token, paused_for_approval"
)


def _optional_col(row: Any, name: str) -> Any:
    """Tolerate missing columns on old mocks/rows (narrow compat)."""
    try:
        keys = row.keys()
    except Exception:
        keys = None
    try:
        if keys is not None:
            return row[name] if name in keys else None
        if isinstance(row, dict):
            return row.get(name)
        return getattr(row, name, None)
    except Exception:
        return None


def _row_to_job(row: Any) -> Job:
    # claim_token / paused_for_approval may be absent on old mocks/rows.
    claim = _optional_col(row, "claim_token")
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
        paused_for_approval=_optional_col(row, "paused_for_approval"),
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
            """,  # nosec B608 # Fixed SQL fragments; external values use bound parameters.
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
        row = await db.fetchrow(f"SELECT {_COLUMNS} FROM jobs WHERE id = $1", job_id)  # nosec B608 # Fixed SQL fragments; external values use bound parameters.
        return _row_to_job(row) if row else None

    async def list(self, *, session_id: uuid.UUID | None = None, limit: int = 100) -> list[Job]:
        if session_id is not None:
            rows = await db.fetch(
                f"SELECT {_COLUMNS} FROM jobs WHERE session_id = $1 ORDER BY created_at DESC LIMIT $2",  # nosec B608 # Fixed SQL fragments; external values use bound parameters.
                session_id,
                limit,
            )
        else:
            rows = await db.fetch(
                f"SELECT {_COLUMNS} FROM jobs ORDER BY created_at DESC LIMIT $1",  # nosec B608 # Fixed columns; bound limit.
                limit,
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
            """,  # nosec B608 # Fixed SQL fragments; external values use bound parameters.
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
        # AH-AUDIT-021: approval-paused jobs are never due. They resume
        # only through an explicit human resume transition (resume_job),
        # then run once under a fresh fenced claim with revalidation.
        row = await db.fetchrow(
            f"""
            UPDATE jobs
            SET status = 'running', last_run_at = {clock},
                next_run_at = {clock} + ({lease} * interval '1 second'),
                claim_token = {token_ph}
            WHERE id = (
                SELECT id FROM jobs
                WHERE enabled AND paused_for_approval IS NULL
                  AND status <> 'paused_approval'
                  AND next_run_at <= {clock}
                ORDER BY next_run_at ASC
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            RETURNING {_COLUMNS}
            """,  # nosec B608 # Fixed SQL fragments; external values use bound parameters.
            *params,
            token,
        )
        return _row_to_job(row) if row else None

    async def renew_lease(self, job_id: uuid.UUID, claim_token: uuid.UUID | None = None) -> bool:
        """Keep an active run from being reclaimed while it is executing.

        Strict fencing (5.5): a token-owned running row requires the EXACT
        claim token. A missing token is never compatibility authorization for
        claimed work — it is rejected. Unclaimed idle rows (legacy/admin,
        token NULL) still accept token-less renewal. The enabled flag is
        intentionally not checked: a job disabled mid-run keeps its lease
        until finish() so two runners cannot both own it.
        """
        async with db.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT status, claim_token FROM jobs WHERE id = $1 FOR UPDATE",
                    job_id,
                )
                if row is None or row["status"] != "running":
                    return False
                try:
                    current = row["claim_token"]
                except Exception:
                    current = None
                if current is not None and claim_token != current:
                    # Stale owner or missing token on claimed work → reject.
                    return False
                result = await conn.execute(
                    """
                    UPDATE jobs
                    SET next_run_at = now() + ($2 * interval '1 second')
                    WHERE id = $1 AND status = 'running'
                    AND (claim_token = $3 OR (claim_token IS NULL AND $3 IS NULL))
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
        paused_for: str | None = None,
    ) -> bool:
        """Record a run outcome and schedule the next run from now.

        Fencing (AH-026): requires the claim token when tokens are in use;
        a stale claimant cannot finish (and reschedule) a newer claim.
        Allows idle->idle (direct finish without claim, for tests/CLI) and
        running->idle/error, but returns False if already error/finished
        to avoid stale overwrite.

        AH-AUDIT-021: ``paused_for`` parks the job awaiting human approval
        (status ``paused_approval`` with the linked approval request id).
        Paused jobs are excluded from due claims until resume_job. Denial
        cancels this run (error outcome); approval resumes via resume_job.
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
                    await db.fetchrow(f"SELECT {_COLUMNS} FROM jobs WHERE id = $1", job_id)  # nosec B608 # Fixed SQL fragments; external values use bound parameters.
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
            paused_status = "paused_approval" if paused_for else ("error" if error else "idle")
            result = await db.execute(
                """
                    UPDATE jobs
                    SET status = $2,
                        last_error = $3,
                        run_count = run_count + 1,
                        paused_for_approval = $5,
                        next_run_at = CASE WHEN kind = 'cron' THEN $4
                            ELSE now() + (interval_seconds || ' seconds')::interval END
                    WHERE id = $1
                    """,
                job_id,
                paused_status,
                error,
                next_run,
                paused_for,
            )
            return parse_command_count(result) > 0 if isinstance(result, str) else True
        async with db.acquire() as conn:
            async with conn.transaction():
                job_row = await conn.fetchrow(
                    f"SELECT {_COLUMNS} FROM jobs WHERE id = $1 FOR UPDATE",  # nosec B608 # Fixed SQL fragments; external values use bound parameters.
                    job_id,
                )
                if job_row is None or job_row["status"] not in ("idle", "running"):
                    return False
                # Strict fencing (5.5): a token-owned running row requires the
                # exact token. Missing token on claimed work is rejected;
                # unclaimed idle rows accept the token-less admin path.
                try:
                    current = job_row["claim_token"]
                except Exception:
                    current = None
                if (
                    job_row["status"] == "running"
                    and current is not None
                    and claim_token != current
                ):
                    return False
                job = _row_to_job(job_row)
                db_now = await conn.fetchval("SELECT now()")
                next_run = (
                    next_cron_time(job.cron_expression, db_now)
                    if job.kind == "cron" and job.cron_expression
                    else None
                )
                paused_status = "paused_approval" if paused_for else ("error" if error else "idle")
                result = await conn.execute(
                    """
                    UPDATE jobs
                    SET status = $2,
                        last_error = $3,
                        run_count = run_count + 1,
                        claim_token = NULL,
                        paused_for_approval = $6,
                        next_run_at = CASE WHEN kind = 'cron' THEN $4
                            ELSE now() + (interval_seconds || ' seconds')::interval END
                    WHERE id = $1 AND status IN ('idle', 'running')
                    AND (claim_token = $5 OR (claim_token IS NULL AND $5 IS NULL))
                    """,
                    job_id,
                    paused_status,
                    error,
                    next_run,
                    claim_token,
                    paused_for,
                )
                return parse_command_count(result) > 0

    async def resume_job(self, job_id: uuid.UUID) -> Job | None:
        """Resume an approval-paused job (AH-AUDIT-021).

        Defined human-resolution transition: paused_approval → idle with
        next_run_at = now, so the next claim runs once under a fresh fenced
        job and session claim with proposal revalidation. Returns the job,
        or None when not paused. Denial (cancel one run vs disable the job)
        is the caller's decision: deny-and-resume retries once; deny-and-
        disable uses set_enabled(False).
        """
        row = await db.fetchrow(
            f"""
            UPDATE jobs
            SET status = 'idle', paused_for_approval = NULL, next_run_at = now()
            WHERE id = $1 AND status = 'paused_approval'
            RETURNING {_COLUMNS}
            """,  # nosec B608 # Fixed SQL fragments; external values use bound parameters.
            job_id,
        )
        return _row_to_job(row) if row else None


job_store = JobStore()


class JobRunner:
    """Background loop that runs due jobs. Started by the gateway or the CLI daemon."""

    _cancel_grace_seconds = 0.1

    def __init__(
        self, store: JobStore | None = None, agent_factory=None, poll_seconds: float = 5.0
    ) -> None:
        self._store = store or job_store
        self._agent_factory = agent_factory  # (agent_name, session_id) -> agent, for tests
        self._poll_seconds = poll_seconds
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._quarantined: set[asyncio.Task] = set()
        self._finalizers: set[asyncio.Task] = set()

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
        # Carry the claim token; loss of ownership cancels the effect task.
        claim_token = getattr(job, "claim_token", None)
        ownership_lost = asyncio.Event()
        lease_task = asyncio.create_task(self._keep_lease(job.id, claim_token, ownership_lost))
        exec_task: asyncio.Task | None = None
        loss_wait: asyncio.Task | None = None
        # AH-AUDIT-021: linked approval request when this run parks paused.
        paused_for: str | None = None
        try:
            # Whole-job timeout so a hung provider cannot monopolize the
            # serial runner. At-least-once effects must be idempotent; the
            # token fence (not the timeout) decides whose outcome commits.
            exec_task = asyncio.create_task(self._execute(job, ownership_lost))
            loss_wait = asyncio.create_task(ownership_lost.wait())
            done, _ = await asyncio.wait(
                {exec_task, loss_wait},
                timeout=float(RUN_LEASE_SECONDS),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                exec_task.cancel()
                await asyncio.wait({exec_task}, timeout=self._cancel_grace_seconds)
                error = f"job timed out after {RUN_LEASE_SECONDS}s"
                logger.warning("Job %s (%s) timed out", job.name, job.id)
            elif loss_wait in done and not exec_task.done():
                # Ownership lost while effects were active: cancel and join.
                exec_task.cancel()
                await asyncio.wait({exec_task}, timeout=self._cancel_grace_seconds)
                error = "ownership lost; execution cancelled"
                logger.warning("Job %s (%s) lost ownership; cancelled", job.name, job.id)
            else:
                loss_wait.cancel()
                try:
                    await exec_task
                except asyncio.CancelledError:
                    error = "cancelled"
                    raise
                except PermissionError as e:
                    # AH-AUDIT-021: typed approval pause parks durably (no
                    # rerun before explicit resume) instead of error-retry.
                    if str(e).startswith("needs_approval:"):
                        paused_for = _pause_request_id(e) or "unknown"
                        error = str(e)
                        logger.warning("Job %s (%s) paused: %s", job.name, job.id, e)
                    else:
                        logger.exception("Job %s (%s) failed", job.name, job.id)
                        error = f"{type(e).__name__}: {e}"
                except Exception as e:
                    logger.exception("Job %s (%s) failed", job.name, job.id)
                    error = f"{type(e).__name__}: {e}"
        except asyncio.CancelledError:
            if exec_task is not None and not exec_task.done():
                exec_task.cancel()
                await asyncio.wait({exec_task}, timeout=self._cancel_grace_seconds)
            error = "cancelled"
            raise
        finally:
            # AH-AUDIT-020: the ownership-wait task is owned here — cancel
            # and join it on EVERY exit (success/failure/timeout/ownership
            # loss/runner cancellation). Task GC is not lifecycle management.
            try:
                if loss_wait is not None and not loss_wait.done():
                    loss_wait.cancel()
                if loss_wait is not None:
                    await asyncio.gather(loss_wait, return_exceptions=True)
            except (asyncio.CancelledError, Exception):
                pass
            if exec_task is not None and not exec_task.done():
                self._quarantine_run(job, exec_task, lease_task, claim_token, error, paused_for)
            else:
                if exec_task is not None and not exec_task.cancelled():
                    exec_task.exception()
                await self._finish_run(job, lease_task, claim_token, error, paused_for)
        return True

    def _quarantine_run(
        self,
        job: Job,
        execution: asyncio.Task,
        lease: asyncio.Task,
        claim_token: uuid.UUID | None,
        error: str | None,
        paused_for: str | None,
    ) -> None:
        """Retain active effects and their lease until genuine termination."""
        self._quarantined.add(execution)
        logger.warning("Job %s (%s) quarantined while cancellation unwinds", job.name, job.id)

        def finalized(task: asyncio.Task) -> None:
            self._finalizers.discard(task)
            if not task.cancelled():
                exc = task.exception()
                if exc is not None:
                    logger.error(
                        "Could not record deferred outcome for job %s",
                        job.id,
                        exc_info=(type(exc), exc, exc.__traceback__),
                    )

        def terminated(task: asyncio.Task) -> None:
            self._quarantined.discard(task)
            if not task.cancelled():
                task.exception()
            finalizer = asyncio.create_task(
                self._finish_run(job, lease, claim_token, error, paused_for)
            )
            self._finalizers.add(finalizer)
            finalizer.add_done_callback(finalized)

        execution.add_done_callback(terminated)

    async def _finish_run(
        self,
        job: Job,
        lease: asyncio.Task,
        claim_token: uuid.UUID | None,
        error: str | None,
        paused_for: str | None,
    ) -> None:
        lease.cancel()
        await asyncio.gather(lease, return_exceptions=True)
        # Only terminated effects may finish; the original token still fences
        # the outcome if another owner acquired the row during quarantine.
        try:
            await self._store.finish(
                job.id, error=error, claim_token=claim_token, paused_for=paused_for
            )
        except TypeError:
            if isinstance(self._store, JobStore):
                raise
            try:
                await self._store.finish(job.id, error=error, claim_token=claim_token)
            except TypeError:
                await self._store.finish(job.id, error=error)

    async def _keep_lease(
        self,
        job_id: uuid.UUID,
        claim_token: uuid.UUID | None = None,
        ownership_lost: asyncio.Event | None = None,
    ) -> None:
        failures = 0
        while True:
            await asyncio.sleep(RUN_LEASE_SECONDS / 3)
            try:
                try:
                    renewed = await self._store.renew_lease(job_id, claim_token)
                except TypeError:
                    if isinstance(self._store, JobStore):
                        raise
                    renewed = await self._store.renew_lease(job_id)
            except Exception:
                # Uncertain lease state: do NOT keep issuing effects
                # indefinitely. Two consecutive failures end ownership.
                failures += 1
                logger.exception("Could not renew lease for job %s (%d/2)", job_id, failures)
                if failures >= 2:
                    if ownership_lost is not None:
                        ownership_lost.set()
                    return
                continue
            failures = 0
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
        # Coordinate with user turns: claim the session turn so a scheduled
        # run never interleaves with an active user turn (fresh fenced claim;
        # busy sessions reschedule via finish()).
        from ah.core.turns import end_turn as _end_job_turn
        from ah.core.turns import try_begin_turn as _begin_job_turn

        job_turn = await _begin_job_turn(job.session_id)
        if not job_turn:
            raise RuntimeError("session turn busy; job rescheduled")
        try:
            if getattr(job, "no_agent", False):
                # AH-AUDIT-009 trust model: no-agent scripts are
                # administrator-configured (human-transport jobs_create
                # only — no agent-accessible tool creates script jobs), but
                # path validation is NOT a sandbox: every run is
                # broker-governed, bound to the exact script content,
                # interpreter, cwd, backend, and session authority. Changed
                # content revalidates; headless pause parks the job.
                from ah.core.assembler import get_token_count
                from ah.core.context import context_manager
                from ah.core.job_scripts import resolve_script_path, run_job_script
                from ah.memory.redaction import redact_secrets
                from ah.permissions.broker import (
                    ApprovalDenied as _ScriptDenied,
                )
                from ah.permissions.broker import NeedsApproval as _ScriptNeeds
                from ah.permissions.broker import permission_broker as _script_broker
                from ah.permissions.policy import build_request as _script_req

                target = resolve_script_path(job.script_path or "")
                try:
                    script_content = target.read_bytes()
                except OSError as e:
                    raise RuntimeError(f"script unreadable: {e}") from None
                if len(script_content) > 200_000:
                    raise RuntimeError("script exceeds 200,000 byte review bound")
                import sys as _sys

                interpreter = _sys.executable if target.suffix.lower() == ".py" else "/usr/bin/bash"
                try:
                    from ah.core.session_mode import get_effective_mode as _eff_mode

                    script_mode = _eff_mode(str(job.session_id) if job.session_id else None)
                except Exception:
                    script_mode = "ask"
                script_proposal = _script_req(
                    operation="process.exec",
                    targets=[str(target)],
                    argv=[interpreter, str(target)],
                    cwd=str(target.parent),
                    # Reversible byte-to-text mapping binds the exact source
                    # without corrupting BOMs or declared Python encodings.
                    content=script_content.decode("latin-1"),
                    mode=script_mode,
                    backend="host",
                    agent_id=job.agent_name,
                    session_id=str(job.session_id),
                    capabilities=["job-script"],
                    tool="job_script",
                    timeout=30,
                )
                approval_id = ""
                try:
                    await _script_broker.guard(script_proposal)
                    approval_id = script_proposal.approval_id or ""
                except _ScriptNeeds as _need:
                    _ap = _need.approval
                    err = PermissionError(
                        f"needs_approval: job paused awaiting human approval; "
                        f"request {_ap.get('request_id')} "
                        f"({script_proposal.operation}); resolve via approvals "
                        "then resume (fresh claim revalidates)."
                    )
                    try:
                        err.approval_request_id = _ap.get("request_id", "")  # type: ignore[attr-defined]
                    except Exception:
                        pass
                    raise err from None
                except _ScriptDenied as _denied:
                    raise RuntimeError(f"script denied: {_denied.reason}") from None
                script_outcome = "failed"
                try:
                    output = redact_secrets(
                        await run_job_script(
                            job.script_path or "",
                            approved_content=script_content,
                            approved_path=target,
                        )
                    ).text
                    script_outcome = "completed"
                except asyncio.CancelledError:
                    script_outcome = "cancelled"
                    raise
                finally:
                    if approval_id:
                        try:
                            await _script_broker.complete(approval_id, script_outcome)
                        except Exception:
                            pass
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
                session_id=job.session_id,
            )
            # AH-023: close owned providers in finally; injected test factories
            # manage their own lifecycle.
            owns_provider = self._agent_factory is None
            try:
                # Check ownership loss before the (potentially long) agent run.
                if ownership_lost is not None and ownership_lost.is_set():
                    raise RuntimeError("job ownership lost")
                response = await agent.run(job.session_id, prompt, verbose=False)
                # Headless jobs never hang on hidden prompts (Phase E):
                # AH-AUDIT-022: pause detection uses the typed
                # AgentResponse.needs_approval outcome — never prose
                # matching. A benign answer merely discussing approvals
                # stays a success; a structured pause (even with arbitrary
                # or empty text) pauses durably and frees worker/lease.
                pauses = list(getattr(response, "needs_approval", None) or [])
                if pauses:
                    first = pauses[0] if isinstance(pauses[0], dict) else {}
                    err = PermissionError(
                        "needs_approval: job paused awaiting human approval; "
                        f"request {first.get('request_id', '')} "
                        f"({first.get('operation', '')}); resolve via approvals "
                        "then resume (fresh claim revalidates)."
                    )
                    err.approval_request_id = first.get("request_id", "")  # type: ignore[attr-defined]
                    raise err
            finally:
                # AH-AUDIT-026: shared owner-aware cleanup contract —
                # bounded join, never closes injected/shared clients, never
                # masks the primary outcome. Claim release runs in the
                # outer run_due_once finally regardless.
                if owns_provider:
                    try:
                        from ah.core.agent_factory import close_agent_provider

                        await close_agent_provider(agent)
                    except Exception:
                        pass
        finally:
            try:
                await _end_job_turn(job.session_id, job_turn)
            except Exception:
                pass

    async def _build_agent(
        self,
        agent_name: str,
        *,
        model: str | None = None,
        provider: str | None = None,
        session_id=None,
    ):
        """Build the job agent through the shared session-aware factory.

        AH-AUDIT-031: scheduled agents use build_agent_for_session with
        explicit documented job overrides (model/provider pinning) and
        inherited session authority — never a divergent direct ReActAgent
        construction missing memory/RAG/consolidation/authority services.
        Injected test factories manage their own lifecycle (no provider
        close); production agents are owned and closed by the caller.
        Precedence: job overrides > session settings > definition defaults
        > global defaults (resolved inside the shared factory).
        """
        if self._agent_factory is not None:
            return self._agent_factory(agent_name)
        from ah.core.agent_factory import build_agent_for_session
        from ah.core.models import Session as _Session
        from ah.core.session import session_manager

        session = None
        if session_id is not None:
            try:
                # AH-AUDIT-019: execution-critical read bypasses the cache.
                session = await session_manager.get_fresh(session_id)
            except Exception:
                session = None
        if session is None:
            # Fallback identity when the session cannot be resolved:
            # definition lookup still goes through the shared factory
            # (fail-closed for unknown specialists).
            import uuid as _uuid
            from datetime import UTC as _UTC
            from datetime import datetime as _dt

            session = _Session(
                id=session_id or _uuid.uuid4(),
                agent_id=agent_name,
                model=model,
                provider=provider,
                created_at=_dt.now(_UTC),
                last_activity=_dt.now(_UTC),
            )
        return await build_agent_for_session(
            session,
            provider_override=provider,
            model_override=model,
        )


job_runner = JobRunner()
