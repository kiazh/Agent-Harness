"""Session lifecycle, prompt streaming, context, and usage endpoints (moved verbatim from app.py; assembled by create_app)."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from machina.api.auth import require_api_key
from machina.api.routes.models import CreateSessionRequest, PromptRequest, UpdateSessionRequest
from machina.api.routes.shared import _redact_value
from machina.api.routes.streaming import _stream_session
from machina.core.config import config
from machina.core.context import context_manager
from machina.core.session import session_manager
from machina.gateway.serializers import chunk_preview, history_from_chunks, session_to_dict
from machina.gateway.server import PROVIDERS
from machina.memory.redaction import redact_secrets
from machina.observability.diagnostics import record_failure
from machina.services import session_context_window

router = APIRouter()


@router.get("/api/v1/sessions/{session_id}/usage", dependencies=[Depends(require_api_key)])
async def get_session_usage(session_id: uuid.UUID, agent: str | None = None) -> dict[str, Any]:
    """Get token usage summary for a session, optionally filtered by agent."""
    from machina.core.usage import usage_store

    session = await session_manager.get(session_id)
    if session is None:
        raise HTTPException(404, "session not found")
    return await usage_store.summary(session_id, agent or session.agent_id)


@router.patch("/api/v1/sessions/{session_id}", dependencies=[Depends(require_api_key)])
async def update_session(session_id: uuid.UUID, req: UpdateSessionRequest) -> dict[str, Any]:
    """Update session title, goal, or emotion state."""
    if req.title is None and req.goal is None and req.emotion is None:
        raise HTTPException(400, "title, goal, or emotion is required")
    session = await session_manager.get(session_id)
    if session is None:
        raise HTTPException(404, "session not found")
    from machina.core.turns import mutation_scope

    async with mutation_scope(session_id) as token:
        if token is None:
            raise HTTPException(409, "stop the running reply before changing this session")
        if req.emotion is not None:
            from machina.memory.persona import EmotionTopology

            if req.emotion and req.emotion not in EmotionTopology.EMOTION_PROFILES:
                raise HTTPException(400, "unknown emotion")
            # AH-AUDIT-018: transactional field merge — concurrent
            # compaction/watermark writes are preserved, never clobbered.
            await session_manager.update_state_fields(session_id, {"emotion": req.emotion or None})
        if req.title is not None:
            await session_manager.set_title(session_id, req.title)
        if req.goal is not None:
            await session_manager.set_goal(session_id, req.goal)
        return {"session": session_to_dict(await session_manager.get(session_id))}


@router.delete("/api/v1/sessions/{session_id}", dependencies=[Depends(require_api_key)])
async def delete_session(session_id: uuid.UUID) -> dict[str, bool]:
    """Delete a session by ID (mutation claim; live turns reject first)."""
    from machina.core.turns import begin_mutation, end_mutation, turn_active

    if await turn_active(session_id):
        raise HTTPException(409, "a turn is already running for this session")
    token = await begin_mutation(session_id)
    if not token:
        raise HTTPException(409, "a turn is already running for this session")
    try:
        if await turn_active(session_id):
            raise HTTPException(409, "a turn is already running for this session")
        if not await session_manager.delete(session_id):
            raise HTTPException(404, "session not found")
        return {"deleted": True}
    finally:
        await end_mutation(session_id, token)


@router.post("/api/v1/sessions", dependencies=[Depends(require_api_key)])
# legacy alias — preserved per the removal plan in routes/__init__.py; do not remove.
@router.post("/sessions", dependencies=[Depends(require_api_key)])
async def create_session(req: CreateSessionRequest) -> dict[str, Any]:
    """Create a new session."""
    from machina.core.agent_def import agent_registry
    from machina.core.agent_factory import resolve_provider_model
    from machina.memory.persona import EmotionTopology

    if req.emotion and req.emotion not in EmotionTopology.EMOTION_PROFILES:
        raise HTTPException(400, "unknown emotion")
    if req.provider is not None and req.provider not in PROVIDERS:
        raise HTTPException(400, f"unknown provider: {req.provider}")
    if req.model is not None and not req.model.strip():
        raise HTTPException(400, "model must be a non-empty string")
    try:
        definition = await agent_registry.get("harness")
    except Exception:
        # The default agent shares the factory's permitted registry fallback.
        definition = None
    provider, model = resolve_provider_model(
        definition=definition, provider_override=req.provider, model_override=req.model
    )
    session = await session_manager.create(
        title=req.title,
        model=model,
        provider=provider,
        goal=req.goal,
        context_budget=config.get("context_budget"),
        state={"emotion": req.emotion} if req.emotion else None,
    )
    try:
        from machina.gateway.features.mode import grant_full_default

        await grant_full_default(session)
    except Exception as _boundary_error:
        # Optional fallback preserves the primary outcome; report no payload.
        record_failure("app.create_app", _boundary_error)
    return {"session": session_to_dict(session)}


@router.get("/api/v1/sessions", dependencies=[Depends(require_api_key)])
# legacy alias — preserved per the removal plan in routes/__init__.py; do not remove.
@router.get("/sessions", dependencies=[Depends(require_api_key)])
async def list_sessions(limit: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
    """List sessions."""
    sessions = await session_manager.list_sessions(limit=limit)
    return {"sessions": [session_to_dict(s) for s in sessions]}


@router.get("/api/v1/sessions/{session_id}", dependencies=[Depends(require_api_key)])
# legacy alias — preserved per the removal plan in routes/__init__.py; do not remove.
@router.get("/sessions/{session_id}", dependencies=[Depends(require_api_key)])
async def get_session(session_id: str) -> dict[str, Any]:
    """Get a session by ID."""
    try:
        sid = uuid.UUID(session_id)
    except ValueError:
        raise HTTPException(400, "invalid session ID") from None
    session = await session_manager.get(sid)
    if session is None:
        raise HTTPException(404, "session not found")
    chunks = await context_manager.get_chunks(sid, limit=200)
    return {
        "session": session_to_dict(session),
        "history": [_redact_value(h) for h in history_from_chunks(chunks)],
    }


@router.post("/api/v1/sessions/{session_id}/chat", dependencies=[Depends(require_api_key)])
@router.post("/api/v1/sessions/{session_id}/prompt", dependencies=[Depends(require_api_key)])
# legacy alias — preserved per the removal plan in routes/__init__.py; do not remove.
@router.post("/sessions/{session_id}/prompt", dependencies=[Depends(require_api_key)])
async def prompt_session(session_id: str, req: PromptRequest) -> StreamingResponse:
    return await _stream_session(session_id, req)


@router.get("/api/v1/sessions/{session_id}/context", dependencies=[Depends(require_api_key)])
# legacy alias — preserved per the removal plan in routes/__init__.py; do not remove.
@router.get("/sessions/{session_id}/context", dependencies=[Depends(require_api_key)])
async def get_context(session_id: str, limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
    try:
        sid = uuid.UUID(session_id)
    except ValueError:
        raise HTTPException(400, "invalid session ID") from None
    session = await session_manager.get(sid)
    if session is None:
        raise HTTPException(404, "session not found")
    chunks = await context_manager.get_chunks(sid, limit=limit)
    return {
        "chunks": [
            {
                "id": str(c.id),
                "type": c.chunk_type,
                "agent": c.agent_id,
                "tokens": c.token_count,
                "createdAt": c.created_at.isoformat() if c.created_at else None,
                "preview": redact_secrets(chunk_preview(c)).text,
            }
            for c in chunks
        ],
        "totalTokens": await context_manager.get_token_usage(sid),
        "budget": await session_context_window(session),
        "goal": session.goal,
    }
