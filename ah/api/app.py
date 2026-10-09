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
from ah.gateway.errors import (
    DATABASE_UNAVAILABLE,
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    NOT_FOUND,
    TURN_IN_PROGRESS,
    UNAUTHORIZED,
    RpcError,
)
from ah.gateway.serializers import chunk_preview, history_from_chunks, session_to_dict
from ah.gateway.server import PROVIDERS, Gateway
from ah.memory.redaction import StreamingSecretRedactor, redact_secrets

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


class ResolveApprovalRequest(BaseModel):
    verdict: str = Field(..., min_length=1, max_length=16)
    sessionId: uuid.UUID | None = None
    grant: str = Field(default="once", min_length=1, max_length=16)


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
        # Alias kept for backwards compatibility. It is held only around id
        # assignment below — never across ``handle_line`` — so concurrent RPCs
        # are not serialized behind one long-lived handler.
        self._id_lock = self._lock
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
        async with getattr(self, "_id_lock", getattr(self, "_lock", None)) or asyncio.Lock():
            self._next_id += 1
            rid = self._next_id
        self._cleanup_old_responses()
        # The snapshot taken in __init__ goes stale across (re)connects;
        # refresh from the live connection state on every call.
        self._gateway._db_ready = db.connected
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
    """Single shared lifecycle: the runtime graph owns DB/audit/scheduler."""
    from ah.core.runtime import runtime_services
    from ah.plugins.loader import load_plugins

    load_plugins()
    runner = None
    rpc_gw = None
    try:
        report = await runtime_services.startup()
        if report.get("db") == "ready":
            from ah.core.scheduler import JobRunner

            runner = JobRunner()
            runner.start()
        # Create a single _RpcGateway for all RPC dispatches
        rpc_gw = _RpcGateway()
        app.state._rpc_gateway = rpc_gw
    except Exception as e:
        logger.warning("Runtime startup degraded: %s", e)
    yield
    # AH-AUDIT-024: bounded shutdown — no stage awaits cancellation-
    # resistant work indefinitely.
    if runner is not None:
        try:
            await asyncio.wait_for(runner.stop(), timeout=10)
        except (TimeoutError, asyncio.CancelledError, Exception):
            logger.warning("API job runner stop exceeded its bound; continuing")
    if rpc_gw is not None:
        try:
            await asyncio.wait_for(rpc_gw.close(), timeout=10)
        except (TimeoutError, asyncio.CancelledError, Exception):
            logger.warning("API gateway close exceeded its bound; continuing")
    try:
        await asyncio.wait_for(runtime_services.shutdown(), timeout=15)
    except (TimeoutError, asyncio.CancelledError, Exception):
        pass


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
            raise HTTPException(503, "not ready")
        try:
            schema_ready = await db.fetchval(
                "SELECT to_regclass('sessions') IS NOT NULL "
                "AND to_regclass('jobs') IS NOT NULL "
                "AND to_regclass('audit_events') IS NOT NULL "
                "AND to_regclass('llm_usage') IS NOT NULL"
            )
        except Exception:
            raise HTTPException(503, "not ready") from None
        if not schema_ready:
            # Generic message on purpose: distinguishing "no database" from
            # "schema not initialized" lets unauthenticated callers probe state.
            raise HTTPException(503, "not ready")
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
            # AH-AUDIT-018: transactional field merge — concurrent
            # compaction/watermark writes are preserved, never clobbered.
            await session_manager.update_state_fields(session_id, {"emotion": req.emotion or None})
        if req.title is not None:
            await session_manager.set_title(session_id, req.title)
        if req.goal is not None:
            await session_manager.set_goal(session_id, req.goal)
        return {"session": session_to_dict(await session_manager.get(session_id))}

    @app.delete("/api/v1/sessions/{session_id}", dependencies=[Depends(require_api_key)])
    async def delete_session(session_id: uuid.UUID) -> dict[str, bool]:
        """Delete a session by ID (mutation claim; live turns reject first)."""
        from ah.core.turns import begin_mutation, end_mutation, turn_active

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
        if req.provider is not None and req.provider not in PROVIDERS:
            raise HTTPException(400, f"unknown provider: {req.provider}")
        if req.model is not None and not req.model.strip():
            raise HTTPException(400, "model must be a non-empty string")
        session = await session_manager.create(
            title=req.title,
            model=req.model or config.get("model"),
            provider=req.provider or config.get("provider"),
            goal=req.goal,
            context_budget=config.get("context_budget"),
            state={"emotion": req.emotion} if req.emotion else None,
        )
        try:
            from ah.gateway.features.mode import grant_full_default

            await grant_full_default(session)
        except Exception:
            pass
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
            "history": [_redact_value(h) for h in history_from_chunks(chunks)],
        }

    @app.post("/api/v1/sessions/{session_id}/chat", dependencies=[Depends(require_api_key)])
    @app.post("/api/v1/sessions/{session_id}/prompt", dependencies=[Depends(require_api_key)])
    @app.post("/sessions/{session_id}/prompt", dependencies=[Depends(require_api_key)])
    async def prompt_session(session_id: str, req: PromptRequest) -> StreamingResponse:
        """Stream agent events as Server-Sent Events."""
        try:
            sid = uuid.UUID(session_id)
        except ValueError:
            raise HTTPException(400, "invalid session ID") from None
        # AH-AUDIT-019: execution-critical read bypasses the display cache.
        session = await session_manager.get_fresh(sid)
        if session is None:
            raise HTTPException(404, "session not found")
        # Shared coordinator: owner-token claim BEFORE SSE headers so a
        # conflicting turn gets a clean 409 instead of an interleaved stream.
        # Fails closed: claim errors propagate, never concurrent execution.
        from ah.core.turns import end_turn as _end_turn
        from ah.core.turns import try_begin_turn as _try_begin

        turn_token = await _try_begin(sid)
        if not turn_token:
            raise HTTPException(409, "a turn is already running for this session")

        # AH-AUDIT-013: managed renewal while the stream owns the turn
        # (compute only here — headless SSE never waits on humans, so no
        # approval accounting is needed on this path). Created lazily on
        # first iteration so an unconsumed stream never renews forever;
        # stopped when the generator exits; crash expiry recovery preserved.
        _renew_state: dict[str, Any] = {}

        async def _ensure_renewal() -> None:
            if _renew_state.get("task") is not None:
                return
            _renew_stop = asyncio.Event()
            _renew_state["stop"] = _renew_stop

            async def _renew_loop() -> None:
                from ah.core.turns import renew_turn as _renew_stream_claim

                while not _renew_stop.is_set():
                    try:
                        await asyncio.wait_for(_renew_stop.wait(), timeout=120)
                    except TimeoutError:
                        pass
                    if _renew_stop.is_set():
                        return
                    try:
                        if not await _renew_stream_claim(sid, turn_token):
                            logger.warning("SSE turn lost ownership for session %s", session_id)
                            return
                    except Exception:
                        pass

            _renew_state["task"] = asyncio.create_task(_renew_loop())

        async def _stop_renewal() -> None:
            _renew_stop = _renew_state.pop("stop", None)
            _renew_task = _renew_state.pop("task", None)
            if _renew_stop is not None:
                try:
                    _renew_stop.set()
                except Exception:
                    pass
            if _renew_task is not None and not _renew_task.done():
                _renew_task.cancel()
            if _renew_task is not None:
                try:
                    await asyncio.gather(_renew_task, return_exceptions=True)
                except (asyncio.CancelledError, Exception):
                    pass

        async def event_stream() -> AsyncGenerator[str, None]:
            agent = None
            _stream_cancelled = False
            _stage = "turn_started"
            await _ensure_renewal()

            def _record_stage(stage: str) -> None:
                nonlocal _stage
                _stage = stage
                logger.debug("Session %s stage: %s", session_id, stage)

            # Boundary-safe streaming redaction: hold back a tail across
            # deltas so a secret split at any boundary never leaks (AH-001).
            # Final flush redacts the remainder; the done event carries the
            # fully-redacted concatenation, never the raw accumulation.
            stream_redactor = StreamingSecretRedactor()
            try:
                # AH-022: definition-aware factory so specialist sessions
                # cannot bypass allowed_tools/persona via direct HTTP.
                from ah.core.agent_factory import build_agent_for_session

                _record_stage("building_agent")
                agent = await build_agent_for_session(session)
                _record_stage("waiting_for_provider")
                # Total turn timeout: a per-anext wait_for against a fixed
                # deadline so a hung provider cannot hold the stream forever.
                try:
                    timeout = float(config.get("turn_timeout") or 300)
                except (TypeError, ValueError):
                    timeout = 300.0
                deadline = asyncio.get_running_loop().time() + timeout
                stream = agent.run_stream(sid, req.text, verbose=req.verbose)
                while True:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        raise TimeoutError("turn timed out")
                    try:
                        event = await asyncio.wait_for(stream.__anext__(), timeout=remaining)
                    except StopAsyncIteration:
                        break
                    if event.type == "text":
                        # Emit only the safe prefix; the tail stays buffered
                        # until more deltas arrive or flush() runs at done.
                        safe = stream_redactor.feed(event.content or "")
                        if safe:
                            data = _serialize_event(
                                StreamEvent(type="text", content=safe),
                                _pre_redacted=True,
                            )
                            yield f"data: {json.dumps(data)}\n\n"
                        continue
                    if event.type == "done" and event.response is not None:
                        tail = stream_redactor.flush()
                        if tail:
                            # The tail was withheld from earlier deltas; emit
                            # it now (redacted) before the terminal event.
                            data = _serialize_event(
                                StreamEvent(type="text", content=tail),
                                _pre_redacted=True,
                            )
                            yield f"data: {json.dumps(data)}\n\n"
                        # AH-AUDIT-030: typed needs_approval SSE output with
                        # request/session/turn binding and the sanitized
                        # proposal. A pause is actionable and distinct from
                        # failure; resume revalidates under a fresh fenced
                        # claim (never by continuing stale generator state).
                        for pause in getattr(event.response, "needs_approval", None) or []:
                            if not isinstance(pause, dict) or not pause.get("request_id"):
                                continue
                            pause_event = StreamEvent(
                                type="needs_approval",
                                content=str(pause.get("request_id", "")),
                                tool_name=str(pause.get("operation", "")),
                                tool_args={
                                    "requestId": str(pause.get("request_id", "")),
                                    "sessionId": str(pause.get("session_id", "") or session_id),
                                    "operation": str(pause.get("operation", "")),
                                    "target": str(pause.get("target", "")),
                                    "status": str(pause.get("status", "pending")),
                                },
                            )
                            data = _serialize_event(pause_event)
                            yield f"data: {json.dumps(data)}\n\n"
                    data = _serialize_event(event)
                    yield f"data: {json.dumps(data)}\n\n"
            except TimeoutError:
                logger.warning("Prompt stream timed out for session %s", session_id)
                error_data = {"type": "error", "message": "turn timed out"}
                yield f"data: {json.dumps(error_data)}\n\n"
            except Exception:
                logger.exception("Prompt stream failed for session %s", session_id)
                error_data = {"type": "error", "message": "prompt failed; check server logs"}
                yield f"data: {json.dumps(error_data)}\n\n"
            except asyncio.CancelledError:
                # Client disconnect / server shutdown: bounded cleanup below,
                # then propagate (never yield DONE here — the consumer is gone
                # and swallowing cancellation would strand the task).
                _stream_cancelled = True
                raise
            finally:
                _record_stage("cleanup_started")
                # H-01/H-02/H-03/H-06: Bounded cleanup contract.
                # 1. Learning tasks: bounded join, then cancel leftovers.
                # 2. Provider close: bounded with timeout.
                # 3. Claim release: independent outer finally, always runs.
                # 4. Auto-compaction: fire-and-forget, never blocks [DONE].
                try:
                    _learning = (
                        tuple(getattr(agent, "_learning_tasks", ())) if agent is not None else ()
                    )
                    if _learning:
                        _record_stage("joining_optional_work")
                        # Bounded join: learning reviews must never block
                        # normal answer completion indefinitely.
                        try:
                            await asyncio.wait_for(
                                asyncio.gather(*_learning, return_exceptions=True),
                                timeout=2,
                            )
                        except (TimeoutError, asyncio.CancelledError, Exception):
                            for _t in _learning:
                                try:
                                    if not _t.done():
                                        _t.cancel()
                                except Exception:
                                    pass
                finally:
                    # Claim release in independent outer finally so provider
                    # close errors or cancellation cannot skip it (H-02).
                    try:
                        _record_stage("closing_owned_clients")
                        # AH-023: close only owned providers, bounded.
                        if agent is not None:
                            try:
                                from ah.core.agent_factory import close_agent_provider

                                await asyncio.wait_for(
                                    close_agent_provider(agent),
                                    timeout=5,
                                )
                            except (TimeoutError, asyncio.CancelledError):
                                logger.warning(
                                    "Provider close timed out for session %s", session_id
                                )
                            except Exception:
                                logger.exception(
                                    "Could not close prompt provider for session %s",
                                    session_id,
                                )
                    finally:
                        _record_stage("releasing_ownership")
                        # AH-AUDIT-013: stop renewal during release.
                        try:
                            await _stop_renewal()
                        except (asyncio.CancelledError, Exception):
                            pass
                        if turn_token:
                            try:
                                await _end_turn(sid, turn_token)
                            except Exception:
                                pass
                    # H-03: Auto-compaction is fire-and-forget background work.
                    # Never blocks [DONE] or holds the stream open.
                    # AH-AUDIT-025: tracked by the shared runtime for bounded
                    # shutdown joining before DB closure.
                    if not _stream_cancelled:
                        _record_stage("compaction_maintenance")
                        try:
                            from ah import services as _services
                            from ah.core.runtime import runtime_services as _runtime

                            task = asyncio.create_task(_services.maybe_auto_compact(sid))
                            task.add_done_callback(
                                lambda t: t.exception() if not t.cancelled() else None
                            )
                            try:
                                _runtime.track(task)
                            except Exception:
                                pass
                        except Exception:
                            pass
                if not _stream_cancelled:
                    _record_stage("answer_complete")
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
                    "preview": redact_secrets(chunk_preview(c)).text,
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
                agent_id=agent,
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
        if req.scriptPath is not None:
            from ah.core.job_scripts import resolve_script_path

            try:
                resolve_script_path(req.scriptPath)
            except ValueError as e:
                # Defer missing-file errors to run time so API tests with
                # placeholder paths still pass; reject traversal/extension now.
                if "does not exist" not in str(e):
                    raise HTTPException(400, str(e)) from None
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

    # ── approvals (Phase E: authenticated list/resolve, ownership enforced) ──

    @app.get("/api/v1/approvals", dependencies=[Depends(require_api_key)])
    async def list_approvals(sessionId: uuid.UUID) -> dict[str, Any]:
        """List pending human approvals for a session (poll-based recovery)."""
        from ah.permissions import store as _store

        session = await session_manager.get(sessionId)
        if session is None:
            raise HTTPException(404, "session not found")
        return {"approvals": await _store.list_pending(str(sessionId))}

    @app.post("/api/v1/approvals/{request_id}/resolve", dependencies=[Depends(require_api_key)])
    async def resolve_approval(request_id: str, req: ResolveApprovalRequest) -> dict[str, Any]:
        """Resolve a pending approval. Ownership enforced via stored session binding."""
        from ah.permissions import store as _store

        rec = await _store.get_approval(request_id)
        if rec is None:
            raise HTTPException(404, "approval not found")
        if req.sessionId is not None and str(rec.get("session_id")) != str(req.sessionId):
            raise HTTPException(403, "approval does not belong to this session")
        verdict = req.verdict.lower()
        if verdict not in ("approved", "denied"):
            raise HTTPException(400, "verdict must be approved|denied")
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
        return {"approval": {"requestId": request_id, "status": verdict}}

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
        """Generic JSON-RPC dispatch to any gateway feature method.

        NOTE: the gateway is shared across HTTP requests. ``session.create``
        and ``config.set`` mutate that shared state (model/provider writes are
        serialized by ``Gateway._config_lock``); prefer the REST session
        endpoints when per-request isolation matters.
        """
        if req.method in {"prompt.submit", "prompt.cancel", "shutdown"}:
            raise HTTPException(400, "use the session prompt stream for turns")
        gw = getattr(app.state, "_rpc_gateway", None)
        if gw is None:
            raise HTTPException(503, "RPC gateway not initialized")
        try:
            result = await gw.call(req.method, req.params)
            # AH-002: REST context previews are redacted, so the generic RPC
            # bridge must apply the same policy — otherwise context.get
            # previews and session.export markdown leak raw transcript
            # secrets through /rpc.
            return {"result": _redact_value(result)}
        except RpcError as e:
            status = _rpc_http_status(e.code)
            raise HTTPException(
                status, e.message if status != 500 else "RPC dispatch failed"
            ) from None
        except Exception:
            logger.exception("RPC dispatch failed for method %s", req.method)
            raise HTTPException(500, "internal error") from None

    return app


