"""Helpers shared by the API route modules (moved verbatim from app.py).

Kept in one place so every router redacts, serializes, and maps RPC errors
identically. ``machina.api.app`` re-exports ``_redact_value`` and
``_serialize_event`` for backwards compatibility.
"""

from __future__ import annotations

from typing import Any

from machina.core.models import StreamEvent
from machina.gateway.errors import (
    DATABASE_UNAVAILABLE,
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    NOT_FOUND,
    TURN_IN_PROGRESS,
    UNAUTHORIZED,
)
from machina.memory.redaction import redact_secrets


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
    if isinstance(value, set):
        return {_redact_value(v) for v in value}
    if isinstance(value, bytes):
        try:
            return redact_secrets(value.decode("utf-8")).text.encode("utf-8")
        except UnicodeDecodeError:
            return value
    return value


def _serialize_event(
    event: StreamEvent,
    *,
    _pre_redacted: bool = False,
    protocol_version: int = 1,
    session_id: str = "",
    turn_id: str = "",
) -> dict[str, Any]:
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
        if event.tool_outcome:
            data["outcome"] = event.tool_outcome
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
            from machina.protocol import approval_summary

            data["needsApproval"] = [approval_summary(p) for p in pauses]
    elif event.type == "needs_approval":
        # Typed pause (AH-AUDIT-030): actionable, never a generic error.
        data["requestId"] = event.content
        data["operation"] = event.tool_name
        data["proposal"] = _redact_value(dict(event.tool_args or {}))
    if protocol_version == 2:
        from machina.protocol import canonical_http_event

        return canonical_http_event(data, session_id, turn_id)
    if protocol_version != 1:
        raise ValueError("unsupported protocol version")
    return data
