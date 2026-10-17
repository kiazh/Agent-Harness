"""Shared presentation helpers for the gateway and HTTP API."""

from __future__ import annotations

import base64
from typing import Any

from machina.core.models import ContextChunk, Session


def json_safe_payload(value: Any) -> Any:
    """Preserve MessagePack binary values in JSON-RPC responses."""
    if isinstance(value, bytes):
        return {"$base64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, dict):
        return {str(key): json_safe_payload(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe_payload(item) for item in value]
    return value


def session_to_dict(session: Session) -> dict[str, Any]:
    return {
        "id": str(session.id),
        "title": session.title or "",
        "model": session.model or "",
        "provider": session.provider or "",
        "status": session.status,
        "lastActivity": session.last_activity.isoformat() if session.last_activity else None,
        # Additive orchestration link: delegated child sessions carry the
        # session that spawned them; root sessions carry null. The UI uses it
        # to build the agent tab strip (parent family grouping).
        "parentSessionId": (
            None
            if getattr(session, "is_fork", False)
            else (
                str(session.parent_session_id)
                if getattr(session, "parent_session_id", None)
                else None
            )
        ),
        "isFork": bool(getattr(session, "is_fork", False)),
    }


def history_from_chunks(chunks: list[ContextChunk]) -> list[dict[str, Any]]:
    """Turn stored context chunks (newest first) into a chronological transcript."""
    history: list[dict[str, Any]] = []
    for chunk in reversed(chunks):
        payload = chunk.payload
        if chunk.chunk_type == "user_message":
            history.append({"role": "user", "content": str(payload.get("content", ""))})
        elif chunk.chunk_type == "assistant_message":
            content = str(payload.get("content", ""))
            if payload.get("interrupted"):
                content += "\n[Interrupted reply]"
            history.append({"role": "assistant", "content": content})
        elif chunk.chunk_type == "tool_call":
            history.append(
                {
                    "role": "tool",
                    "tool": str(payload.get("tool", "")),
                    "content": str(payload.get("result_preview", "")),
                }
            )
        elif chunk.chunk_type == "compression_summary":
            history.append({"role": "system", "content": str(payload.get("content", ""))})
    return history


def chunk_preview(chunk: ContextChunk) -> str:
    payload = chunk.payload
    for key in ("content", "text", "result_preview"):
        if payload.get(key):
            return str(payload[key])[:200]
    if payload.get("tool"):
        return f"{payload['tool']}({payload.get('args', {})})"[:200]
    return str(payload)[:200]
