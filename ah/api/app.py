"""FastAPI application factory for the AgentHarness HTTP API.

Exposes REST endpoints that reuse the same core services (session_manager,
context_manager, memory_store, job_store, agent_registry) and the Gateway
class that the terminal UI drives via JSON-RPC.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ah import __version__
from ah.api.auth import require_api_key
from ah.core.config import config
from ah.core.context import context_manager
from ah.core.models import StreamEvent
from ah.core.session import session_manager
from ah.db.connection import db
from ah.gateway.errors import RpcError
from ah.gateway.server import Gateway, history_from_chunks, session_to_dict

logger = logging.getLogger(__name__)


# ─── request / response models ──────────────────────────────────────────────


class CreateSessionRequest(BaseModel):
    title: str | None = None
    model: str | None = None
    provider: str | None = None
    goal: str | None = None


class CreateJobRequest(BaseModel):
    name: str | None = None
    kind: str = "interval"
    prompt: str | None = None
    intervalSeconds: int = 300
    agent: str = "harness"


class PromptRequest(BaseModel):
    text: str = Field(..., min_length=1)
    verbose: bool = False


class RpcRequest(BaseModel):
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


class RpcResponse(BaseModel):
    result: Any = None
    error: dict[str, Any] | None = None


# ─── gateway helper ────────────────────────────────────────────────────────


class _RpcGateway:
    """Minimal Gateway wrapper that captures the JSON-RPC response.

    The Gateway class expects a ``write`` callable that receives response
    frames. We capture the last frame with a matching ``id`` so the HTTP
    layer can return it to the client.
    """

    def __init__(self) -> None:
        self._responses: dict[int, dict[str, Any]] = {}
        self._next_id = 0
        self._gateway = Gateway(self._capture, owns_db=False)
        self._gateway._db_ready = db.connected

    def _capture(self, frame: dict[str, Any]) -> None:
        rid = frame.get("id")
        if rid is not None:
            self._responses[rid] = frame

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        rid = self._next_id
        frame = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        await self._gateway.handle_line(json.dumps(frame))
        response = self._responses.pop(rid, None)
        if response is None:
            raise RpcError(-32603, "no response from gateway")
        if "error" in response:
            err = response["error"]
            raise RpcError(err.get("code", -32603), err.get("message", "unknown error"))
        return response.get("result", {})

    async def close(self) -> None:
        await self._gateway.close()


# ─── lifespan ──────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Connect to the database on startup and close on shutdown."""
    try:
        await db.connect()
    except Exception as e:
        logger.warning("Database not available: %s", e)
    yield
    await db.close()


# ─── app factory ───────────────────────────────────────────────────────────


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="AgentHarness API",
        version=__version__,
        lifespan=lifespan,
    )

    # ── health ──────────────────────────────────────────────────────────────

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    # ── sessions ────────────────────────────────────────────────────────────

    @app.post("/sessions", dependencies=[Depends(require_api_key)])
    async def create_session(req: CreateSessionRequest) -> dict[str, Any]:
        session = await session_manager.create(
            title=req.title,
            model=req.model or config.get("model"),
            provider=req.provider or config.get("provider"),
            goal=req.goal,
            context_budget=config.get("context_budget"),
        )
        return {"session": session_to_dict(session)}

    @app.get("/sessions", dependencies=[Depends(require_api_key)])
    async def list_sessions(limit: int = 20) -> dict[str, Any]:
        sessions = await session_manager.list_sessions(limit=limit)
        return {"sessions": [session_to_dict(s) for s in sessions]}

    @app.get("/sessions/{session_id}", dependencies=[Depends(require_api_key)])
    async def get_session(session_id: str) -> dict[str, Any]:
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

            try:
                llm = get_provider(
                    provider=session.provider or config.get("provider"),
                    model=session.model or config.get("model"),
                )
                agent = ReActAgent(
                    provider=llm,
                    max_iterations=config.get("max_iterations"),
                    agent_id=config.get("agent_id"),
                )
                async for event in agent.run_stream(sid, req.text, verbose=req.verbose):
                    data = _serialize_event(event)
                    yield f"data: {json.dumps(data)}\n\n"
            except Exception as e:
                logger.exception("Prompt stream failed for session %s", session_id)
                error_data = {"type": "error", "message": str(e)}
                yield f"data: {json.dumps(error_data)}\n\n"
            finally:
                yield "data: [DONE]\n\n"

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )

    @app.get("/sessions/{session_id}/context", dependencies=[Depends(require_api_key)])
    async def get_context(session_id: str, limit: int = 50) -> dict[str, Any]:
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
                    "preview": _chunk_preview(c),
                }
                for c in chunks
            ],
            "totalTokens": await context_manager.get_token_usage(sid),
            "budget": session.context_budget,
            "goal": session.goal,
        }

    @app.get("/sessions/{session_id}/memory", dependencies=[Depends(require_api_key)])
    async def get_memory(session_id: str, limit: int = 20) -> dict[str, Any]:
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

    @app.post("/sessions/{session_id}/jobs", dependencies=[Depends(require_api_key)])
    async def create_job(session_id: str, req: CreateJobRequest) -> dict[str, Any]:
        try:
            sid = uuid.UUID(session_id)
        except ValueError:
            raise HTTPException(400, "invalid session ID") from None
        session = await session_manager.get(sid)
        if session is None:
            raise HTTPException(404, "session not found")
        from ah.core.scheduler import DEFAULT_HEARTBEAT_PROMPT, job_store

        kind = req.kind
        if kind not in ("heartbeat", "interval"):
            raise HTTPException(400, "kind must be 'heartbeat' or 'interval'")
        prompt = req.prompt or DEFAULT_HEARTBEAT_PROMPT
        try:
            job = await job_store.create(
                name=req.name or f"{kind} job",
                kind=kind,
                session_id=sid,
                prompt=prompt,
                interval_seconds=req.intervalSeconds,
                agent_name=req.agent,
            )
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        return {"job": job.to_dict()}

    @app.get("/sessions/{session_id}/jobs", dependencies=[Depends(require_api_key)])
    async def list_jobs(session_id: str, limit: int = 100) -> dict[str, Any]:
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
        from ah.core.agent_def import agent_registry

        agents = await agent_registry.list()
        return {"agents": [a.to_dict() for a in agents]}

    # ── rpc ─────────────────────────────────────────────────────────────────

    @app.post("/rpc", dependencies=[Depends(require_api_key)])
    async def rpc_dispatch(req: RpcRequest) -> dict[str, Any]:
        """Generic JSON-RPC dispatch to any gateway feature method."""
        if req.method in {"prompt.submit", "prompt.cancel", "shutdown"}:
            raise HTTPException(400, "use the session prompt stream for turns")
        gw = _RpcGateway()
        try:
            result = await gw.call(req.method, req.params)
            return {"result": result}
        except RpcError as e:
            raise HTTPException(400, f"RPC error {e.code}: {e.message}") from None
        except Exception as e:
            logger.exception("RPC dispatch failed for method %s", req.method)
            raise HTTPException(500, f"internal error: {e}") from None
        finally:
            await gw.close()

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


def _chunk_preview(chunk: Any) -> str:
    """Extract a short preview from a context chunk."""
    payload = chunk.payload
    for key in ("content", "text", "result_preview"):
        if payload.get(key):
            return str(payload[key])[:200]
    if payload.get("tool"):
        return f"{payload['tool']}({payload.get('args', {})})"[:200]
    return str(payload)[:200]
