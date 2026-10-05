"""Job/scheduler-related feature handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ah.gateway.errors import INVALID_PARAMS, NOT_FOUND, RpcError
from ah.gateway.features._common import _int, _str, _uuid

if TYPE_CHECKING:
    from ah.gateway.server import Gateway


async def jobs_create(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Create a new job."""
    gw.require_db()
    from ah.core.scheduler import DEFAULT_HEARTBEAT_PROMPT, job_store

    session = await gw.get_session(params)
    kind = params.get("kind", "interval")
    if kind not in ("heartbeat", "interval", "cron"):
        raise RpcError(INVALID_PARAMS, "kind must be 'heartbeat', 'interval', or 'cron'")
    no_agent = params.get("noAgent", False)
    if not isinstance(no_agent, bool):
        raise RpcError(INVALID_PARAMS, "noAgent must be a boolean")
    if not no_agent and "scriptPath" in params:
        raise RpcError(INVALID_PARAMS, "scriptPath requires noAgent=true")
    prompt = _str(params, "prompt", required=kind == "interval" and not no_agent, max_len=5000) or (
        "" if no_agent else DEFAULT_HEARTBEAT_PROMPT
    )
    try:
        job = await job_store.create(
            name=_str(params, "name", required=False, max_len=100) or f"{kind} job",
            kind=kind,
            session_id=session.id,
            prompt=prompt,
            interval_seconds=_int(params, "intervalSeconds", 300, 10, 86_400),
            agent_name=_str(params, "agent", required=False, max_len=100) or session.agent_id,
            cron_expression=_str(params, "cronExpression", required=kind == "cron", max_len=100)
            or None,
            model=_str(params, "model", max_len=200) if "model" in params else None,
            provider=_str(params, "provider", max_len=100) if "provider" in params else None,
            no_agent=no_agent,
            script_path=_str(params, "scriptPath", max_len=500) if no_agent else None,
        )
    except ValueError as e:
        raise RpcError(INVALID_PARAMS, str(e)) from None
    return {"job": job.to_dict()}


async def jobs_list(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """List jobs for a session."""
    gw.require_db()
    from ah.core.scheduler import job_store

    session_id = (await gw.get_session(params)).id if params.get("sessionId") else None
    jobs = await job_store.list(session_id=session_id, limit=_int(params, "limit", 100, 1, 500))
    return {"jobs": [j.to_dict() for j in jobs]}


async def jobs_set_enabled(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Enable or disable a job."""
    gw.require_db()
    from ah.core.scheduler import job_store

    enabled = params.get("enabled")
    if not isinstance(enabled, bool):
        raise RpcError(INVALID_PARAMS, "enabled must be a boolean")
    job_id = _uuid(params, "id")
    if params.get("sessionId"):
        session = await gw.get_session(params)
        owned = await job_store.get(job_id)
        if owned is None or owned.session_id != session.id:
            raise RpcError(NOT_FOUND, "job not found for this session")
    job = await job_store.set_enabled(job_id, enabled)
    if job is None:
        raise RpcError(NOT_FOUND, "job not found")
    return {"job": job.to_dict()}


async def jobs_delete(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Delete a job."""
    gw.require_db()
    from ah.core.scheduler import job_store

    job_id = _uuid(params, "id")
    if params.get("sessionId"):
        session = await gw.get_session(params)
        owned = await job_store.get(job_id)
        if owned is None or owned.session_id != session.id:
            raise RpcError(NOT_FOUND, "job not found for this session")
    if not await job_store.delete(job_id):
        raise RpcError(NOT_FOUND, "job not found")
    return {"deleted": True}
