"""Memory endpoints (canonical and legacy session-scoped read) (moved verbatim from app.py; assembled by create_app)."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from machina.api.auth import require_api_key
from machina.api.routes.models import CreateMemoryRequest, CreatePersonaMemoryRequest
from machina.core.session import session_manager

router = APIRouter()


@router.post("/api/v1/memory", dependencies=[Depends(require_api_key)])
async def create_memory(req: CreateMemoryRequest) -> dict[str, Any]:
    """Create a new memory entry in the store."""
    from machina.memory.store import memory_store

    if req.category not in {"preference", "decision", "fact", "event", "transient"}:
        raise HTTPException(400, "invalid memory category")
    if req.sessionId is not None and await session_manager.get(req.sessionId) is None:
        raise HTTPException(404, "session not found")
    entry = await memory_store.add(
        session_id=req.sessionId,
        agent_id=req.agent,
        content=req.content,
        category=req.category,
        importance=req.importance,
        explicitly_important=True,
    )
    return {"memory": {"id": str(entry.id), "content": entry.content, "category": entry.category}}


@router.post("/api/v1/persona-memory", dependencies=[Depends(require_api_key)])
async def create_persona_memory(req: CreatePersonaMemoryRequest) -> dict[str, Any]:
    """Create a persona-specific interpretation of an existing fact."""
    from machina.memory.persona import persona_memory_store
    from machina.memory.store import memory_store

    fact = await memory_store.get(req.factId)
    if fact is None or fact.quarantined:
        raise HTTPException(404, "fact not found")
    entry = await persona_memory_store.add_persona(
        fact_id=req.factId,
        persona_id=req.personaId,
        interpretation=req.interpretation,
        emotional_valence=req.emotionalValence,
        emotional_arousal=req.emotionalArousal,
        confidence=req.confidence,
    )
    return {"personaMemory": {"id": str(entry.id), "factId": str(entry.fact_id)}}


@router.get("/api/v1/memory/search", dependencies=[Depends(require_api_key)])
async def search_memory(
    query: str = Query(..., min_length=1, max_length=2000),
    limit: int = Query(5, ge=1, le=50),
    agent: str | None = None,
    category: str | None = None,
    emotion: str | None = None,
) -> dict[str, Any]:
    """Search memories by hybrid semantic + keyword similarity.

    Reports the retrieval mode actually used: ``dense+sparse`` when a query
    embedding was prepared, ``keyword-only`` when no embedder is available.
    """
    from machina.memory.persona import EmotionTopology
    from machina.memory.retriever import MemoryRetrievalError, MemoryRetriever

    if emotion and emotion not in EmotionTopology.EMOTION_PROFILES:
        raise HTTPException(400, "unknown emotion")

    # H05: explicit searches prepare the dense query embedding like ordinary
    # agent preparation; None (no embedder) keeps retrieval keyword-only.
    query_embedding = await MemoryRetriever.prepare_query_embedding(query)
    try:
        results = await MemoryRetriever(top_k=limit).retrieve(
            query,
            agent_id=agent,
            category=category,
            emotion=emotion,
            query_embedding=query_embedding,
        )
    except MemoryRetrievalError as exc:
        # H06: a total retrieval outage is an error, not "no memories".
        raise HTTPException(503, f"memory retrieval unavailable: {exc}") from None
    return {
        "memories": [
            {
                "id": str(item.memory.id),
                "content": item.memory.content,
                "category": item.memory.category,
                "score": item.score,
                "personaInterpretation": item.persona_interpretation,
            }
            for item in results
        ],
        "mode": "dense+sparse" if query_embedding is not None else "keyword-only",
    }


# legacy alias — preserved per the removal plan in routes/__init__.py; do not remove.
@router.get("/sessions/{session_id}/memory", dependencies=[Depends(require_api_key)])
async def get_memory(session_id: str, limit: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
    try:
        sid = uuid.UUID(session_id)
    except ValueError:
        raise HTTPException(400, "invalid session ID") from None
    session = await session_manager.get(sid)
    if session is None:
        raise HTTPException(404, "session not found")
    from machina.memory.store import memory_store

    memories = await memory_store.search(session_id=sid, limit=limit)
    return {
        "memories": [
            {
                "id": str(m.id),
                "content": m.content,
                "category": m.category,
                "importance": round(m.importance, 3),
                "accessCount": m.access_count,
                "createdAt": m.created_at.isoformat() if m.created_at else None,
                "sessionId": str(m.session_id) if m.session_id else None,
            }
            for m in memories
        ],
        "total": await memory_store.count(session_id=sid),
    }
