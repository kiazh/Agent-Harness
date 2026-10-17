"""In-memory LLM response cache — TTL/size bounded, identity-scoped."""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from typing import Any

from cachetools import TTLCache

from machina.core.models import LLMResponse, ToolDefinition

# ---------------------------------------------------------------------------
# In-memory LLM response cache — eliminates duplicate POSTs from retries with
# identical payloads. Keys include the provider, effective request, and caller
# identity when one was supplied; entries are TTL- and size-bounded.
#
# Residual sharing (N4, final pass): callers passing neither session_id nor
# agent_id stay identity-free — including the main agent path, which reaches
# complete() through usage.complete_call WITHOUT identity kwargs. Identical
# requests from two sessions can share one cached body within the TTL; the
# second reservation re-attributes cached usage (conservative over-count).
# ---------------------------------------------------------------------------
_RESPONSE_CACHE_TTL = 60  # seconds
_RESPONSE_CACHE_MAXSIZE = 256
_RESPONSE_CACHE: TTLCache = TTLCache(maxsize=_RESPONSE_CACHE_MAXSIZE, ttl=_RESPONSE_CACHE_TTL)


def _caller_identity(session_id: uuid.UUID | None, agent_id: str | None) -> str | None:
    """Cache-scoping discriminator derived from the accounting caller, if any.

    A cached response bypasses reserve/finish, so serving one caller's answer
    to a different caller would silently under-count usage for the second one.
    """
    if session_id is None and agent_id is None:
        return None
    return f"{agent_id or ''}:{session_id or ''}"


def _cache_key(
    messages: list[dict[str, str]],
    tools: list[ToolDefinition] | None,
    model: str,
    request: dict[str, Any] | None = None,
    identity: str | None = None,
) -> str:
    """Build a cache key from the request parameters and caller identity."""
    raw = json.dumps(
        {
            "messages": messages,
            "tools": tools,
            "model": model,
            "request": request,
            "identity": identity,
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _copy_cached_response(value: Any) -> Any:
    """Defensive copy so no caller can mutate shared cache state.

    ``LLMResponse`` carries mutable containers (tool_calls list, raw dict,
    usage dict); handing the stored object out would let one caller's edits
    poison every later cache hit.
    """
    if isinstance(value, LLMResponse):
        return LLMResponse(
            content=value.content,
            model=value.model,
            usage=dict(value.usage or {}),
            raw=copy.deepcopy(value.raw or {}),
            tool_calls=copy.deepcopy(value.tool_calls or []),
            cache_hit=value.cache_hit,
        )
    return copy.deepcopy(value)


def _cache_get(key: str) -> Any | None:
    """Return a copy of the cached response if present, else None."""
    value = _RESPONSE_CACHE.get(key)
    if value is None:
        return None
    result = _copy_cached_response(value)
    if isinstance(result, LLMResponse):
        result.cache_hit = True
    return result


def _cache_put(key: str, value: Any) -> None:
    """Store an isolated copy of *value* in the TTL/size-bounded cache."""
    _RESPONSE_CACHE[key] = _copy_cached_response(value)


def _cache_clear() -> None:
    """Clear the response cache. Used by tests to isolate cache state."""
    _RESPONSE_CACHE.clear()
