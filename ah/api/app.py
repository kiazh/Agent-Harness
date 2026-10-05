"""FastAPI application factory for the AgentHarness HTTP API.

Exposes REST endpoints that reuse the same core services (session_manager,
context_manager, memory_store, job_store, agent_registry) and the Gateway
class that the terminal UI drives via JSON-RPC.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from ah import __version__
from ah.api.auth import require_api_key
from ah.api.rate_limit import RateLimitMiddleware
from ah.core.config import config
from ah.core.context import context_manager
from ah.core.models import StreamEvent
from ah.core.session import session_manager
from ah.db.connection import db
from ah.gateway.errors import RpcError
from ah.gateway.serializers import chunk_preview, history_from_chunks, session_to_dict
from ah.gateway.server import Gateway

logger = logging.getLogger(__name__)


# ─── request / response models ──────────────────────────────────────────────


class CreateSessionRequest(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    model: str | None = Field(default=None, max_length=200)
    provider: str | None = Field(default=None, max_length=50)
    goal: str | None = Field(default=None, max_length=10_000)
    emotion: str | None = None


class UpdateSessionRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    goal: str | None = Field(default=None, max_length=10_000)
    emotion: str | None = None


class CreateJobRequest(BaseModel):
    name: str | None = Field(default=None, max_length=100)
    kind: str = "interval"
    prompt: str | None = Field(default=None, max_length=5000)
    intervalSeconds: int = Field(default=300, ge=10, le=86_400)
    agent: str | None = Field(default=None, min_length=1, max_length=100)
    cronExpression: str | None = Field(default=None, max_length=100)
    model: str | None = Field(default=None, min_length=1, max_length=200)
    provider: str | None = Field(default=None, min_length=1, max_length=100)
    noAgent: bool = False
    scriptPath: str | None = Field(default=None, min_length=1, max_length=500)


class PromptRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=20_000)
    verbose: bool = False


class CreateMemoryRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=20_000)
    category: str = "fact"
    importance: float = Field(default=0.5, ge=0, le=1)
    sessionId: uuid.UUID | None = None
    agent: str = "harness"


class CreatePersonaMemoryRequest(BaseModel):
    factId: uuid.UUID
    personaId: str = Field(..., min_length=1, max_length=100)
    interpretation: str = Field(..., min_length=1, max_length=20_000)
    emotionalValence: float = Field(default=0.0, ge=-1, le=1)
    emotionalArousal: float = Field(default=0.0, ge=0, le=1)
    confidence: float = Field(default=0.5, ge=0, le=1)


class CreateDocumentRequest(BaseModel):
    sessionId: uuid.UUID
    source: str = Field(..., min_length=1, max_length=500)
    content: str = Field(..., min_length=1, max_length=1_000_000)
    metadata: dict[str, Any] = Field(default_factory=dict)
    agent: str | None = None


class RpcRequest(BaseModel):
    method: str = Field(..., min_length=1, max_length=100)
    params: dict[str, Any] = Field(default_factory=dict)


# ─── gateway helper ────────────────────────────────────────────────────────


class _RpcGateway:
    """Minimal Gateway wrapper that captures the JSON-RPC response.

    The Gateway class expects a ``write`` callable that receives response
    frames. We capture the last frame with a matching ``id`` so the HTTP
    layer can return it to the client.

    Thread safety: uses an ``asyncio.Lock`` to protect ``_next_id`` increment
    and ``_responses`` dict access, making it safe for concurrent use across
    multiple HTTP requests.

    TTL cleanup: entries in ``_responses`` that are older than
    ``_RESPONSE_TTL_SECONDS`` are automatically cleaned up to prevent
    unbounded growth when responses never arrive.
    """

    _RESPONSE_TTL_SECONDS = 60.0

    def __init__(self) -> None:
        self._responses: dict[int, dict[str, Any]] = {}
        self._response_timestamps: dict[int, float] = {}
        self._next_id = 0
        self._lock = asyncio.Lock()
        self._gateway = Gateway(self._capture, owns_db=False)
        self._gateway._db_ready = db.connected

    def _capture(self, frame: dict[str, Any]) -> None:
        rid = frame.get("id")
        if rid is not None:
            self._responses[rid] = frame
            self._response_timestamps[rid] = asyncio.get_event_loop().time()

    def _cleanup_old_responses(self) -> None:
        """Remove entries from _responses that have exceeded the TTL."""
        now = asyncio.get_event_loop().time()
        expired = [
            rid
            for rid, ts in self._response_timestamps.items()
            if now - ts > self._RESPONSE_TTL_SECONDS
        ]
        for rid in expired:
            self._responses.pop(rid, None)
            self._response_timestamps.pop(rid, None)

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Call a gateway method via JSON-RPC and return the result.

        Args:
            method: The JSON-RPC method name to call.
            params: The parameters to pass to the method.

        Returns:
            The result dict from the gateway response.

        Raises:
            RpcError: If no response is received or the response contains an error.
        """
        async with self._lock:
            self._next_id += 1
            rid = self._next_id
            self._cleanup_old_responses()
            frame = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
            await self._gateway.handle_line(json.dumps(frame))
            response = self._responses.pop(rid, None)
            self._response_timestamps.pop(rid, None)
        if response is None:
            raise RpcError(-32603, "no response from gateway")
        if "error" in response:
            err = response["error"]
            raise RpcError(err.get("code", -32603), err.get("message", "unknown error"))
        return response.get("result", {})

    async def close(self) -> None:
        """Close the underlying gateway connection."""
        await self._gateway.close()


