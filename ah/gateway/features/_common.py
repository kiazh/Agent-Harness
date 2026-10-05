"""Common helpers for gateway feature handlers."""

from __future__ import annotations

import uuid
from typing import Any

from ah.core.config import DEFAULTS, SECRET_KEYS, config
from ah.gateway.errors import INVALID_PARAMS, RpcError

MEMORY_CATEGORIES = ("preference", "decision", "fact", "event", "transient")


def _str(params: dict[str, Any], key: str, *, required: bool = True, max_len: int = 10_000) -> str:
    value = params.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise RpcError(INVALID_PARAMS, f"{key} is required")
        return ""
    if not isinstance(value, str):
        raise RpcError(INVALID_PARAMS, f"{key} must be a string")
    value = value.strip()
    if len(value) > max_len:
        raise RpcError(INVALID_PARAMS, f"{key} is too long (max {max_len} characters)")
    return value


def _int(params: dict[str, Any], key: str, default: int, lo: int, hi: int) -> int:
    value = params.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
        raise RpcError(INVALID_PARAMS, f"{key} must be an integer between {lo} and {hi}")
    return value


def _uuid(params: dict[str, Any], key: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(params.get(key)))
    except (ValueError, TypeError):
        raise RpcError(INVALID_PARAMS, f"{key} must be a UUID") from None


def _iso(value: Any) -> str | None:
    return value.isoformat() if value else None


def _memory(
    m: Any, score: float | None = None, persona_interpretation: str | None = None
) -> dict[str, Any]:
    data = {
        "id": str(m.id),
        "content": m.content,
        "category": m.category,
        "importance": round(float(m.importance), 3),
        "accessCount": m.access_count,
        "createdAt": _iso(m.created_at),
        "sessionId": str(m.session_id) if m.session_id else None,
    }
    if score is not None:
        data["score"] = round(float(score), 4)
    if persona_interpretation is not None:
        data["personaInterpretation"] = persona_interpretation
    return data


def _pending(p: Any) -> dict[str, Any]:
    return {
        "id": str(p.id),
        "content": p.content,
        "category": p.category,
        "importance": round(float(p.importance), 3),
        "redactions": list(p.redactions),
        "status": str(p.status),
        "createdAt": _iso(p.created_at),
    }


def _skill(s: Any, content: bool = False) -> dict[str, Any]:
    data = {
        "name": s.name,
        "description": s.description,
        "triggers": list(s.triggers),
        "version": s.version,
        "sourceType": s.source_type,
        "usageCount": s.usage_count,
        "enabled": s.enabled,
    }
    if content:
        data["content"] = s.content
    return data


def _profile(p: Any) -> dict[str, Any]:
    return {
        "userId": p.user_id,
        "displayName": p.display_name,
        "preferences": p.preferences,
        "interactionCount": p.interaction_count,
        "topTopics": [{"topic": t, "count": c} for t, c in p.get_top_topics()],
        "updatedAt": _iso(p.updated_at),
    }


def _config_snapshot() -> dict[str, Any]:
    return {
        key: (bool(config.get(key)) if key in SECRET_KEYS else config.get(key)) for key in DEFAULTS
    }


def coerce_config_value(key: str, value: Any) -> Any:
    """Convert *value* to the type of ``DEFAULTS[key]``; raise RpcError if impossible."""
    default = DEFAULTS[key]
    try:
        if isinstance(default, bool):
            if isinstance(value, bool):
                return value
            text = str(value).strip().lower()
            if text in ("true", "1", "yes", "on"):
                return True
            if text in ("false", "0", "no", "off"):
                return False
            raise ValueError
        if isinstance(default, int):
            return int(value)
        if isinstance(default, float):
            return float(value)
        return str(value)
    except (ValueError, TypeError):
        expected = type(default).__name__
        raise RpcError(INVALID_PARAMS, f"{key} must be a {expected}") from None
