"""Human-approval list/resolve/resume endpoints (moved verbatim from app.py; assembled by create_app)."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from machina.api.auth import require_api_key
from machina.api.routes.models import PromptRequest, ResolveApprovalRequest, ResumeApprovalRequest
from machina.api.routes.streaming import _stream_session
from machina.core.session import session_manager

router = APIRouter()


@router.get("/api/v1/approvals", dependencies=[Depends(require_api_key)])
async def list_approvals(sessionId: uuid.UUID) -> dict[str, Any]:
    """List pending human approvals for a session (poll-based recovery)."""
    from machina.permissions import store as _store

    session = await session_manager.get(sessionId)
    if session is None:
        raise HTTPException(404, "session not found")
    return {"approvals": await _store.list_pending(str(sessionId))}


@router.post("/api/v1/approvals/{request_id}/resolve", dependencies=[Depends(require_api_key)])
async def resolve_approval(request_id: str, req: ResolveApprovalRequest) -> dict[str, Any]:
    """Resolve a pending approval. Ownership enforced via stored session binding."""
    from machina.permissions import store as _store

    rec = await _store.get_approval(request_id)
    if rec is None:
        raise HTTPException(404, "approval not found")
    if req.sessionId is not None and str(rec.get("session_id")) != str(req.sessionId):
        raise HTTPException(403, "approval does not belong to this session")
    verdict = req.verdict.lower()
    if verdict not in ("approved", "denied", "cancelled", "expired"):
        raise HTTPException(400, "verdict must be approved|denied|cancelled|expired")
    grant = (req.grant or "once").lower()
    if grant not in ("once", "session"):
        raise HTTPException(400, "grant must be once|session")
    # Authenticated API principal (never agent: prefix).
    resolved = await _store.resolve_decision(request_id, verdict, principal="http")
    if resolved is None:
        raise HTTPException(409, "approval already consumed or not permitted")
    if verdict == "approved" and grant == "session":
        targets = [t for t in str(rec.get("target", "")).split("|") if t]
        await _store.save_grant(
            {
                "session_id": rec.get("session_id", ""),
                "agent_id": rec.get("agent_id", ""),
                "mode": "ask",
                "capability": rec.get("operation", ""),
                "scope_path": targets[0] if targets else None,
                "scope_type": "file",
                "grant_kind": "session",
                "digest": rec.get("digest", ""),
            }
        )
    return {
        "approval": {"requestId": request_id, "status": verdict},
        "event": {
            "type": "approval.resolved",
            "requestId": request_id,
            "sessionId": rec.get("session_id"),
            "turnId": rec.get("turn_id"),
            "status": verdict,
        },
        "continuation": f"/api/v1/approvals/{request_id}/resume" if verdict == "approved" else None,
    }


@router.post("/api/v1/approvals/{request_id}/resume", dependencies=[Depends(require_api_key)])
async def resume_approval(request_id: str, req: ResumeApprovalRequest) -> StreamingResponse:
    from machina.core.approval_resume import matches_approval, resumed_request
    from machina.permissions import store
    from machina.tools.base import registry

    record = await store.get_approval(request_id)
    if record is None:
        raise HTTPException(404, "approval not found")
    if record.get("session_id") != str(req.sessionId) or record.get("turn_id") != req.turnId:
        raise HTTPException(403, "approval origin does not match")
    if record.get("status") != "approved" or record.get("consumed"):
        raise HTTPException(409, {"type": "approval.unavailable", "status": record.get("status")})
    if not record.get("proposal_complete") or not record["proposal"].get("tool"):
        raise HTTPException(409, "reviewable tool proposal required; request fresh approval")
    tool = registry._tools.get(record["proposal"]["tool"])
    if tool is None:
        raise HTTPException(409, "approved tool no longer available")
    try:
        registry._validate_tool_args(tool, req.toolArgs)
        action = await resumed_request(record, req.toolArgs)
    except ValueError:
        raise HTTPException(400, "invalid continuation arguments") from None
    if not matches_approval(action, record):
        fresh = await store.create_approval(action, principal="http")
        raise HTTPException(
            409,
            {
                "type": "needs_approval",
                "requestId": fresh["request_id"],
                "reason": "action changed; fresh approval required",
            },
        )
    return await _stream_session(
        str(req.sessionId),
        PromptRequest(text="approval continuation", protocolVersion=req.protocolVersion),
        resume=(record, req.toolArgs),
    )
