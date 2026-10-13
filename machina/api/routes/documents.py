"""RAG document indexing and search endpoints (moved verbatim from app.py; assembled by create_app)."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from machina.api.auth import require_api_key
from machina.api.routes.models import CreateDocumentRequest
from machina.core.session import session_manager

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/api/v1/documents", dependencies=[Depends(require_api_key)])
async def index_document(req: CreateDocumentRequest) -> dict[str, Any]:
    """Index a document into the RAG pipeline."""
    from machina.rag.loaders import Document
    from machina.tools.rag import get_rag_pipeline

    session = await session_manager.get(req.sessionId)
    if session is None:
        raise HTTPException(404, "session not found")
    agent = req.agent or session.agent_id
    if agent != session.agent_id:
        raise HTTPException(403, "agent does not own this session")
    try:
        pipeline = await get_rag_pipeline()
        chunks = await pipeline.index_document(
            Document(content=req.content, source=req.source),
            session_id=req.sessionId,
            agent_id=agent,
            metadata=req.metadata,
        )
    except Exception:
        logger.exception("Document indexing failed")
        raise HTTPException(502, "document indexing failed") from None
    return {"chunksIndexed": len(chunks), "source": req.source}


@router.get("/api/v1/documents/search", dependencies=[Depends(require_api_key)])
async def search_documents(
    sessionId: uuid.UUID,
    query: str = Query(..., min_length=1, max_length=2000),
    topK: int = Query(5, ge=1, le=50),
    agent: str | None = None,
) -> dict[str, Any]:
    """Search indexed documents."""
    from machina.tools.rag import get_rag_pipeline

    session = await session_manager.get(sessionId)
    if session is None:
        raise HTTPException(404, "session not found")
    agent = agent or session.agent_id
    if agent != session.agent_id:
        raise HTTPException(403, "agent does not own this session")
    try:
        pipeline = await get_rag_pipeline()
        results = await pipeline.search(query=query, session_id=sessionId, top_k=topK)
    except Exception:
        logger.exception("Document search failed")
        raise HTTPException(502, "document search failed") from None
    return {
        "results": [
            {
                "id": str(item.chunk.id),
                "text": item.chunk.payload.get("text", ""),
                "source": item.chunk.payload.get("source", ""),
                "score": item.score,
            }
            for item in results
        ]
    }