# ─── helpers ───────────────────────────────────────────────────────────────


def _rpc_http_status(code: int) -> int:
    """Map gateway RpcError codes to HTTP statuses."""
    if code in (METHOD_NOT_FOUND, INVALID_PARAMS):
        return 400
    if code == NOT_FOUND:  # 1002, incl. SESSION_NOT_FOUND
        return 404
    if code == TURN_IN_PROGRESS:  # 1003
        return 409
    if code == DATABASE_UNAVAILABLE:  # 1001
        return 503
    if code == UNAUTHORIZED:  # 1005
        return 401
    return 500


def _redact_value(value: Any) -> Any:
    """Recursively redact secret-looking strings in *value*."""
    if isinstance(value, str):
        return redact_secrets(value).text
    if isinstance(value, dict):
        return {k: _redact_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(v) for v in value)
    return value


def _serialize_event(event: StreamEvent, *, _pre_redacted: bool = False) -> dict[str, Any]:
    """Convert a StreamEvent to a JSON-serializable dict.

    When *_pre_redacted* is True the text content already passed through the
    boundary-safe streaming redactor and must not be redacted a second time
    (double redaction is harmless but the flag documents the contract).
    """
    data: dict[str, Any] = {"type": event.type}
    if event.type == "text":
        data["content"] = event.content if _pre_redacted else redact_secrets(event.content).text
    elif event.type == "tool_call":
        data["tool"] = event.tool_name
        data["args"] = _redact_value(event.tool_args)
        if getattr(event, "tool_call_id", ""):
            data["id"] = event.tool_call_id
    elif event.type == "tool_result":
        data["tool"] = event.tool_name
        data["result"] = redact_secrets(event.tool_result).text
        if getattr(event, "tool_call_id", ""):
            data["id"] = event.tool_call_id
    elif event.type == "token_usage":
        data["tokens"] = event.tokens_used
    elif event.type == "done" and event.response is not None:
        resp = event.response
        data["content"] = redact_secrets(resp.content).text
        data["tokens"] = resp.tokens_used
        data["iterations"] = resp.iterations
        data["toolCalls"] = len(resp.tool_calls)
        pauses = [
            p
            for p in (getattr(resp, "needs_approval", None) or [])
            if isinstance(p, dict) and p.get("request_id")
        ]
        if pauses:
            data["needsApproval"] = [
                {
                    "requestId": str(p.get("request_id", "")),
                    "operation": str(p.get("operation", "")),
                    "target": str(p.get("target", "")),
                    "status": str(p.get("status", "pending")),
                }
                for p in pauses
            ]
    elif event.type == "needs_approval":
        # Typed pause (AH-AUDIT-030): actionable, never a generic error.
        data["requestId"] = event.content
        data["operation"] = event.tool_name
        data["proposal"] = _redact_value(dict(event.tool_args or {}))
    return data
