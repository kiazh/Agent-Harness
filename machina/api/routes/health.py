"""Health and observability endpoints (moved verbatim from app.py; assembled by create_app)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse

from machina import __version__
from machina.api.auth import require_api_key
from machina.db import connection as _connection

router = APIRouter()


# legacy alias — preserved per the removal plan in routes/__init__.py; do not remove.
@router.get("/health")
async def health() -> dict[str, str]:
    """Return service health status and version."""
    return {"status": "ok", "version": __version__}


# legacy alias — preserved per the removal plan in routes/__init__.py; do not remove.
@router.get("/ready")
async def ready() -> dict[str, str]:
    """Check if the database is connected and schema is initialized."""
    if not _connection.db.connected:
        raise HTTPException(503, "not ready")
    try:
        schema_ready = await _connection.db.fetchval(
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


# legacy alias — preserved per the removal plan in routes/__init__.py; do not remove.
@router.get("/metrics", dependencies=[Depends(require_api_key)])
async def prometheus_metrics() -> PlainTextResponse:
    """Return Prometheus-formatted metrics including LLM usage totals."""
    from machina.core.usage import usage_store
    from machina.observability.metrics import prometheus_text

    body = prometheus_text()
    if _connection.db.connected:
        totals = await usage_store.totals()
        body += (
            "# HELP machina_llm_usage_requests_total Durable LLM call attempts.\n"
            "# TYPE machina_llm_usage_requests_total counter\n"
            f"machina_llm_usage_requests_total {totals['requests']}\n"
            "# HELP machina_llm_usage_accounted_tokens_total Known tokens or conservative reservations.\n"
            "# TYPE machina_llm_usage_accounted_tokens_total counter\n"
            f"machina_llm_usage_accounted_tokens_total {totals['accounted_tokens']}\n"
            "# HELP machina_llm_usage_unknown_calls Calls without provider token usage.\n"
            "# TYPE machina_llm_usage_unknown_calls gauge\n"
            f"machina_llm_usage_unknown_calls {totals['unknown_calls']}\n"
        )
    return PlainTextResponse(body, media_type="text/plain; version=0.0.4")
