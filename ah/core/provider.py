"""LLM provider abstraction — OpenRouter, Ollama, OpenAI-compatible."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import httpx

from ah.core.config import config
from ah.core.exceptions import ProviderError, RateLimitError, ValidationError
from ah.core.metrics import metrics
from ah.core.models import (  # noqa: F401 — re-exported for backward compat
    LLMResponse,
    StreamEvent,
    ToolDefinition,
)
from ah.core.usage import usage_store

__all__ = [
    "audit_log",
    "AsyncTokenBucket",
    "LLMProvider",
    "get_provider",
]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Retry with backoff for rate-limited (429) responses
# ---------------------------------------------------------------------------
# The provider retries 429s a small fixed number of times; the agent layer
# must NOT retry 429s again (see ah.core.agent._is_rate_limit_error), or one
# rate limit would fan out into dozens of requests.
# Free-tier models (openrouter/free, *:free) get more retries + longer waits
# because OpenRouter throttles them aggressively.
_MAX_RETRIES = 2
_FREE_MAX_RETRIES = 5
_RETRY_BASE_DELAY = 1.0  # seconds; doubles each attempt
_FREE_BASE_DELAY = 4.0  # free tier starts slower

# Fallback chain when the requested free model keeps 429ing/404ing.
# Refreshed 2026-10-05 from GET /api/v1/models (all :free). First success wins.
_FREE_FALLBACK_MODELS = (
    "qwen/qwen3.8-27b:free",
    "google/gemma-4-26b-a4b-it:free",
    "google/gemma-4-31b-it:free",
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
    "liquid/lfm-2.5-2.6b:free",
    "cohere/north-mini-code:free",
)


def _is_free_model(model: str) -> bool:
    m = (model or "").lower()
    return m == "openrouter/free" or m.endswith(":free") or m.endswith("/free")


def _quota_exhausted(resp: httpx.Response) -> bool:
    """True when OpenRouter reports the *account-wide daily* free quota is gone.

    ``X-RateLimit-Remaining: 0`` means no free model will serve this account
    until the window resets — the cap is account-wide, not per model. Rotating
    through the fallback chain cannot help and only burns more requests, so
    callers must stop immediately instead of trying the next candidate.
    """
    try:
        remaining = resp.headers.get("x-ratelimit-remaining")
        if remaining is not None and remaining.strip().isdigit():
            return int(remaining.strip()) == 0
    except Exception:
        pass
    return False


def _quota_reset_at(resp: httpx.Response) -> float | None:
    """Parse ``X-RateLimit-Reset`` (epoch milliseconds) into epoch seconds."""
    try:
        raw = resp.headers.get("x-ratelimit-reset", "")
        if raw and raw.strip().isdigit():
            value = int(raw.strip())
            # Heuristic: 13+ digits is milliseconds, fewer is already seconds.
            return value / 1000.0 if value > 10_000_000_000 else float(value)
    except Exception:
        pass
    return None


def _quota_message(resp: httpx.Response, model: str) -> str:
    """Human-readable exhaustion message including the reset time (local)."""
    limit = resp.headers.get("x-ratelimit-limit", "").strip() or "?"
    reset_at = _quota_reset_at(resp)
    when = ""
    if reset_at is not None:
        import datetime

        local = datetime.datetime.fromtimestamp(reset_at).astimezone()
        when = f" Quota resets {local.strftime('%Y-%m-%d %H:%M %Z')}."
    return (
        f"OpenRouter free-tier daily quota exhausted ({limit} requests/day, "
        f"account-wide).{when} Every free model returns 429 until then — "
        f"purchase credits for 1000/day, or switch to a paid model."
    )


def _retry_delay(attempt: int, resp: httpx.Response | None, free: bool) -> float:
    # Honor OpenRouter's Retry-After when present (seconds or HTTP date).
    if resp is not None:
        try:
            ra = resp.headers.get("retry-after", "")
            if ra and ra.strip().isdigit():
                return min(float(ra.strip()), 60.0)
        except Exception:
            pass
    import random

    base = _FREE_BASE_DELAY if free else _RETRY_BASE_DELAY
    delay = base * (2 ** attempt)
    delay = min(delay, 30.0 if free else 8.0)
    return delay + random.uniform(0, 1.0)  # jitter so parallel turns don't thunder


async def _post_with_retry(
    client: httpx.AsyncClient,
    url: str,
    payload: dict[str, Any],
) -> httpx.Response:
    """POST with exponential backoff on 429 (rate limit) responses.

    Paid models: retries 2x (1s, 2s). Free models: ONE attempt here —
    the caller walks the fallback chain instead. Retrying the same throttled
    free model 6x is what burns the per-minute quota (36 POSTs per hello).
    """
    model = str(payload.get("model", ""))
    free = _is_free_model(model)
    # Free: single attempt; fallback loop handles rotation. Paid: 2 retries.
    max_retries = 0 if free else _MAX_RETRIES
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        resp = await client.post(url, json=payload)
        if resp.status_code != 429:
            return resp
        # Account-wide daily quota gone: no free model can serve this, so
        # rotating candidates or sleeping would only waste more requests.
        if free and _quota_exhausted(resp):
            raise RateLimitError(
                _quota_message(resp, model),
                reset_at=_quota_reset_at(resp),
                daily_limit=int(resp.headers.get("x-ratelimit-limit") or 0) or None,
                remaining=0,
            )
        last_exc = httpx.HTTPStatusError(
            f"429 Too Many Requests (attempt {attempt + 1}/{max_retries + 1})",
            request=resp.request,
            response=resp,
        )
        if attempt < max_retries:
            delay = _retry_delay(attempt, resp, free)
            logger.warning(
                "429 rate limited (model=%s), retrying in %.1fs (attempt %d/%d). "
                "Free tier: wait or /model <paid> to skip the queue.",
                model,
                delay,
                attempt + 1,
                max_retries + 1,
            )
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Audit logging
# ---------------------------------------------------------------------------
_audit_logger = logging.getLogger("ah.audit")
_audit_logger.setLevel(logging.INFO)
if not _audit_logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s [AUDIT] %(message)s"))
    _audit_logger.addHandler(_handler)


def _sanitize_value(value: Any) -> Any:
    """Sanitize a value for audit logging — redact potential secrets."""
    if isinstance(value, str):
        sanitized = value
        sanitized = re.sub(r"sk-[a-zA-Z0-9]{20,}", "[REDACTED_KEY]", sanitized)
        sanitized = re.sub(r"sk-ant-[a-zA-Z0-9\-_]{20,}", "[REDACTED_KEY]", sanitized)
        sanitized = re.sub(r"sk-or-[a-zA-Z0-9]{20,}", "[REDACTED_KEY]", sanitized)
        sanitized = re.sub(
            r"(Bearer\s+)[a-zA-Z0-9\-._~+/]+=*",
            r"\1[REDACTED_TOKEN]",
            sanitized,
            flags=re.IGNORECASE,
        )
        sanitized = re.sub(
            r"(password|passwd|pwd)\s*[=:]\s*['\"]?([^\s'\"]{4,})['\"]?",
            r"\1=[REDACTED_PASSWORD]",
            sanitized,
            flags=re.IGNORECASE,
        )
        sanitized = re.sub(
            r"(postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://([^:]+):([^@]+)@",
            r"\1://\2:[REDACTED_DB_PASSWORD]@",
            sanitized,
        )
        sanitized = re.sub(r"AKIA[0-9A-Z]{16}", "[REDACTED_AWS_KEY]", sanitized)
        sanitized = re.sub(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            "[REDACTED_PRIVATE_KEY]",
            sanitized,
            flags=re.DOTALL,
        )
        return sanitized
    elif isinstance(value, dict):
        return {
            k: ("[REDACTED]" if _is_sensitive_audit_key(k) else _sanitize_value(v))
            for k, v in value.items()
        }
    elif isinstance(value, list):
        return [_sanitize_value(v) for v in value]
    elif isinstance(value, tuple):
        return tuple(_sanitize_value(v) for v in value)
    else:
        return value


def _is_sensitive_audit_key(key: Any) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
    return normalized == "authorization" or normalized.endswith(
        (
            "apikey",
            "token",
            "secret",
            "password",
            "passwd",
            "pwd",
            "credential",
            "privatekey",
            "accesskey",
        )
    )


def audit_log(event_type: str, **kwargs) -> None:
    """Log a security-relevant event as JSON. All kwargs are sanitized to redact secrets."""
    sanitized_kwargs = _sanitize_value(kwargs)
    entry = {
        "timestamp": time.time(),
        "event": event_type,
        **sanitized_kwargs,
    }
    _audit_logger.info(json.dumps(entry, default=str))
    from ah.observability.audit import audit_persistence

    audit_persistence.submit(entry)


# ---------------------------------------------------------------------------
# Rate limiting — async token bucket
# ---------------------------------------------------------------------------
class AsyncTokenBucket:
    """Async token bucket rate limiter.

    Configurable via LLM_RATE_LIMIT_CALLS_PER_MINUTE env var (default: 10).
    """

    def __init__(self, rate: float, capacity: int) -> None:
        self.rate = rate  # tokens per second
        self.capacity = capacity
        self.tokens = float(capacity)
        self.last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: int = 1) -> None:
        """Acquire tokens, waiting if necessary."""
        if self.rate == 0:
            return  # A zero configured limit disables throttling.
        while True:
            async with self._lock:
                now = time.monotonic()
                elapsed = now - self.last_refill
                self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
                self.last_refill = now
                if self.tokens >= tokens:
                    self.tokens -= tokens
                    return
                wait_time = (tokens - self.tokens) / self.rate
            await asyncio.sleep(wait_time)


def _get_rate_limiter(provider_key: str = "default") -> AsyncTokenBucket:
    """Return the shared rate limiter for *provider_key*.

    One bucket per provider (process-wide singleton): per-instance limiters
    let N provider instances issue N× the configured rate.
    """
    limiter = _RATE_LIMITERS.get(provider_key)
    if limiter is None:
        calls_per_minute = int(config.get("rate_limit_calls_per_minute"))
        if calls_per_minute < 0:
            raise ValidationError("rate_limit_calls_per_minute must be non-negative")
        limiter = AsyncTokenBucket(rate=calls_per_minute / 60.0, capacity=calls_per_minute)
        _RATE_LIMITERS[provider_key] = limiter
    return limiter


# Process-wide limiter buckets, keyed by provider name (see _get_rate_limiter).
_RATE_LIMITERS: dict[str, AsyncTokenBucket] = {}

# ---------------------------------------------------------------------------
# In-memory LLM response cache — eliminates duplicate POSTs from retries with
# identical payloads. Keyed by sha256 of (messages, tools, model), TTL 60s.
# ---------------------------------------------------------------------------
_RESPONSE_CACHE_TTL = 60  # seconds
_RESPONSE_CACHE: dict[str, tuple[float, Any]] = {}


def _cache_key(messages: list[dict[str, str]], tools: list[ToolDefinition] | None, model: str) -> str:
    """Build a cache key from the request parameters."""
    raw = json.dumps(
        {"messages": messages, "tools": tools, "model": model},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _cache_get(key: str) -> Any | None:
    """Return cached response if present and not expired, else None."""
    entry = _RESPONSE_CACHE.get(key)
    if entry is None:
        return None
    ts, value = entry
    if time.monotonic() - ts > _RESPONSE_CACHE_TTL:
        del _RESPONSE_CACHE[key]
        return None
    return value


def _cache_put(key: str, value: Any) -> None:
    """Store a response in the cache with the current timestamp."""
    _RESPONSE_CACHE[key] = (time.monotonic(), value)


def _cache_clear() -> None:
    """Clear the response cache. Used by tests to isolate cache state."""
    _RESPONSE_CACHE.clear()


# ---------------------------------------------------------------------------
# Data classes — re-exported from ah.core.models for backward compatibility
# ---------------------------------------------------------------------------
# LLMResponse, StreamEvent, ToolDefinition are now defined in ah.core.models


# ---------------------------------------------------------------------------
# Input validation helpers
# ---------------------------------------------------------------------------
def _validate_messages(messages: list[dict[str, str]]) -> None:
    """Validate LLM message format."""
    if not isinstance(messages, list) or not messages:
        raise ValidationError("messages must be a non-empty list")
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            raise ValidationError(f"Message at index {i} must be a dict")
        if "role" not in msg:
            raise ValidationError(f"Message at index {i} missing 'role' field")
        if "content" not in msg:
            raise ValidationError(f"Message at index {i} missing 'content' field")


def _validate_params(temperature: float, max_tokens: int) -> None:
    """Validate LLM call parameters."""
    if not (0.0 <= temperature <= 2.0):
        raise ValidationError(f"temperature must be between 0.0 and 2.0, got {temperature}")
    if not (1 <= max_tokens <= 32768):
        raise ValidationError(f"max_tokens must be between 1 and 32768, got {max_tokens}")


def _tools_payload(tools: list[ToolDefinition]) -> list[dict[str, Any]]:
    """Convert tool definitions to the OpenAI-compatible function-calling format."""
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            },
        }
        for t in tools
    ]


# ---------------------------------------------------------------------------
# Base provider
# ---------------------------------------------------------------------------
class LLMProvider:
    """Base provider interface."""

    async def complete(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        tools: list[ToolDefinition] | None = None,
    ) -> LLMResponse:
        raise NotImplementedError

    async def stream_complete(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        tools: list[ToolDefinition] | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Stream completion — yields StreamEvent objects.

        Yields StreamEvent(type="text", content=...) for each text delta,
        then StreamEvent(type="done", response=LLMResponse) at the end.
        """
        raise NotImplementedError

    async def embed(self, text: str) -> list[float]:
        """Embed a text string into a vector."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# OpenRouter provider
# ---------------------------------------------------------------------------
class OpenRouterProvider(LLMProvider):
    """OpenRouter API — OpenAI-compatible, multi-model."""

    BASE_URL = "https://openrouter.ai/api/v1"

    def __init__(self, api_key: str | None = None, model: str = "openrouter/free") -> None:
        self.api_key = api_key or config.get("openrouter_api_key") or ""
        if not self.api_key:
            raise ValidationError("OPENROUTER_API_KEY not set")
        self.model = model
        self._rate_limiter = _get_rate_limiter("openrouter")
        self.client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/kiazh/agent-harness",
            },
            timeout=120.0,
        )

    async def complete(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        tools: list[ToolDefinition] | None = None,
        session_id: uuid.UUID | None = None,
        agent_id: str | None = None,
    ) -> LLMResponse:
        # Input validation
        _validate_messages(messages)
        _validate_params(temperature, max_tokens)

        # Rate limiting
        await self._rate_limiter.acquire()

        model = model or self.model
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = _tools_payload(tools)

        # Check cache before making any HTTP request
        ckey = _cache_key(messages, tools, model)
        cached = _cache_get(ckey)
        if cached is not None:
            logger.debug("cache hit for model=%s", model)
            return cached

        audit_log("llm_call_start", provider="openrouter", model=model, message_count=len(messages))

        # Reserve usage if session_id and agent_id are provided
        reservation_id = None
        if session_id is not None and agent_id is not None:
            reservation_id = await usage_store.reserve(
                session_id,
                agent_id,
                "openrouter",
                model,
                messages,
                tools or [],
                max_tokens,
            )

        start = time.monotonic()
        candidates = [model]
        if _is_free_model(model):
            # Shrink over-long requests: free tier 429s harder on big max_tokens.
            payload["max_tokens"] = min(max_tokens, 1024)
            for fb in _FREE_FALLBACK_MODELS:
                if fb != model and fb not in candidates:
                    candidates.append(fb)
        last_err: Exception | None = None
        data: dict[str, Any] | None = None
        used_model = model
        for cand in candidates:
            payload["model"] = cand
            try:
                resp = await _post_with_retry(self.client, "/chat/completions", payload)
                resp.raise_for_status()
                data = resp.json()
                used_model = cand
                if cand != model:
                    logger.info("free-tier fallback succeeded with model=%s", cand)
                    audit_log("llm_free_fallback", provider="openrouter", from_model=model, to_model=cand)
                break
            except httpx.HTTPStatusError as e:
                status = e.response.status_code if e.response is not None else 0
                # 429 = throttled, 404 = retired free id OR no free provider
                # can serve this payload (e.g. tools unsupported) -> next model,
                # and for the last candidate retry once without tools.
                retryable = status in (429, 404) if _is_free_model(cand) else status == 429
                last_err = e
                if status == 404 and payload.get("tools") and cand == candidates[-1]:
                    # Per openrouter/free docs, 404 also means "no provider can
                    # serve the request" — free models often can't do tools.
                    # Retry plain-chat before giving up so `hello` still works.
                    logger.warning("free model %s 404 with tools, retrying without tools", cand)
                    stripped = {k: v for k, v in payload.items() if k != "tools"}
                    try:
                        resp2 = await _post_with_retry(self.client, "/chat/completions", stripped)
                        resp2.raise_for_status()
                        data = resp2.json()
                        used_model = cand
                        audit_log("llm_free_fallback_no_tools", provider="openrouter", model=cand)
                        break
                    except RateLimitError:
                        # Quota exhausted — do not mask it as a generic failure.
                        raise
                    except Exception as e2:
                        last_err = e2
                        break
                if not retryable or cand == candidates[-1]:
                    break
                logger.warning("free model %s got %s, trying fallback %s after 2s", cand, status, candidates[candidates.index(cand) + 1])
                await asyncio.sleep(2.0)
                continue
            except RateLimitError:
                # Daily quota is exhausted account-wide: trying other free
                # models cannot succeed, so fail fast with the real reason.
                raise
            except Exception as e:
                last_err = e
                break
        try:
            if data is None:
                raise last_err or RuntimeError("LLM call failed with no response")
        except Exception as e:
            duration_ms = (time.monotonic() - start) * 1000
            metrics.record_llm_call(
                session_id="",
                model=model,
                duration_ms=duration_ms,
                is_error=True,
            )
            if reservation_id is not None:
                await usage_store.finish(reservation_id, failed=True)
            audit_log("llm_call_error", provider="openrouter", model=model, error=str(e))
            raise

        duration_ms = (time.monotonic() - start) * 1000
        # OpenRouter can return HTTP 200 with an error body (and some providers
        # return an empty choices list), so raise_for_status() is not enough.
        choices = data.get("choices")
        if not choices or not isinstance(choices, list):
            error = data.get("error") or {}
            detail = error.get("message") if isinstance(error, dict) else str(error)
            message = (
                f"provider returned no choices for {used_model}: "
                f"{detail or data or 'empty response'}"
            )
            metrics.record_llm_call(
                session_id="", model=used_model, duration_ms=duration_ms, is_error=True
            )
            if reservation_id is not None:
                await usage_store.finish(reservation_id, failed=True)
            audit_log(
                "llm_call_error",
                provider="openrouter",
                model=used_model,
                error="no_choices",
            )
            raise ProviderError(message)

        choice = choices[0]
        message = choice.get("message") or {}
        content = message.get("content", "")
        tool_calls = message.get("tool_calls", []) or []
        usage = data.get("usage", {})

        metrics.record_llm_call(
            session_id="",
            model=data.get("model", model),
            duration_ms=duration_ms,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
        )

        # Finish usage reservation
        if reservation_id is not None:
            await usage_store.finish(
                reservation_id,
                usage,
                model=data.get("model", model),
            )

        audit_log(
            "llm_call_complete",
            provider="openrouter",
            model=data.get("model", model),
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
            tool_calls=len(tool_calls),
        )

        response = LLMResponse(
            content=content,
            model=data.get("model", model),
            usage={
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            },
            raw=data,
            tool_calls=tool_calls,
        )
        _cache_put(ckey, response)
        return response

    async def stream_complete(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        tools: list[ToolDefinition] | None = None,
        session_id: uuid.UUID | None = None,
        agent_id: str | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Stream completion via SSE — yields text deltas as they arrive."""
        # Input validation
        _validate_messages(messages)
        _validate_params(temperature, max_tokens)

        # Rate limiting
        await self._rate_limiter.acquire()

        model = model or self.model
        candidates = [model]
        if _is_free_model(model):
            payload_max = min(max_tokens, 1024)
            for fb in _FREE_FALLBACK_MODELS:
                if fb != model and fb not in candidates:
                    candidates.append(fb)
        else:
            payload_max = max_tokens
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": payload_max,
            "stream": True,
        }
        # NOTE: openrouter/free does not advertise stream_options in its
        # capability list — only send include_usage for paid models.
        if not _is_free_model(model):
            # Ask for a final usage chunk so streamed turns report real token counts
            payload["stream_options"] = {"include_usage": True}
        if tools:
            payload["tools"] = _tools_payload(tools)

        audit_log(
            "llm_stream_start", provider="openrouter", model=model, message_count=len(messages)
        )

        # Reserve usage if session_id and agent_id are provided
        reservation_id = None
        if session_id is not None and agent_id is not None:
            reservation_id = await usage_store.reserve(
                session_id,
                agent_id,
                "openrouter",
                model,
                messages,
                tools or [],
                max_tokens,
            )

        content_parts: list[str] = []
        tool_calls_by_index: dict[int, dict[str, str]] = {}
        stream_usage: dict[str, int] = {}
        resolved_model = model

        last_stream_err: Exception | None = None
        for cand in candidates:
            payload["model"] = cand
            resolved_model = cand
            content_parts = []
            tool_calls_by_index = {}
            stream_usage = {}
            try:
                async with self.client.stream("POST", "/chat/completions", json=payload) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        data = line[6:]
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        resolved_model = chunk.get("model") or resolved_model
                        # The usage chunk arrives with an empty choices list.
                        if chunk.get("usage"):
                            stream_usage = chunk["usage"]
                        choices = chunk.get("choices", [])
                        if not choices:
                            continue
                        delta = choices[0].get("delta", {})

                        # Text content
                        content = delta.get("content", "")
                        if content:
                            content_parts.append(content)
                            yield StreamEvent(type="text", content=content)

                        # Tool calls — accumulate deltas by index
                        tc_deltas = delta.get("tool_calls", [])
                        for tc_delta in tc_deltas:
                            idx = tc_delta.get("index", 0)
                            if idx not in tool_calls_by_index:
                                tool_calls_by_index[idx] = {"id": "", "name": "", "arguments": ""}
                            tc = tool_calls_by_index[idx]
                            if tc_delta.get("id"):
                                tc["id"] = tc_delta["id"]
                            func = tc_delta.get("function", {})
                            if func.get("name"):
                                tc["name"] = func["name"]
                            if func.get("arguments"):
                                frag = func["arguments"]
                                if isinstance(frag, dict):
                                    # Some gateways emit structured args; stringify
                                    # before concatenating the streamed fragments.
                                    frag = json.dumps(frag)
                                tc["arguments"] += frag
                # Success — stop trying fallbacks.
                last_stream_err = None
                break
            except Exception as e:
                if isinstance(e, RateLimitError):
                    # Account-wide daily quota exhausted: no free candidate can
                    # succeed, so stop now instead of sleeping through the rest
                    # of the chain and burning more of the quota.
                    if reservation_id is not None:
                        await usage_store.finish(reservation_id, failed=True)
                    audit_log(
                        "llm_stream_rate_limited",
                        provider="openrouter",
                        model=cand,
                        error=str(e),
                    )
                    raise
                status = None
                if isinstance(e, httpx.HTTPStatusError) and e.response is not None:
                    status = e.response.status_code
                nothing_yielded = not content_parts and not tool_calls_by_index
                retryable = status in (429, 404) and nothing_yielded and cand != candidates[-1]
                if retryable:
                    logger.warning("free stream model %s got %s, trying fallback after 2s", cand, status)
                    last_stream_err = e
                    # Discard any partial state so a throttled candidate cannot
                    # contaminate the next one.
                    content_parts = []
                    tool_calls_by_index = {}
                    stream_usage = {}
                    await asyncio.sleep(2.0)
                    continue
                if status == 404 and nothing_yielded and payload.get("tools") and cand == candidates[1]:
                    # Same "no provider can serve tools" case as non-stream:
                    # retry the first fallback model once as plain chat.
                    logger.warning("free stream model %s 404 with tools, retrying without tools", cand)
                    payload.pop("tools", None)
                    content_parts = []
                    tool_calls_by_index = {}
                    stream_usage = {}
                    try:
                        async with self.client.stream("POST", "/chat/completions", json=payload) as resp2:
                            resp2.raise_for_status()
                            async for line in resp2.aiter_lines():
                                if not line.startswith("data: "):
                                    continue
                                d2 = line[6:]
                                if d2 == "[DONE]":
                                    break
                                try:
                                    ch2 = json.loads(d2)
                                except json.JSONDecodeError:
                                    continue
                                resolved_model = ch2.get("model") or resolved_model
                                if ch2.get("usage"):
                                    stream_usage = ch2["usage"]
                                chs = ch2.get("choices", [])
                                if not chs:
                                    continue
                                dl2 = chs[0].get("delta", {})
                                c2 = dl2.get("content", "")
                                if c2:
                                    content_parts.append(c2)
                                    yield StreamEvent(type="text", content=c2)
                        last_stream_err = None
                        break
                    except Exception as e2:
                        e = e2
                if reservation_id is not None:
                    await usage_store.finish(reservation_id, failed=True)
                audit_log("llm_stream_error", provider="openrouter", model=cand, error=str(e))
                raise

        # Build final tool calls list
        tool_calls: list[dict] = []
        for idx in sorted(tool_calls_by_index.keys()):
            tc = tool_calls_by_index[idx]
            tool_calls.append(
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": tc["arguments"],
                    },
                }
            )

        full_content = "".join(content_parts)
        usage = {
            "prompt_tokens": stream_usage.get("prompt_tokens", 0),
            "completion_tokens": stream_usage.get("completion_tokens", 0),
            "total_tokens": stream_usage.get("total_tokens", 0),
        }

        # Finish usage reservation
        if reservation_id is not None:
            await usage_store.finish(reservation_id, usage, model=resolved_model)

        audit_log(
            "llm_stream_complete", provider="openrouter", model=model, tool_calls=len(tool_calls)
        )

        yield StreamEvent(
            type="done",
            response=LLMResponse(
                content=full_content,
                model=resolved_model,
                usage=usage,
                tool_calls=tool_calls,
            ),
        )

    async def embed(self, text: str) -> list[float]:
        """Embed using OpenRouter's embedding endpoint."""
        resp = await self.client.post(
            "/embeddings",
            json={"model": "text-embedding-3-small", "input": text},
        )
        resp.raise_for_status()
        data = resp.json()
        # OpenRouter can return 200 with an error body or empty data list.
        if not data.get("data") or not isinstance(data["data"], list):
            raise ProviderError(
                f"OpenRouter embed returned no data: {data.get('error') or data}"
            )
        first = data["data"][0]
        if not isinstance(first, dict) or "embedding" not in first:
            raise ProviderError(
                f"OpenRouter embed response missing 'embedding' key: {first}"
            )
        return first["embedding"]

    async def close(self) -> None:
        # Close the shared client (it will be recreated if needed)
        await self.client.aclose()


