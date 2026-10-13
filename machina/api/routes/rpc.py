"""Generic JSON-RPC dispatch endpoint (moved verbatim from app.py).

The only change from the original is mechanical: the handler reads the
shared ``_RpcGateway`` from ``request.app.state`` instead of the ``create_app``
closure, so the route can live on an APIRouter. Path, auth, and shapes are
unchanged.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from machina.api.auth import require_api_key
from machina.api.routes.models import RpcRequest
from machina.api.routes.shared import _redact_value, _rpc_http_status
from machina.gateway.errors import RpcError

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/rpc", dependencies=[Depends(require_api_key)])
async def rpc_dispatch(req: RpcRequest, request: Request) -> dict[str, Any]:
    """Generic JSON-RPC dispatch to any gateway feature method.

    NOTE: the gateway is shared across HTTP requests. ``session.create``
    and ``config.set`` mutate that shared state (model/provider writes are
    serialized by ``Gateway._config_lock``); prefer the REST session
    endpoints when per-request isolation matters.
    """
    if req.method in {"prompt.submit", "prompt.cancel", "shutdown"}:
        raise HTTPException(400, "use the session prompt stream for turns")
    gw = getattr(request.app.state, "_rpc_gateway", None)
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
        raise HTTPException(status, e.message if status != 500 else "RPC dispatch failed") from None
    except Exception:
        logger.exception("RPC dispatch failed for method %s", req.method)
        raise HTTPException(500, "internal error") from None
