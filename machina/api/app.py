"""FastAPI application factory for the Machina HTTP API.

Exposes REST endpoints that reuse the same core services (session_manager,
context_manager, memory_store, job_store, agent_registry) and the Gateway
class that the terminal UI drives via JSON-RPC.

Structure (S4.1): routes live in per-resource APIRouter modules under
``machina.api.routes`` (sessions, memory, jobs, documents, approvals,
agents, rpc, health, plus shared models/helpers/streaming). ``create_app``
below only assembles them onto one ``FastAPI`` instance — lifespan and
middleware are preserved here.

Backwards-compatibility surface: several names are re-exported (or kept as
module attributes) because tests and callers address them as
``machina.api.app.<name>`` — ``db`` / ``Gateway`` / ``session_manager`` /
``context_manager`` (patch targets), ``StreamEvent``, ``PromptRequest`` and
the other request models, ``_serialize_event`` / ``_redact_value``,
``CLAIM_RENEW_INTERVAL_SECONDS``, ``_RpcGateway``, and ``lifespan``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from machina import __version__
from machina.api.rate_limit import RateLimitMiddleware
from machina.api.routes import agents, approvals, documents, health, jobs, memory, rpc, sessions
from machina.api.routes.models import (
    CreateDocumentRequest,
    CreateJobRequest,
    CreateMemoryRequest,
    CreatePersonaMemoryRequest,
    CreateSessionRequest,
    PromptRequest,
    ResolveApprovalRequest,
    ResumeApprovalRequest,
    RpcRequest,
    UpdateSessionRequest,
)
from machina.api.routes.shared import _redact_value, _serialize_event
from machina.core.context import context_manager  # noqa: F401 — re-exported (patch target)
from machina.core.models import StreamEvent  # noqa: F401 — re-exported (test attribute)
from machina.core.session import session_manager  # noqa: F401 — re-exported (patch target)
from machina.db.connection import db
from machina.gateway.errors import RpcError
from machina.gateway.server import Gateway
from machina.observability.diagnostics import record_failure

logger = logging.getLogger(__name__)
CLAIM_RENEW_INTERVAL_SECONDS = 30.0

__all__ = [
    "CLAIM_RENEW_INTERVAL_SECONDS",
    "CreateDocumentRequest",
    "CreateJobRequest",
    "CreateMemoryRequest",
    "CreatePersonaMemoryRequest",
    "CreateSessionRequest",
    "PromptRequest",
    "ResolveApprovalRequest",
    "ResumeApprovalRequest",
    "RpcRequest",
    "StreamEvent",
    "UpdateSessionRequest",
    "_RpcGateway",
    "_redact_value",
    "_serialize_event",
    "context_manager",
    "create_app",
    "db",
    "lifespan",
    "session_manager",
]


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
        # W8: explicit embedded-gateway signal — never inherit the stdio
        # MACHINA_GATEWAY_TOKEN. The HTTP boundary already enforces API-key
        # auth; inheriting the token would 401 every /api/v1/rpc call.
        self._gateway = Gateway(self._capture, owns_db=False, inherit_env_token=False)
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
    from machina.core.runtime import runtime_services
    from machina.plugins.loader import load_plugins

    load_plugins()
    runner = None
    rpc_gw = None
    try:
        report = await runtime_services.startup()
        if report.get("db") == "ready":
            from machina.core.scheduler import JobRunner

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
    except (TimeoutError, asyncio.CancelledError, Exception) as _boundary_error:
        # Optional fallback preserves the primary outcome; report no payload.
        record_failure("app.lifespan", _boundary_error)


# ─── app factory ───────────────────────────────────────────────────────────


def create_app() -> FastAPI:
    """Create and configure the FastAPI application (thin assembly)."""
    app = FastAPI(
        title="Machina API",
        version=__version__,
        lifespan=lifespan,
    )
    app.add_middleware(RateLimitMiddleware)
    # Mount each resource router's routes directly (instead of
    # ``include_router``): the installed FastAPI resolves included routers
    # lazily, leaving placeholder objects in ``app.routes``. The test-suite
    # and operators introspect ``app.routes`` for real ``APIRoute`` entries
    # (path/methods/endpoint), so mount the already-built routes verbatim.
    # Registration order groups by resource; no two same-method routes
    # overlap, so matching is identical to the pre-split inline order.
    for resource in (health, sessions, memory, jobs, documents, approvals, agents, rpc):
        app.router.routes.extend(resource.router.routes)
    _mark_changed = getattr(app.router, "_mark_routes_changed", None)
    if callable(_mark_changed):
        _mark_changed()
    return app