# ─── lifespan ──────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Connect to the database on startup and close on shutdown."""
    from ah.observability.audit import audit_persistence
    from ah.plugins.loader import load_plugins

    load_plugins()
    runner = None
    rpc_gw = None
    try:
        await db.connect()
        audit_persistence.start()
        from ah.core.scheduler import JobRunner

        runner = JobRunner()
        runner.start()
        # Create a single _RpcGateway for all RPC dispatches
        rpc_gw = _RpcGateway()
        app.state._rpc_gateway = rpc_gw
    except Exception as e:
        logger.warning("Database not available: %s", e)
    yield
    if runner is not None:
        await runner.stop()
    if rpc_gw is not None:
        await rpc_gw.close()
    await audit_persistence.stop()
    await db.close()


# ─── app factory ───────────────────────────────────────────────────────────


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="AgentHarness API",
        version=__version__,
        lifespan=lifespan,
    )
    app.add_middleware(RateLimitMiddleware)

    # ── health ──────────────────────────────────────────────────────────────

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Return service health status and version."""
        return {"status": "ok", "version": __version__}

    @app.get("/ready")
    async def ready() -> dict[str, str]:
        """Check if the database is connected and schema is initialized."""
        if not db.connected:
            raise HTTPException(503, "database unavailable")
        try:
            schema_ready = await db.fetchval(
                "SELECT to_regclass('sessions') IS NOT NULL "
                "AND to_regclass('jobs') IS NOT NULL "
                "AND to_regclass('audit_events') IS NOT NULL "
                "AND to_regclass('llm_usage') IS NOT NULL"
            )
        except Exception:
            raise HTTPException(503, "database unavailable") from None
        if not schema_ready:
            raise HTTPException(503, "database schema is not initialized")
        return {"status": "ready"}

    @app.get("/metrics", dependencies=[Depends(require_api_key)])
    async def prometheus_metrics() -> PlainTextResponse:
        """Return Prometheus-formatted metrics including LLM usage totals."""
        from ah.core.usage import usage_store
        from ah.observability.metrics import prometheus_text

        body = prometheus_text()
        if db.connected:
            totals = await usage_store.totals()
            body += (
                "# HELP ah_llm_usage_requests_total Durable LLM call attempts.\n"
                "# TYPE ah_llm_usage_requests_total counter\n"
                f"ah_llm_usage_requests_total {totals['requests']}\n"
                "# HELP ah_llm_usage_accounted_tokens_total Known tokens or conservative reservations.\n"
                "# TYPE ah_llm_usage_accounted_tokens_total counter\n"
                f"ah_llm_usage_accounted_tokens_total {totals['accounted_tokens']}\n"
                "# HELP ah_llm_usage_unknown_calls Calls without provider token usage.\n"
                "# TYPE ah_llm_usage_unknown_calls gauge\n"
                f"ah_llm_usage_unknown_calls {totals['unknown_calls']}\n"
            )
        return PlainTextResponse(body, media_type="text/plain; version=0.0.4")

    @app.get("/api/v1/sessions/{session_id}/usage", dependencies=[Depends(require_api_key)])
    async def get_session_usage(session_id: uuid.UUID, agent: str | None = None) -> dict[str, Any]:
        """Get token usage summary for a session, optionally filtered by agent."""
        from ah.core.usage import usage_store

        session = await session_manager.get(session_id)
        if session is None:
            raise HTTPException(404, "session not found")
        return await usage_store.summary(session_id, agent or session.agent_id)

    @app.patch("/api/v1/sessions/{session_id}", dependencies=[Depends(require_api_key)])
    async def update_session(session_id: uuid.UUID, req: UpdateSessionRequest) -> dict[str, Any]:
        """Update session title, goal, or emotion state."""
        if req.title is None and req.goal is None and req.emotion is None:
            raise HTTPException(400, "title, goal, or emotion is required")
        session = await session_manager.get(session_id)
        if session is None:
            raise HTTPException(404, "session not found")
        if req.emotion is not None:
            from ah.memory.persona import EmotionTopology

            if req.emotion and req.emotion not in EmotionTopology.EMOTION_PROFILES:
                raise HTTPException(400, "unknown emotion")
            await session_manager.update_state(
                session_id,
                {
                    **session.state,
                    "emotion": req.emotion or None,
                },
            )
        if req.title is not None:
            await session_manager.set_title(session_id, req.title)
        if req.goal is not None:
            await session_manager.set_goal(session_id, req.goal)
        return {"session": session_to_dict(await session_manager.get(session_id))}

    @app.delete("/api/v1/sessions/{session_id}", dependencies=[Depends(require_api_key)])
    async def delete_session(session_id: uuid.UUID) -> dict[str, bool]:
        """Delete a session by ID."""
        if not await session_manager.delete(session_id):
            raise HTTPException(404, "session not found")
        return {"deleted": True}

    @app.post("/api/v1/memory", dependencies=[Depends(require_api_key)])
    async def create_memory(req: CreateMemoryRequest) -> dict[str, Any]:
        """Create a new memory entry in the store."""
        from ah.memory.store import memory_store

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
        return {
            "memory": {"id": str(entry.id), "content": entry.content, "category": entry.category}
        }

    @app.post("/api/v1/persona-memory", dependencies=[Depends(require_api_key)])
    async def create_persona_memory(req: CreatePersonaMemoryRequest) -> dict[str, Any]:
        """Create a persona-specific interpretation of an existing fact."""
        from ah.memory.persona import persona_memory_store
        from ah.memory.store import memory_store

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

    @app.get("/api/v1/memory/search", dependencies=[Depends(require_api_key)])
    async def search_memory(
        query: str = Query(..., min_length=1, max_length=2000),
        limit: int = Query(5, ge=1, le=50),
        agent: str | None = None,
        category: str | None = None,
        emotion: str | None = None,
    ) -> dict[str, Any]:
        """Search memories by semantic similarity with optional filters."""
        from ah.memory.persona import EmotionTopology
        from ah.memory.retriever import MemoryRetriever

        if emotion and emotion not in EmotionTopology.EMOTION_PROFILES:
            raise HTTPException(400, "unknown emotion")

        results = await MemoryRetriever(top_k=limit).retrieve(
            query, agent_id=agent, category=category, emotion=emotion
        )
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
            ]
        }

    @app.delete("/api/v1/jobs/{job_id}", dependencies=[Depends(require_api_key)])
    async def delete_job(job_id: uuid.UUID) -> dict[str, bool]:
        from ah.core.scheduler import job_store

        if not await job_store.delete(job_id):
            raise HTTPException(404, "job not found")
        return {"deleted": True}

    # ── sessions ────────────────────────────────────────────────────────────

    @app.post("/api/v1/sessions", dependencies=[Depends(require_api_key)])
    @app.post("/sessions", dependencies=[Depends(require_api_key)])
    async def create_session(req: CreateSessionRequest) -> dict[str, Any]:
        """Create a new session."""
        from ah.memory.persona import EmotionTopology

        if req.emotion and req.emotion not in EmotionTopology.EMOTION_PROFILES:
            raise HTTPException(400, "unknown emotion")
        session = await session_manager.create(
            title=req.title,
            model=req.model or config.get("model"),
            provider=req.provider or config.get("provider"),
            goal=req.goal,
            context_budget=config.get("context_budget"),
            state={"emotion": req.emotion} if req.emotion else None,
        )
        return {"session": session_to_dict(session)}

    @app.get("/api/v1/sessions", dependencies=[Depends(require_api_key)])
    @app.get("/sessions", dependencies=[Depends(require_api_key)])
    async def list_sessions(limit: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
        """List sessions."""
        sessions = await session_manager.list_sessions(limit=limit)
        return {"sessions": [session_to_dict(s) for s in sessions]}

    @app.get("/api/v1/sessions/{session_id}", dependencies=[Depends(require_api_key)])
    @app.get("/sessions/{session_id}", dependencies=[Depends(require_api_key)])
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
            "history": history_from_chunks(chunks),
        }

    @app.post("/api/v1/sessions/{session_id}/chat", dependencies=[Depends(require_api_key)])
    @app.post("/sessions/{session_id}/prompt", dependencies=[Depends(require_api_key)])
    async def prompt_session(session_id: str, req: PromptRequest) -> StreamingResponse:
        """Stream agent events as Server-Sent Events."""
        try:
            sid = uuid.UUID(session_id)
        except ValueError:
            raise HTTPException(400, "invalid session ID") from None
        session = await session_manager.get(sid)
        if session is None:
            raise HTTPException(404, "session not found")

        async def event_stream() -> AsyncGenerator[str, None]:
            from ah.core.agent import ReActAgent
            from ah.core.provider import get_provider

            llm = None
            agent = None
            try:
                llm = get_provider(
                    provider=session.provider or config.get("provider"),
                    model=session.model or config.get("model"),
                )
                agent = ReActAgent(
                    provider=llm,
                    max_iterations=config.get("max_iterations"),
                    agent_id=session.agent_id,
                )
                async for event in agent.run_stream(sid, req.text, verbose=req.verbose):
                    data = _serialize_event(event)
                    yield f"data: {json.dumps(data)}\n\n"
            except Exception:
                logger.exception("Prompt stream failed for session %s", session_id)
                error_data = {"type": "error", "message": "prompt failed; check server logs"}
                yield f"data: {json.dumps(error_data)}\n\n"
            finally:
                try:
                    if agent is not None:
                        await asyncio.gather(
                            *getattr(agent, "_learning_tasks", ()), return_exceptions=True
                        )
                finally:
                    if llm is not None:
                        try:
                            await llm.close()
                        except Exception:
                            logger.exception(
                                "Could not close prompt provider for session %s", session_id
                            )
                yield "data: [DONE]\n\n"

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )

    @app.get("/api/v1/sessions/{session_id}/context", dependencies=[Depends(require_api_key)])
    @app.get("/sessions/{session_id}/context", dependencies=[Depends(require_api_key)])
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
                    "preview": chunk_preview(c),
                }
                for c in chunks
            ],
            "totalTokens": await context_manager.get_token_usage(sid),
            "budget": session.context_budget,
            "goal": session.goal,
        }

    @app.get("/sessions/{session_id}/memory", dependencies=[Depends(require_api_key)])
    async def get_memory(session_id: str, limit: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
        try:
            sid = uuid.UUID(session_id)
        except ValueError:
            raise HTTPException(400, "invalid session ID") from None
        session = await session_manager.get(sid)
        if session is None:
            raise HTTPException(404, "session not found")
        from ah.memory.store import memory_store

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

    @app.post("/api/v1/documents", dependencies=[Depends(require_api_key)])
    async def index_document(req: CreateDocumentRequest) -> dict[str, Any]:
        """Index a document into the RAG pipeline."""
        from ah.rag.loaders import Document
        from ah.tools.rag import get_rag_pipeline

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
                metadata=req.metadata,
            )
        except Exception:
            logger.exception("Document indexing failed")
            raise HTTPException(502, "document indexing failed") from None
        return {"chunksIndexed": len(chunks), "source": req.source}

    @app.get("/api/v1/documents/search", dependencies=[Depends(require_api_key)])
    async def search_documents(
        sessionId: uuid.UUID,
        query: str = Query(..., min_length=1, max_length=2000),
        topK: int = Query(5, ge=1, le=50),
        agent: str | None = None,
    ) -> dict[str, Any]:
        """Search indexed documents."""
        from ah.tools.rag import get_rag_pipeline

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

    @app.post("/api/v1/sessions/{session_id}/jobs", dependencies=[Depends(require_api_key)])
    @app.post("/sessions/{session_id}/jobs", dependencies=[Depends(require_api_key)])
    async def create_job(session_id: str, req: CreateJobRequest) -> dict[str, Any]:
        """Create a new job."""
        try:
            sid = uuid.UUID(session_id)
        except ValueError:
            raise HTTPException(400, "invalid session ID") from None
        session = await session_manager.get(sid)
        if session is None:
            raise HTTPException(404, "session not found")
        from ah.core.scheduler import DEFAULT_HEARTBEAT_PROMPT, job_store

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

    @app.get("/api/v1/sessions/{session_id}/jobs", dependencies=[Depends(require_api_key)])
    @app.get("/sessions/{session_id}/jobs", dependencies=[Depends(require_api_key)])
    async def list_jobs(session_id: str, limit: int = Query(100, ge=1, le=500)) -> dict[str, Any]:
        """List jobs for a session."""
        try:
            sid = uuid.UUID(session_id)
        except ValueError:
            raise HTTPException(400, "invalid session ID") from None
        session = await session_manager.get(sid)
        if session is None:
            raise HTTPException(404, "session not found")
        from ah.core.scheduler import job_store

        jobs = await job_store.list(session_id=sid, limit=limit)
        return {"jobs": [j.to_dict() for j in jobs]}

    # ── agents ──────────────────────────────────────────────────────────────

    @app.get("/agents", dependencies=[Depends(require_api_key)])
    async def list_agents() -> dict[str, Any]:
        """List all agents."""
        from ah.core.agent_def import agent_registry

        agents = await agent_registry.list()
        return {"agents": [a.to_dict() for a in agents]}

    # ── rpc ─────────────────────────────────────────────────────────────────

    @app.post("/rpc", dependencies=[Depends(require_api_key)])
    async def rpc_dispatch(req: RpcRequest) -> dict[str, Any]:
        """Generic JSON-RPC dispatch to any gateway feature method."""
        if req.method in {"prompt.submit", "prompt.cancel", "shutdown"}:
            raise HTTPException(400, "use the session prompt stream for turns")
        gw = getattr(app.state, "_rpc_gateway", None)
        if gw is None:
            raise HTTPException(503, "RPC gateway not initialized")
        try:
            result = await gw.call(req.method, req.params)
            return {"result": result}
        except RpcError as e:
            if e.code in {-32601, -32602}:
                raise HTTPException(400, e.message) from None
            raise HTTPException(500, "RPC dispatch failed") from None
        except Exception:
            logger.exception("RPC dispatch failed for method %s", req.method)
            raise HTTPException(500, "internal error") from None

    return app


# ─── helpers ───────────────────────────────────────────────────────────────


def _serialize_event(event: StreamEvent) -> dict[str, Any]:
    """Convert a StreamEvent to a JSON-serializable dict."""
    data: dict[str, Any] = {"type": event.type}
    if event.type == "text":
        data["content"] = event.content
    elif event.type == "tool_call":
        data["tool"] = event.tool_name
        data["args"] = event.tool_args
    elif event.type == "tool_result":
        data["tool"] = event.tool_name
        data["result"] = event.tool_result
    elif event.type == "token_usage":
        data["tokens"] = event.tokens_used
    elif event.type == "done" and event.response is not None:
        resp = event.response
        data["content"] = resp.content
        data["tokens"] = resp.tokens_used
        data["iterations"] = resp.iterations
        data["toolCalls"] = len(resp.tool_calls)
    return data
