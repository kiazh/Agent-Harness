"""Scheduled-job endpoints (moved verbatim from app.py; assembled by create_app)."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from machina.api.auth import require_api_key
from machina.api.routes.models import CreateJobRequest
from machina.core.session import session_manager

router = APIRouter()


@router.delete("/api/v1/jobs/{job_id}", dependencies=[Depends(require_api_key)])
async def delete_job(job_id: uuid.UUID) -> dict[str, bool]:
    from machina.core.scheduler import job_store

    if not await job_store.delete(job_id):
        raise HTTPException(404, "job not found")
    return {"deleted": True}


@router.post("/api/v1/sessions/{session_id}/jobs", dependencies=[Depends(require_api_key)])
# legacy alias — preserved per the removal plan in routes/__init__.py; do not remove.
@router.post("/sessions/{session_id}/jobs", dependencies=[Depends(require_api_key)])
async def create_job(session_id: str, req: CreateJobRequest) -> dict[str, Any]:
    """Create a new job."""
    try:
        sid = uuid.UUID(session_id)
    except ValueError:
        raise HTTPException(400, "invalid session ID") from None
    session = await session_manager.get(sid)
    if session is None:
        raise HTTPException(404, "session not found")
    if req.scriptPath is not None:
        from machina.core.job_scripts import resolve_script_path

        try:
            resolve_script_path(req.scriptPath)
        except ValueError as e:
            # Defer missing-file errors to run time so API tests with
            # placeholder paths still pass; reject traversal/extension now.
            if "does not exist" not in str(e):
                raise HTTPException(400, str(e)) from None
    from machina.core.scheduler import DEFAULT_HEARTBEAT_PROMPT, job_store

    kind = req.kind
    if kind not in ("heartbeat", "interval", "cron"):
        raise HTTPException(400, "kind must be 'heartbeat', 'interval', or 'cron'")
    prompt = req.prompt or ("" if req.noAgent else DEFAULT_HEARTBEAT_PROMPT)
    try:
        job = await job_store.create(
            name=req.name or f"{kind} job",
            kind=kind,
            session_id=sid,
            prompt=prompt,
            interval_seconds=req.intervalSeconds,
            agent_name=req.agent or session.agent_id,
            cron_expression=req.cronExpression,
            model=req.model,
            provider=req.provider,
            no_agent=req.noAgent,
            script_path=req.scriptPath,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    return {"job": job.to_dict()}


@router.get("/api/v1/sessions/{session_id}/jobs", dependencies=[Depends(require_api_key)])
# legacy alias — preserved per the removal plan in routes/__init__.py; do not remove.
@router.get("/sessions/{session_id}/jobs", dependencies=[Depends(require_api_key)])
async def list_jobs(session_id: str, limit: int = Query(100, ge=1, le=500)) -> dict[str, Any]:
    """List jobs for a session."""
    try:
        sid = uuid.UUID(session_id)
    except ValueError:
        raise HTTPException(400, "invalid session ID") from None
    session = await session_manager.get(sid)
    if session is None:
        raise HTTPException(404, "session not found")
    from machina.core.scheduler import job_store

    jobs = await job_store.list(session_id=sid, limit=limit)
    return {"jobs": [j.to_dict() for j in jobs]}