# ---------------------------------------------------------------------------
# Ollama provider
# ---------------------------------------------------------------------------
class OllamaProvider(LLMProvider):
    """Local Ollama provider — free, self-hosted."""

    def __init__(self, model: str = "llama3.1", base_url: str = "http://localhost:11434") -> None:
        self.model = model
        self.base_url = base_url
        self._rate_limiter = _get_rate_limiter("ollama")
        self.client = httpx.AsyncClient(base_url=base_url, timeout=120.0)

    async def complete(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        tools: list[ToolDefinition] | None = None,
        session_id: uuid.UUID | None = None,
        agent_id: str | None = None,
    ) -> LLMResponse:
        # Input validation
        _validate_messages(messages)
        _validate_params(temperature, max_tokens)

        # Rate limiting
        await self._rate_limiter.acquire()

        payload: dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
        }
        if tools:
            payload["tools"] = _tools_payload(tools)

        # Check cache before making any HTTP request
        ckey = _cache_key(messages, tools, model or self.model)
        cached = _cache_get(ckey)
        if cached is not None:
            logger.debug("cache hit for ollama model=%s", model or self.model)
            return cached

        audit_log(
            "llm_call_start",
            provider="ollama",
            model=model or self.model,
            message_count=len(messages),
        )

        # Reserve usage if session_id and agent_id are provided
        reservation_id = None
        if session_id is not None and agent_id is not None:
            reservation_id = await usage_store.reserve(
                session_id,
                agent_id,
                "ollama",
                model or self.model,
                messages,
                tools or [],
                max_tokens,
            )

        start = time.monotonic()
        try:
            resp = await self.client.post("/api/chat", json=payload)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            duration_ms = (time.monotonic() - start) * 1000
            metrics.record_llm_call(
                session_id="",
                model=model or self.model,
                duration_ms=duration_ms,
                is_error=True,
            )
            if reservation_id is not None:
                await usage_store.finish(reservation_id, failed=True)
            audit_log("llm_call_error", provider="ollama", model=model or self.model, error=str(e))
            raise

        # Ollama can return 200 with an error body (e.g. model not found).
        if "error" in data:
            duration_ms = (time.monotonic() - start) * 1000
            metrics.record_llm_call(
                session_id="",
                model=model or self.model,
                duration_ms=duration_ms,
                is_error=True,
            )
            if reservation_id is not None:
                await usage_store.finish(reservation_id, failed=True)
            audit_log(
                "llm_call_error",
                provider="ollama",
                model=model or self.model,
                error=data["error"],
            )
            raise ProviderError(f"Ollama error: {data['error']}")

        duration_ms = (time.monotonic() - start) * 1000
        message = data["message"]
        content = message.get("content", "")
        tool_calls = message.get("tool_calls", []) or []

        metrics.record_llm_call(
            session_id="",
            model=data.get("model", model or self.model),
            duration_ms=duration_ms,
            prompt_tokens=data.get("prompt_eval_count", 0),
            completion_tokens=data.get("eval_count", 0),
        )

        # Finish usage reservation
        if reservation_id is not None:
            await usage_store.finish(
                reservation_id,
                {
                    "prompt_tokens": data.get("prompt_eval_count", 0),
                    "completion_tokens": data.get("eval_count", 0),
                    "total_tokens": data.get("prompt_eval_count", 0) + data.get("eval_count", 0),
                },
                model=data.get("model", model or self.model),
            )

        audit_log(
            "llm_call_complete",
            provider="ollama",
            model=data.get("model", model or self.model),
            prompt_tokens=data.get("prompt_eval_count", 0),
            completion_tokens=data.get("eval_count", 0),
            tool_calls=len(tool_calls),
        )

        response = LLMResponse(
            content=content,
            model=data.get("model", model or self.model),
            usage={
                "prompt_tokens": data.get("prompt_eval_count", 0),
                "completion_tokens": data.get("eval_count", 0),
                "total_tokens": data.get("prompt_eval_count", 0) + data.get("eval_count", 0),
            },
            raw=data,
            tool_calls=tool_calls,
        )
        _cache_put(ckey, response)
        return response

    async def stream_complete(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        tools: list[ToolDefinition] | None = None,
        session_id: uuid.UUID | None = None,
        agent_id: str | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Stream completion via newline-delimited JSON."""
        # Input validation
        _validate_messages(messages)
        _validate_params(temperature, max_tokens)

        # Rate limiting
        await self._rate_limiter.acquire()

        payload: dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
            "stream": True,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
        }
        if tools:
            payload["tools"] = _tools_payload(tools)

        audit_log(
            "llm_stream_start",
            provider="ollama",
            model=model or self.model,
            message_count=len(messages),
        )

        # Reserve usage if session_id and agent_id are provided
        reservation_id = None
        if session_id is not None and agent_id is not None:
            reservation_id = await usage_store.reserve(
                session_id,
                agent_id,
                "ollama",
                model or self.model,
                messages,
                tools or [],
                max_tokens,
            )

        content_parts: list[str] = []
        tool_calls: list[dict] = []

        try:
            async with self.client.stream("POST", "/api/chat", json=payload) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    message = data.get("message", {})
                    content = message.get("content", "")
                    if content:
                        content_parts.append(content)
                        yield StreamEvent(type="text", content=content)

                    # Ollama streaming tool calls
                    tc = message.get("tool_calls", [])
                    if tc:
                        for call in tc:
                            if isinstance(call, dict):
                                fn = call.get("function", {}) or {}
                                args = fn.get("arguments", "")
                                if isinstance(args, dict):
                                    # Normalize structured args to a string so
                                    # downstream json.loads / concat keeps working.
                                    call = {
                                        **call,
                                        "function": {**fn, "arguments": json.dumps(args)},
                                    }
                            tool_calls.append(call)

                    if data.get("done"):
                        break
        except Exception as e:
            if reservation_id is not None:
                await usage_store.finish(reservation_id, failed=True)
            audit_log(
                "llm_stream_error", provider="ollama", model=model or self.model, error=str(e)
            )
            raise

        full_content = "".join(content_parts)

        # Finish usage reservation
        if reservation_id is not None:
            await usage_store.finish(
                reservation_id,
                {},
                model=model or self.model,
            )

        audit_log(
            "llm_stream_complete",
            provider="ollama",
            model=model or self.model,
            tool_calls=len(tool_calls),
        )

        yield StreamEvent(
            type="done",
            response=LLMResponse(
                content=full_content,
                model=model or self.model,
                usage={},
                tool_calls=tool_calls,
            ),
        )

    async def embed(self, text: str) -> list[float]:
        """Embed using Ollama's embedding endpoint."""
        resp = await self.client.post(
            f"{self.base_url}/api/embeddings",
            json={"model": "nomic-embed-text", "prompt": text},
        )
        resp.raise_for_status()
        data = resp.json()
        return data["embedding"]

    async def close(self) -> None:
        # Close the shared client (it will be recreated if needed)
        await self.client.aclose()


def get_provider(
    provider: str = "openrouter",
    model: str | None = None,
) -> LLMProvider:
    """Factory: return configured provider."""
    if provider == "openrouter":
        return OpenRouterProvider(model=model or "openrouter/free")
    elif provider == "ollama":
        return OllamaProvider(model=model or "llama3.1")
    else:
        raise ValidationError(f"Unknown provider: {provider}")
