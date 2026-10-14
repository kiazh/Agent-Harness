"""LLM provider abstraction — OpenRouter, Ollama, OpenAI-compatible."""

from __future__ import annotations

import asyncio
import copy
import datetime
import hashlib
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from cachetools import TTLCache

from machina.core.config import config
from machina.core.exceptions import ProviderError, RateLimitError, ValidationError
from machina.core.images import anthropic_content, ollama_messages
from machina.core.metrics import metrics
from machina.core.models import (  # noqa: F401 — re-exported for backward compat
    LLMResponse,
    StreamEvent,
    ToolDefinition,
)
from machina.core.usage import _estimate_tokens, usage_store
from machina.observability.diagnostics import record_failure

__all__ = [
    "audit_log",
    "AsyncTokenBucket",
    "LLMProvider",
    "get_provider",
    "PROVIDERS",
    "PROVIDER_SPECS",
    "ProviderSpec",
    "REASONING_EFFORTS",
    "normalize_reasoning_effort",
    "reload_rate_limiters",
]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Reasoning effort — per-provider reasoning depth (low|medium|high).
#
# Each provider family exposes this differently, so the mapping lives here
# next to the transport code:
#   openai/deepseek/xai/groq/together .. top-level ``reasoning_effort``
#   openrouter ........................ ``reasoning: {"effort": ...}``
#   anthropic ......................... ``thinking`` budget in tokens
#   ollama ............................ boolean ``think`` flag
#   mistral/google .................... no API knob (model choice expresses it)
# The value is only ever sent when explicitly set (per-call argument or the
# ``reasoning_effort`` config key); unset means "provider default", so existing
# behavior is unchanged.
# ---------------------------------------------------------------------------
REASONING_EFFORTS = ("low", "medium", "high")

# Anthropic thinking budgets per effort level (tokens reserved for thinking).
ANTHROPIC_THINKING_BUDGETS = {"low": 1024, "medium": 4096, "high": 10000}


def normalize_reasoning_effort(value: str | None) -> str:
    """Normalize a reasoning-effort value; ``""``/None means unset (provider default)."""
    if value is None:
        return ""
    normalized = str(value).strip().lower()
    if normalized in ("", "none", "off", "default", "auto"):
        return ""
    if normalized not in REASONING_EFFORTS:
        raise ValidationError(
            f"reasoning_effort must be one of {REASONING_EFFORTS} (or unset), got {value!r}"
        )
    return normalized


def _resolve_effort(explicit: str | None, instance_default: str | None = None) -> str:
    """Effort precedence: per-call argument > provider instance > config key."""
    if explicit is not None:
        return normalize_reasoning_effort(explicit)
    if instance_default:
        return normalize_reasoning_effort(instance_default)
    try:
        return normalize_reasoning_effort(config.get("reasoning_effort"))
    except (ValidationError, ValueError) as exc:
        # A bad value in config.yaml must not fail every call, but it must not
        # vanish silently either: log what was ignored and use the provider
        # default. Anything else (I/O, programming errors) propagates. The
        # exception class is logged instead of its message because the record
        # sanitizer strips exception text (it can echo config/prompt content).
        logger.warning(
            "Ignoring invalid reasoning_effort config value (%s); using provider default",
            type(exc).__name__,
        )
        return ""


# ---------------------------------------------------------------------------
# Retry with backoff for rate-limited (429) responses
# ---------------------------------------------------------------------------
# The provider retries 429s a small fixed number of times; the agent layer
# must NOT retry 429s again (see machina.core.agent._is_rate_limit_error), or one
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


# ---------------------------------------------------------------------------
# Direct provider specs — one entry per model family with its own API.
# All of these expose an OpenAI-compatible ``/chat/completions`` endpoint
# (verified against vendor docs), so a single transport class serves them.
# ``reasoning`` controls how ``reasoning_effort`` is sent:
#   "openai" .. top-level ``reasoning_effort`` param (openai, deepseek, xai,
#               groq, together all document it)
#   "none" .... no knob; model choice expresses effort (mistral, google)
# ---------------------------------------------------------------------------
class ProviderSpec:
    """Static config for one direct (OpenAI-compatible) provider family."""

    def __init__(
        self,
        name: str,
        base_url: str,
        api_key_env: str,
        default_model: str,
        reasoning: str = "none",
        description: str = "",
        stream_usage: bool = False,
    ) -> None:
        self.name = name
        self.base_url = base_url
        self.api_key_env = api_key_env
        self.config_key = api_key_env.lower()
        self.default_model = default_model
        self.reasoning = reasoning
        self.description = description
        # Whether the vendor honors stream_options.include_usage. Only set
        # for vendors whose docs confirm it — others 400 on the param.
        self.stream_usage = stream_usage


PROVIDER_SPECS: dict[str, ProviderSpec] = {
    "openai": ProviderSpec(
        name="openai",
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
        default_model="gpt-4o-mini",
        reasoning="openai",
        description="OpenAI (GPT models)",
        stream_usage=True,
    ),
    "deepseek": ProviderSpec(
        name="deepseek",
        base_url="https://api.deepseek.com",
        api_key_env="DEEPSEEK_API_KEY",
        default_model="deepseek-chat",
        reasoning="openai",
        description="DeepSeek (chat + reasoner)",
        stream_usage=True,
    ),
    "xai": ProviderSpec(
        name="xai",
        base_url="https://api.x.ai/v1",
        api_key_env="XAI_API_KEY",
        default_model="grok-4",
        reasoning="openai",
        description="xAI (Grok models)",
    ),
    "groq": ProviderSpec(
        name="groq",
        base_url="https://api.groq.com/openai/v1",
        api_key_env="GROQ_API_KEY",
        default_model="llama-3.3-70b-versatile",
        reasoning="openai",
        description="Groq (fast inference)",
    ),
    "together": ProviderSpec(
        name="together",
        base_url="https://api.together.xyz/v1",
        api_key_env="TOGETHER_API_KEY",
        default_model="meta-llama/Llama-3.3-70B-Instruct-Turbo",
        reasoning="openai",
        description="Together AI (open models)",
    ),
    "mistral": ProviderSpec(
        name="mistral",
        base_url="https://api.mistral.ai/v1",
        api_key_env="MISTRAL_API_KEY",
        default_model="mistral-small-latest",
        reasoning="none",
        description="Mistral models",
    ),
    "google": ProviderSpec(
        name="google",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        api_key_env="GOOGLE_API_KEY",
        default_model="gemini-2.0-flash",
        reasoning="none",
        description="Google (Gemini models, OpenAI-compat endpoint)",
    ),
}

# Every chat provider the harness can route to: OpenRouter (multi-model),
# the direct families above, Anthropic (native API), and local Ollama.
PROVIDERS = ("openrouter", *tuple(PROVIDER_SPECS), "anthropic", "ollama")


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
    except Exception as _boundary_error:
        # Optional fallback preserves the primary outcome; report no payload.
        record_failure("provider._quota_exhausted", _boundary_error)
    return False


def _quota_reset_at(resp: httpx.Response) -> float | None:
    """Parse ``X-RateLimit-Reset`` (epoch milliseconds) into epoch seconds."""
    try:
        raw = resp.headers.get("x-ratelimit-reset", "")
        if raw and raw.strip().isdigit():
            value = int(raw.strip())
            # Heuristic: 13+ digits is milliseconds, fewer is already seconds.
            return value / 1000.0 if value > 10_000_000_000 else float(value)
    except Exception as _boundary_error:
        # Optional fallback preserves the primary outcome; report no payload.
        record_failure("provider._quota_reset_at", _boundary_error)
    return None


def _quota_message(resp: httpx.Response, model: str) -> str:
    """Human-readable exhaustion message including the reset time (local)."""
    limit = resp.headers.get("x-ratelimit-limit", "").strip() or "?"
    reset_at = _quota_reset_at(resp)
    when = ""
    if reset_at is not None:
        local = datetime.datetime.fromtimestamp(reset_at).astimezone()
        when = f" Quota resets {local.strftime('%Y-%m-%d %H:%M %Z')}."
    return (
        f"OpenRouter free-tier daily quota exhausted ({limit} requests/day, "
        f"account-wide).{when} Every free model returns 429 until then — "
        f"purchase credits for 1000/day, or switch to a paid model."
    )


def _retry_delay(attempt: int, resp: httpx.Response | None, free: bool) -> float:
    # Honor Retry-After when present: delta-seconds or an HTTP-date.
    if resp is not None:
        raw = ""
        try:
            raw = (resp.headers.get("retry-after") or "").strip()
        except Exception as _boundary_error:
            # Optional fallback preserves the primary outcome; report no payload.
            record_failure("provider._retry_delay", _boundary_error)
        if raw:
            if raw.isdigit():
                return min(float(raw), 60.0)
            try:
                from email.utils import parsedate_to_datetime

                when = parsedate_to_datetime(raw)
                if when is not None:
                    if when.tzinfo is None:
                        when = when.replace(tzinfo=datetime.UTC)
                    delta = (when - datetime.datetime.now(datetime.UTC)).total_seconds()
                    return min(max(delta, 0.0), 60.0)
            except (TypeError, ValueError, OverflowError) as _boundary_error:
                # Not a valid HTTP-date either — fall through to plain backoff.
                record_failure("provider._retry_delay", _boundary_error)
    import random

    base = _FREE_BASE_DELAY if free else _RETRY_BASE_DELAY
    delay = base * (2**attempt)
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


@asynccontextmanager
async def _stream_with_retry(
    client: httpx.AsyncClient,
    url: str,
    payload: dict[str, Any],
) -> AsyncGenerator[httpx.Response, None]:
    """Apply the POST retry policy before exposing any streaming content."""
    model = str(payload.get("model", ""))
    free = _is_free_model(model)
    max_retries = 0 if free else _MAX_RETRIES
    for attempt in range(max_retries + 1):
        async with client.stream("POST", url, json=payload) as resp:
            if resp.status_code != 429:
                yield resp
                return
            if free and _quota_exhausted(resp):
                raise RateLimitError(
                    _quota_message(resp, model),
                    reset_at=_quota_reset_at(resp),
                    daily_limit=int(resp.headers.get("x-ratelimit-limit") or 0) or None,
                    remaining=0,
                )
            if attempt == max_retries:
                resp.raise_for_status()
            delay = _retry_delay(attempt, resp, free)
        # Close the rejected response before waiting or opening another stream.
        logger.warning("429 rate limited (model=%s), retrying stream in %.1fs", model, delay)
        await asyncio.sleep(delay)


def _stream_error_message(chunk: dict[str, Any], model: str) -> str:
    """Human-readable message for an explicit mid-stream provider error frame.

    OpenAI-compatible gateways sometimes send HTTP 200 and then an
    ``{"error": ...}`` SSE frame instead of choices. Treating that as a benign
    keep-alive would report a truncated stream as a successful completion.
    """
    error = chunk.get("error")
    if isinstance(error, dict):
        detail = error.get("message") or error.get("type") or str(error)
    else:
        detail = str(error)
    return f"provider stream error for {model}: {detail}"


def _truncated_stream_error(provider: str, model: str) -> ProviderError:
    """Clean EOF without ``[DONE]`` or a ``finish_reason`` frame.

    A clean EOF is not a completion: reporting ``done`` for a truncated
    stream would persist partial content as a successful turn.
    """
    return ProviderError(
        f"{provider} stream ended without a terminal frame ([DONE] or finish_reason) for {model}"
    )


# ---------------------------------------------------------------------------
# Audit logging
# ---------------------------------------------------------------------------
_audit_logger = logging.getLogger("machina.audit")
_audit_logger.setLevel(logging.INFO)
if not _audit_logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s [AUDIT] %(message)s"))
    _audit_logger.addHandler(_handler)


def _sanitize_value(value: Any) -> Any:
    """Sanitize a value for audit logging — redact potential secrets."""
    if isinstance(value, bytes):
        try:
            return _sanitize_value(value.decode("utf-8")).encode("utf-8")
        except UnicodeDecodeError:
            return value
    if isinstance(value, str):
        sanitized = value
        sanitized = re.sub(r"sk-[a-zA-Z0-9]{20,}", "[REDACTED_KEY]", sanitized)
        sanitized = re.sub(r"sk-ant-[a-zA-Z0-9\-_]{20,}", "[REDACTED_KEY]", sanitized)
        sanitized = re.sub(r"sk-or-[a-zA-Z0-9\-_]{20,}", "[REDACTED_KEY]", sanitized)
        sanitized = re.sub(
            r"\b(ghp_[a-zA-Z0-9]{20,}|gho_[a-zA-Z0-9]{20,}|ghu_[a-zA-Z0-9]{20,}"
            r"|ghs_[a-zA-Z0-9]{20,}|github_pat_[a-zA-Z0-9_]{20,})",
            "[REDACTED_KEY]",
            sanitized,
        )
        sanitized = re.sub(r"\bxox[baprs]-[a-zA-Z0-9\-]{10,}", "[REDACTED_KEY]", sanitized)
        sanitized = re.sub(
            r"\beyJ[a-zA-Z0-9\-_]{10,}\.eyJ[a-zA-Z0-9\-_]{10,}\.[a-zA-Z0-9\-_]{10,}",
            "[REDACTED_KEY]",
            sanitized,
        )
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
    elif isinstance(value, set):
        return {_sanitize_value(v) for v in value}
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
    protected = {
        "prompt",
        "completion",
        "content",
        "query",
        "query_text",
        "search_text",
        "response",
        "result",
        "tool_args",
        "args",
        "payload",
        "messages",
        "task",
        "preview",
        "result_preview",
        "validation_reason",
        "error",
        "message",
    }

    def metadata(value):
        if isinstance(value, dict):
            return {
                key: metadata(item)
                for key, item in value.items()
                if key.lower() not in protected
                and not key.lower().endswith(("_content", "_preview", "_payload"))
            }
        if isinstance(value, (list, tuple)):
            return [metadata(item) for item in value]
        return value

    sanitized_kwargs = _sanitize_value(metadata(kwargs))
    entry = {
        "timestamp": time.time(),
        "event": event_type,
        **sanitized_kwargs,
    }
    _audit_logger.info(json.dumps(entry, default=str))
    from machina.observability.audit import audit_persistence

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


def _build_rate_limiter(calls_per_minute: int) -> AsyncTokenBucket:
    """Construct one bucket from a per-minute budget (single source of truth)."""
    if calls_per_minute < 0:
        raise ValidationError("rate_limit_calls_per_minute must be non-negative")
    return AsyncTokenBucket(rate=calls_per_minute / 60.0, capacity=calls_per_minute)


def _get_rate_limiter(provider_key: str = "default") -> AsyncTokenBucket:
    """Return the shared rate limiter for *provider_key*.

    One bucket per provider (process-wide singleton): per-instance limiters
    let N provider instances issue N× the configured rate.
    """
    limiter = _RATE_LIMITERS.get(provider_key)
    if limiter is None:
        limiter = _build_rate_limiter(int(config.get("rate_limit_calls_per_minute")))
        _RATE_LIMITERS[provider_key] = limiter
    return limiter


def reload_rate_limiters() -> None:
    """Rebuild every live rate-limit bucket from the current config.

    Buckets are created lazily and then cached for the process lifetime, so a
    runtime ``rate_limit_calls_per_minute`` change would otherwise never reach
    providers that already made a call. Only existing buckets are replaced —
    keys that never made a request are not materialized here.
    """
    calls_per_minute = int(config.get("rate_limit_calls_per_minute"))
    for key in list(_RATE_LIMITERS):
        _RATE_LIMITERS[key] = _build_rate_limiter(calls_per_minute)


# Process-wide limiter buckets, keyed by provider name (see _get_rate_limiter).
_RATE_LIMITERS: dict[str, AsyncTokenBucket] = {}

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
        )
    return copy.deepcopy(value)


def _cache_get(key: str) -> Any | None:
    """Return a copy of the cached response if present, else None."""
    value = _RESPONSE_CACHE.get(key)
    if value is None:
        return None
    return _copy_cached_response(value)


def _cache_put(key: str, value: Any) -> None:
    """Store an isolated copy of *value* in the TTL/size-bounded cache."""
    _RESPONSE_CACHE[key] = _copy_cached_response(value)


def _cache_clear() -> None:
    """Clear the response cache. Used by tests to isolate cache state."""
    _RESPONSE_CACHE.clear()


# ---------------------------------------------------------------------------
# Data classes — re-exported from machina.core.models for backward compatibility
# ---------------------------------------------------------------------------
# LLMResponse, StreamEvent, ToolDefinition are now defined in machina.core.models


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
# Prompt-cache breakpoints (E1 pilot — ``prompt_cache_enabled``, default OFF)
#
# The stable prefix (system prompt + tool schemas) is resent on every call.
# When enabled, the two ends that bound it get an Anthropic-style ephemeral
# ``cache_control`` marker: the final system content block and the last tool
# schema entry. AnthropicProvider sends breakpoints natively; OpenRouter
# passes them through only for models routed to Anthropic ("anthropic" in the
# model id) — Direct providers and Ollama are untouched.
#
# Expected saving is not quantified here: it is bounded by the serving
# model's minimum cacheable prompt length (~1k-4k tokens for current Claude
# models), by real-turn prefix stability, and by the cache-write premium
# until a prefix is read back within the TTL. It stays a pilot (default off)
# until that is measured on real turns.
# ---------------------------------------------------------------------------
def _prompt_cache_breakpoints_enabled() -> bool:
    """Read the pilot toggle live; invalid config degrades to disabled.

    A bad value must never take provider calls down: without breakpoints the
    request is still fully valid (same contract as ``reasoning_effort``).
    """
    try:
        return bool(config.get("prompt_cache_enabled"))
    except (ValidationError, ValueError) as exc:
        logger.warning(
            "Ignoring invalid prompt_cache_enabled config value (%s); "
            "sending requests without cache breakpoints",
            type(exc).__name__,
        )
        return False


def _ephemeral_cache_control() -> dict[str, str]:
    """Fresh Anthropic-style ephemeral breakpoint marker."""
    return {"type": "ephemeral"}


def _openrouter_anthropic_model(model: str | None) -> bool:
    """Whether an OpenRouter model id routes to Anthropic for cache_control."""
    return "anthropic" in (model or "").lower()


def _mark_openrouter_system_breakpoint(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]] | None:
    """Copy *messages* with an ephemeral breakpoint on the system tail.

    Returns None when there is no non-empty system message to mark, so the
    caller can leave the payload untouched. The caller's list and dicts are
    never mutated: breakpoints belong to the wire payload only, not to the
    caller's history.
    """
    last_system = -1
    for index, message in enumerate(messages):
        if message.get("role") == "system":
            last_system = index
    if last_system < 0:
        return None
    message = messages[last_system]
    content = message.get("content")
    if isinstance(content, str):
        if not content:
            return None
        marked: Any = [
            {"type": "text", "text": content, "cache_control": _ephemeral_cache_control()}
        ]
    elif isinstance(content, list) and content and isinstance(content[-1], dict):
        # Already block-form: mark a copy of the final block.
        marked = [
            *content[:-1],
            {**content[-1], "cache_control": _ephemeral_cache_control()},
        ]
    else:
        return None
    out = list(messages)
    out[last_system] = {**message, "content": marked}
    return out


def _mark_last_tool_breakpoint(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Copy *tools* with an ephemeral breakpoint on the final entry."""
    if not tools:
        return tools
    return [*tools[:-1], {**tools[-1], "cache_control": _ephemeral_cache_control()}]


def _openrouter_wire_messages(
    messages: list[dict[str, Any]], cache_breakpoints: bool
) -> tuple[list[dict[str, Any]], bool]:
    """Return ``(messages_for_wire, system_marked)`` for OpenRouter.

    With breakpoints enabled the system tail is marked in a copied list;
    the caller's history is never mutated. Disabled or unmarkable input is
    returned unchanged so the OFF path stays byte-identical.
    """
    if not cache_breakpoints:
        return messages, False
    marked = _mark_openrouter_system_breakpoint(messages)
    if marked is None:
        return messages, False
    return marked, True


def _log_prompt_cache_breakpoints(provider: str, model: str, *, system: bool, tools: bool) -> None:
    """One debug line per call when breakpoints attach (pilot visibility).

    The saving expectation is stated, not measured: benefit depends on the
    serving model's minimum cacheable prompt length and real-turn prefix
    stability (follow-up sizing), so no number is invented here.
    """
    if not (system or tools):
        return
    logger.debug(
        "prompt cache breakpoints attached provider=%s model=%s system=%s tools=%s; "
        "saving unmeasured (minimum cacheable prompt length and real-turn "
        "prefix stability apply)",
        provider,
        model,
        system,
        tools,
    )


# ---------------------------------------------------------------------------
# Base provider
# ---------------------------------------------------------------------------
class LLMProvider:
    """Base provider interface."""

    async def context_window(self, model: str | None = None) -> int:
        from machina.core.context_windows import context_window_for

        return context_window_for(model or getattr(self, "model", None), self)

    def reservation_max_tokens(self, max_tokens: int, reasoning_effort: str | None = None) -> int:
        """Maximum output requested on the wire, including reasoning tokens."""
        return max_tokens

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

    async def close(self) -> None:
        """Release provider resources.

        Base contract: a provider is always closable. Concrete transports
        override this to close their HTTP client; resource-free providers
        (and test doubles) inherit this no-op.
        """


# ---------------------------------------------------------------------------
# OpenRouter provider
# ---------------------------------------------------------------------------
class OpenRouterProvider(LLMProvider):
    """OpenRouter API — OpenAI-compatible, multi-model."""

    BASE_URL = "https://openrouter.ai/api/v1"

    async def context_window(self, model: str | None = None) -> int:
        from machina.core.context_windows import openrouter_context_window

        return await openrouter_context_window(self.client, model or self.model)

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "openrouter/free",
        reasoning_effort: str | None = None,
    ) -> None:
        self.api_key = api_key or config.get("openrouter_api_key") or ""
        if not self.api_key:
            raise ValidationError("OPENROUTER_API_KEY not set")
        self.model = model
        self._default_effort = normalize_reasoning_effort(reasoning_effort)
        self._rate_limiter = _get_rate_limiter("openrouter")
        self.client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/kiazh/machina",
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
        reasoning_effort: str | None = None,
        session_id: uuid.UUID | None = None,
        agent_id: str | None = None,
    ) -> LLMResponse:
        # Input validation
        _validate_messages(messages)
        _validate_params(temperature, max_tokens)

        # Rate limiting
        await self._rate_limiter.acquire()

        model = model or self.model
        cache_breakpoints = _prompt_cache_breakpoints_enabled() and _openrouter_anthropic_model(
            model
        )
        wire_messages, system_marked = _openrouter_wire_messages(messages, cache_breakpoints)
        payload: dict[str, Any] = {
            "model": model,
            "messages": wire_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        tools_marked = False
        if tools:
            tool_payload = _tools_payload(tools)
            if cache_breakpoints:
                tool_payload = _mark_last_tool_breakpoint(tool_payload)
                tools_marked = True
            payload["tools"] = tool_payload
        effort = _resolve_effort(reasoning_effort, self._default_effort)
        if effort:
            payload["reasoning"] = {"effort": effort}
        _log_prompt_cache_breakpoints("openrouter", model, system=system_marked, tools=tools_marked)

        # Check cache before making any HTTP request
        if _is_free_model(model):
            payload["max_tokens"] = min(max_tokens, 1024)
        ckey = _cache_key(
            messages,
            tools,
            f"openrouter:{model}",
            payload,
            _caller_identity(session_id, agent_id),
        )
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
            # max_tokens was already clamped before the cache key was built.
            for fb in _FREE_FALLBACK_MODELS:
                if fb != model and fb not in candidates:
                    candidates.append(fb)
        last_err: Exception | None = None
        data: dict[str, Any] | None = None
        used_model = model
        cacheable = True
        for cand in candidates:
            payload["model"] = cand
            try:
                resp = await _post_with_retry(self.client, "/chat/completions", payload)
                resp.raise_for_status()
                data = resp.json()
                used_model = cand
                if cand != model:
                    # A fallback candidate's answer must not later masquerade
                    # as the requested model (see N1 cache note below).
                    cacheable = False
                    logger.info("free-tier fallback succeeded with model=%s", cand)
                    audit_log(
                        "llm_free_fallback", provider="openrouter", from_model=model, to_model=cand
                    )
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
                        # The answer was produced without the tools the cache
                        # key covers, so it must not be cached under that key.
                        cacheable = False
                        audit_log("llm_free_fallback_no_tools", provider="openrouter", model=cand)
                        break
                    except Exception as e2:
                        last_err = e2
                        break
                if not retryable or cand == candidates[-1]:
                    break
                logger.warning(
                    "free model %s got %s, trying fallback %s after 2s",
                    cand,
                    status,
                    candidates[candidates.index(cand) + 1],
                )
                await asyncio.sleep(2.0)
                continue
            except RateLimitError as e:
                # Daily quota is exhausted account-wide: trying other free
                # models cannot succeed, so fail fast with the real reason.
                last_err = e
                break
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
        if cacheable:
            _cache_put(ckey, response)
        else:
            # N1: fallback-chain (or tools-stripped) answers are intentionally
            # not cached. A cache hit must never present another model's answer
            # under the requested model, and the next call should retry the
            # requested model in case its quota has recovered.
            logger.debug(
                "not caching fallback response for requested model=%s (used %s)",
                model,
                used_model,
            )
        return response

    async def stream_complete(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        tools: list[ToolDefinition] | None = None,
        reasoning_effort: str | None = None,
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
        cache_breakpoints = _prompt_cache_breakpoints_enabled() and _openrouter_anthropic_model(
            model
        )
        wire_messages, system_marked = _openrouter_wire_messages(messages, cache_breakpoints)
        payload: dict[str, Any] = {
            "model": model,
            "messages": wire_messages,
            "temperature": temperature,
            "max_tokens": payload_max,
            "stream": True,
        }
        # NOTE: openrouter/free does not advertise stream_options in its
        # capability list — only send include_usage for paid models.
        if not _is_free_model(model):
            # Ask for a final usage chunk so streamed turns report real token counts
            payload["stream_options"] = {"include_usage": True}
        tools_marked = False
        if tools:
            tool_payload = _tools_payload(tools)
            if cache_breakpoints:
                tool_payload = _mark_last_tool_breakpoint(tool_payload)
                tools_marked = True
            payload["tools"] = tool_payload
        effort = _resolve_effort(reasoning_effort, self._default_effort)
        if effort:
            payload["reasoning"] = {"effort": effort}
        _log_prompt_cache_breakpoints("openrouter", model, system=system_marked, tools=tools_marked)

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
        saw_terminal = False

        for cand in candidates:
            payload["model"] = cand
            resolved_model = cand
            content_parts = []
            tool_calls_by_index = {}
            stream_usage = {}
            saw_terminal = False
            try:
                async with _stream_with_retry(self.client, "/chat/completions", payload) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        data = line[6:]
                        if data == "[DONE]":
                            saw_terminal = True
                            break
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(chunk, dict):
                            continue
                        # An explicit error frame (HTTP 200 + SSE error body)
                        # must fail the stream, not silently truncate it.
                        if chunk.get("error"):
                            raise ProviderError(_stream_error_message(chunk, resolved_model))
                        resolved_model = chunk.get("model") or resolved_model
                        # The usage chunk arrives with an empty choices list.
                        if chunk.get("usage"):
                            stream_usage = chunk["usage"]
                        choices = chunk.get("choices", [])
                        if not choices:
                            continue
                        if choices[0].get("finish_reason"):
                            saw_terminal = True
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
                    if not saw_terminal:
                        raise _truncated_stream_error("openrouter", resolved_model)
                # Success — stop trying fallbacks.
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
                    logger.warning(
                        "free stream model %s got %s, trying fallback after 2s", cand, status
                    )
                    # Discard any partial state so a throttled candidate cannot
                    # contaminate the next one.
                    content_parts = []
                    tool_calls_by_index = {}
                    stream_usage = {}
                    await asyncio.sleep(2.0)
                    continue
                if (
                    status == 404
                    and nothing_yielded
                    and payload.get("tools")
                    and cand == candidates[-1]
                ):
                    # Same "no provider can serve tools" case as non-stream:
                    # retry the last candidate once as plain chat.
                    logger.warning(
                        "free stream model %s 404 with tools, retrying without tools", cand
                    )
                    payload.pop("tools", None)
                    content_parts = []
                    tool_calls_by_index = {}
                    stream_usage = {}
                    saw_terminal = False
                    try:
                        async with _stream_with_retry(
                            self.client, "/chat/completions", payload
                        ) as resp2:
                            resp2.raise_for_status()
                            async for line in resp2.aiter_lines():
                                if not line.startswith("data: "):
                                    continue
                                d2 = line[6:]
                                if d2 == "[DONE]":
                                    saw_terminal = True
                                    break
                                try:
                                    ch2 = json.loads(d2)
                                except json.JSONDecodeError:
                                    continue
                                if isinstance(ch2, dict) and ch2.get("error"):
                                    raise ProviderError(_stream_error_message(ch2, resolved_model))
                                resolved_model = ch2.get("model") or resolved_model
                                if ch2.get("usage"):
                                    stream_usage = ch2["usage"]
                                chs = ch2.get("choices", [])
                                if not chs:
                                    continue
                                if chs[0].get("finish_reason"):
                                    saw_terminal = True
                                dl2 = chs[0].get("delta", {})
                                c2 = dl2.get("content", "")
                                if c2:
                                    content_parts.append(c2)
                                    yield StreamEvent(type="text", content=c2)
                            if not saw_terminal:
                                raise _truncated_stream_error("openrouter", resolved_model)
                        break
                    except Exception as e2:
                        e = e2
                if reservation_id is not None:
                    await usage_store.finish(reservation_id, failed=True)
                audit_log("llm_stream_error", provider="openrouter", model=cand, error=str(e))
                raise e

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

    async def close(self) -> None:
        # Close the shared client (it will be recreated if needed)
        await self.client.aclose()


# ---------------------------------------------------------------------------
# Ollama provider
# ---------------------------------------------------------------------------
class OllamaProvider(LLMProvider):
    """Local Ollama provider — free, self-hosted."""

    def __init__(
        self,
        model: str = "llama3.1",
        base_url: str = "http://localhost:11434",
        reasoning_effort: str | None = None,
    ) -> None:
        self.model = model
        self.base_url = base_url
        self._default_effort = normalize_reasoning_effort(reasoning_effort)
        self._rate_limiter = _get_rate_limiter("ollama")
        self.client = httpx.AsyncClient(base_url=base_url, timeout=120.0)

    @staticmethod
    def _think_flag(effort: str) -> bool | None:
        """Map effort to Ollama's boolean ``think`` flag (None = omit, model default)."""
        if not effort:
            return None
        return effort != "low"

    async def complete(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        tools: list[ToolDefinition] | None = None,
        reasoning_effort: str | None = None,
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
            "messages": ollama_messages(messages),
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
        }
        if tools:
            payload["tools"] = _tools_payload(tools)

        think = self._think_flag(_resolve_effort(reasoning_effort, self._default_effort))
        if think is not None:
            payload["think"] = think

        # Check cache before making any HTTP request
        ckey = _cache_key(
            messages,
            tools,
            f"ollama:{model or self.model}",
            payload,
            _caller_identity(session_id, agent_id),
        )
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
        if isinstance(data, dict) and "error" in data:
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
        # A 200 body without a message (e.g. {"done": true}) must fail like the
        # other providers instead of a bare KeyError that strands the usage
        # reservation and bypasses metrics/audit.
        message = data.get("message") if isinstance(data, dict) else None
        if not isinstance(message, dict):
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
                error="no_message",
            )
            detail = data if isinstance(data, dict) else "empty response"
            raise ProviderError(f"Ollama returned no message for {model or self.model}: {detail}")

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
        reasoning_effort: str | None = None,
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
            "messages": ollama_messages(messages),
            "stream": True,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
        }
        if tools:
            payload["tools"] = _tools_payload(tools)
        think = self._think_flag(_resolve_effort(reasoning_effort, self._default_effort))
        if think is not None:
            payload["think"] = think

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
            async with _stream_with_retry(self.client, "/api/chat", payload) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        # Ollama streams NDJSON; stray keep-alive lines are
                        # benign and must not fail the stream.
                        continue
                    if not isinstance(data, dict):
                        continue
                    if data.get("error"):
                        # Explicit error frame: fail honestly instead of
                        # reporting a truncated stream as success.
                        raise ProviderError(
                            f"Ollama stream error for {model or self.model}: {data['error']}"
                        )

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

    async def close(self) -> None:
        # Close the shared client (it will be recreated if needed)
        await self.client.aclose()


# ---------------------------------------------------------------------------
# Direct provider — one transport for every OpenAI-compatible family
# (OpenAI, DeepSeek, xAI, Groq, Together, Mistral, Google). Driven by a
# ProviderSpec so base URL, key, default model and effort mapping stay
# accurate in one table instead of seven copy-pasted classes.
# ---------------------------------------------------------------------------
class DirectProvider(LLMProvider):
    """OpenAI-compatible chat API for a single vendor (see PROVIDER_SPECS)."""

    def __init__(
        self,
        spec: ProviderSpec,
        api_key: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        self.spec = spec
        self.api_key = api_key or config.get(spec.config_key) or ""
        if not self.api_key:
            raise ValidationError(f"{spec.api_key_env} not set")
        self.model = model or spec.default_model
        self._default_effort = normalize_reasoning_effort(reasoning_effort)
        self._rate_limiter = _get_rate_limiter(spec.name)
        self.client = httpx.AsyncClient(
            base_url=spec.base_url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout=120.0,
        )

    def _effort_payload(self, reasoning_effort: str | None) -> dict[str, Any]:
        """Extra body params for the requested effort ({} when unset/unsupported)."""
        effort = _resolve_effort(reasoning_effort, self._default_effort)
        if effort and self.spec.reasoning == "openai":
            return {"reasoning_effort": effort}
        return {}

    def _cache_key_for(self, messages, tools, model: str, request=None, identity=None) -> str:
        # Namespace by provider so identical model ids on two vendors can't collide.
        return _cache_key(messages, tools, f"{self.spec.name}:{model}", request, identity)

    async def complete(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        tools: list[ToolDefinition] | None = None,
        reasoning_effort: str | None = None,
        session_id: uuid.UUID | None = None,
        agent_id: str | None = None,
    ) -> LLMResponse:
        _validate_messages(messages)
        _validate_params(temperature, max_tokens)
        await self._rate_limiter.acquire()

        model = model or self.model
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            **self._effort_payload(reasoning_effort),
        }
        if tools:
            payload["tools"] = _tools_payload(tools)

        ckey = self._cache_key_for(
            messages, tools, model, payload, _caller_identity(session_id, agent_id)
        )
        cached = _cache_get(ckey)
        if cached is not None:
            logger.debug("cache hit for %s model=%s", self.spec.name, model)
            return cached

        audit_log(
            "llm_call_start", provider=self.spec.name, model=model, message_count=len(messages)
        )

        reservation_id = None
        if session_id is not None and agent_id is not None:
            reservation_id = await usage_store.reserve(
                session_id, agent_id, self.spec.name, model, messages, tools or [], max_tokens
            )

        start = time.monotonic()
        try:
            resp = await _post_with_retry(self.client, "/chat/completions", payload)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            duration_ms = (time.monotonic() - start) * 1000
            metrics.record_llm_call(
                session_id="", model=model, duration_ms=duration_ms, is_error=True
            )
            if reservation_id is not None:
                await usage_store.finish(reservation_id, failed=True)
            audit_log("llm_call_error", provider=self.spec.name, model=model, error=str(e))
            raise

        duration_ms = (time.monotonic() - start) * 1000
        choices = data.get("choices")
        if not choices or not isinstance(choices, list):
            error = data.get("error") or {}
            detail = error.get("message") if isinstance(error, dict) else str(error)
            message = (
                f"provider returned no choices for {model}: {detail or data or 'empty response'}"
            )
            metrics.record_llm_call(
                session_id="", model=model, duration_ms=duration_ms, is_error=True
            )
            if reservation_id is not None:
                await usage_store.finish(reservation_id, failed=True)
            audit_log("llm_call_error", provider=self.spec.name, model=model, error="no_choices")
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
        if reservation_id is not None:
            await usage_store.finish(reservation_id, usage, model=data.get("model", model))
        audit_log(
            "llm_call_complete",
            provider=self.spec.name,
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
        reasoning_effort: str | None = None,
        session_id: uuid.UUID | None = None,
        agent_id: str | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Stream completion via SSE — yields text deltas as they arrive."""
        _validate_messages(messages)
        _validate_params(temperature, max_tokens)
        await self._rate_limiter.acquire()

        model = model or self.model
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
            **self._effort_payload(reasoning_effort),
        }
        # stream_options.include_usage is only sent for vendors whose docs
        # confirm it — others 400 on the param.
        if self.spec.stream_usage:
            payload["stream_options"] = {"include_usage": True}
        if tools:
            payload["tools"] = _tools_payload(tools)

        audit_log(
            "llm_stream_start", provider=self.spec.name, model=model, message_count=len(messages)
        )

        reservation_id = None
        if session_id is not None and agent_id is not None:
            reservation_id = await usage_store.reserve(
                session_id, agent_id, self.spec.name, model, messages, tools or [], max_tokens
            )

        content_parts: list[str] = []
        tool_calls_by_index: dict[int, dict[str, str]] = {}
        stream_usage: dict[str, int] = {}
        resolved_model = model
        saw_terminal = False
        try:
            async with _stream_with_retry(self.client, "/chat/completions", payload) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data == "[DONE]":
                        saw_terminal = True
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(chunk, dict):
                        continue
                    if chunk.get("error"):
                        # Explicit error frame: fail the stream honestly
                        # instead of reporting a truncated success.
                        raise ProviderError(_stream_error_message(chunk, resolved_model))
                    resolved_model = chunk.get("model") or resolved_model
                    # The usage chunk arrives with an empty choices list.
                    if chunk.get("usage"):
                        stream_usage = chunk["usage"]
                    choices = chunk.get("choices", [])
                    if not choices:
                        continue
                    if choices[0].get("finish_reason"):
                        saw_terminal = True
                    delta = choices[0].get("delta", {})
                    content = delta.get("content", "")
                    if content:
                        content_parts.append(content)
                        yield StreamEvent(type="text", content=content)
                    for tc_delta in delta.get("tool_calls", []):
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
                                frag = json.dumps(frag)
                            tc["arguments"] += frag
            if not saw_terminal:
                raise _truncated_stream_error(self.spec.name, resolved_model)
        except Exception as e:
            if reservation_id is not None:
                await usage_store.finish(reservation_id, failed=True)
            audit_log("llm_stream_error", provider=self.spec.name, model=model, error=str(e))
            raise

        tool_calls: list[dict] = []
        for idx in sorted(tool_calls_by_index.keys()):
            tc = tool_calls_by_index[idx]
            tool_calls.append(
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {"name": tc["name"], "arguments": tc["arguments"]},
                }
            )
        full_content = "".join(content_parts)
        usage = {
            "prompt_tokens": int(stream_usage.get("prompt_tokens", 0)),
            "completion_tokens": int(stream_usage.get("completion_tokens", 0)),
        }
        if not usage["prompt_tokens"] and not usage["completion_tokens"]:
            # Estimates support turn accounting and display, but must never
            # release the conservative durable reservation as vendor usage.
            usage = {
                "prompt_tokens": _estimate_tokens(messages, tools or [], 0),
                "completion_tokens": _estimate_tokens(
                    [{"role": "assistant", "content": full_content, "tool_calls": tool_calls}],
                    [],
                    0,
                ),
                "estimated": True,
            }
        usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
        if reservation_id is not None:
            await usage_store.finish(reservation_id, usage, model=resolved_model)
        audit_log(
            "llm_stream_complete", provider=self.spec.name, model=model, tool_calls=len(tool_calls)
        )
        yield StreamEvent(
            type="done",
            response=LLMResponse(
                content=full_content, model=resolved_model, usage=usage, tool_calls=tool_calls
            ),
        )

    async def close(self) -> None:
        await self.client.aclose()


# ---------------------------------------------------------------------------
# Anthropic provider — native Messages API (Anthropic has no OpenAI-compatible
# endpoint). Requests/responses are normalized to the harness's OpenAI-style
# shapes so the agent loop works unchanged.
# ---------------------------------------------------------------------------
class AnthropicProvider(LLMProvider):
    """Anthropic Messages API — https://api.anthropic.com/v1/messages."""

    BASE_URL = "https://api.anthropic.com"
    API_VERSION = "2023-06-01"
    DEFAULT_MODEL = "claude-3-5-sonnet-20241022"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        self.api_key = api_key or config.get("anthropic_api_key") or ""
        if not self.api_key:
            raise ValidationError("ANTHROPIC_API_KEY not set")
        self.model = model or self.DEFAULT_MODEL
        self._default_effort = normalize_reasoning_effort(reasoning_effort)
        self._rate_limiter = _get_rate_limiter("anthropic")
        self.client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": self.API_VERSION,
                "Content-Type": "application/json",
            },
            timeout=120.0,
        )

    @staticmethod
    def _to_anthropic_messages(
        messages: list[dict[str, Any]],
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """Split OpenAI-style history into (system, anthropic messages).

        Handles the harness's shapes: plain role/content dicts, assistant
        messages carrying OpenAI ``tool_calls``, and ``role: tool`` results.
        """
        system_parts: list[str] = []
        out: list[dict[str, Any]] = []
        for msg in messages:
            role = msg.get("role", "user")
            if role == "system":
                system_parts.append(str(msg.get("content", "")))
                continue
            if role == "tool":
                out.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": msg.get("tool_call_id", ""),
                                "content": str(msg.get("content", "")),
                            }
                        ],
                    }
                )
                continue
            tool_calls = msg.get("tool_calls")
            if role == "assistant" and tool_calls:
                blocks: list[dict[str, Any]] = []
                if msg.get("content"):
                    blocks.append({"type": "text", "text": str(msg.get("content"))})
                for tc in tool_calls:
                    fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
                    args = fn.get("arguments", "{}")
                    try:
                        parsed = json.loads(args) if isinstance(args, str) else args
                    except (json.JSONDecodeError, TypeError):
                        parsed = {}
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": tc.get("id", "") if isinstance(tc, dict) else "",
                            "name": fn.get("name", ""),
                            "input": parsed if isinstance(parsed, dict) else {},
                        }
                    )
                out.append({"role": "assistant", "content": blocks})
                continue
            out.append({"role": role, "content": anthropic_content(msg.get("content", ""))})
        return ("\n\n".join(system_parts) if system_parts else None, out)

    @staticmethod
    def _from_response_blocks(content: list[dict[str, Any]]) -> tuple[str, list[dict]]:
        """Normalize Anthropic content blocks to (text, OpenAI-style tool_calls)."""
        texts: list[str] = []
        tool_calls: list[dict] = []
        for block in content or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and block.get("text"):
                texts.append(block["text"])
            elif block.get("type") == "tool_use":
                tool_calls.append(
                    {
                        "id": block.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": block.get("name", ""),
                            "arguments": json.dumps(block.get("input", {})),
                        },
                    }
                )
        return "".join(texts), tool_calls

    def _thinking_payload(
        self, reasoning_effort: str | None, max_tokens: int, temperature: float
    ) -> tuple[dict[str, Any], int, float]:
        """Return (extra body, effective max_tokens, effective temperature)."""
        effort = _resolve_effort(reasoning_effort, self._default_effort)
        if not effort:
            return {}, max_tokens, temperature
        budget = ANTHROPIC_THINKING_BUDGETS[effort]
        # Thinking requires max_tokens above the budget and temperature 1.
        return (
            {"thinking": {"type": "enabled", "budget_tokens": budget}},
            max(max_tokens, budget + 1024),
            1.0,
        )

    def _payload(
        self,
        messages: list[dict[str, Any]],
        model: str,
        temperature: float,
        max_tokens: int,
        tools: list[ToolDefinition] | None,
        reasoning_effort: str | None,
        stream: bool,
    ) -> dict[str, Any]:
        system, anthropic_messages = self._to_anthropic_messages(messages)
        cache_breakpoints = _prompt_cache_breakpoints_enabled()
        thinking, max_tokens, temperature = self._thinking_payload(
            reasoning_effort, max_tokens, temperature
        )
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": anthropic_messages,
            **thinking,
        }
        if system:
            if cache_breakpoints:
                # Block-list form is required to carry cache_control; only
                # used when the pilot flag is on.
                payload["system"] = [
                    {
                        "type": "text",
                        "text": system,
                        "cache_control": _ephemeral_cache_control(),
                    }
                ]
            else:
                payload["system"] = system
        if temperature != 1.0:
            payload["temperature"] = temperature
        tools_marked = False
        if tools:
            tool_defs = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters}
                for t in tools
            ]
            if cache_breakpoints:
                tool_defs = _mark_last_tool_breakpoint(tool_defs)
                tools_marked = True
            payload["tools"] = tool_defs
        if stream:
            payload["stream"] = True
        _log_prompt_cache_breakpoints(
            "anthropic",
            model,
            system=cache_breakpoints and bool(system),
            tools=tools_marked,
        )
        return payload

    def reservation_max_tokens(self, max_tokens: int, reasoning_effort: str | None = None) -> int:
        return self._thinking_payload(reasoning_effort, max_tokens, 0.7)[1]

    async def complete(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        tools: list[ToolDefinition] | None = None,
        reasoning_effort: str | None = None,
        session_id: uuid.UUID | None = None,
        agent_id: str | None = None,
    ) -> LLMResponse:
        _validate_messages(messages)
        _validate_params(temperature, max_tokens)
        await self._rate_limiter.acquire()

        model = model or self.model
        payload = self._payload(
            messages, model, temperature, max_tokens, tools, reasoning_effort, False
        )

        ckey = _cache_key(
            messages,
            tools,
            f"anthropic:{model}",
            payload,
            _caller_identity(session_id, agent_id),
        )
        cached = _cache_get(ckey)
        if cached is not None:
            logger.debug("cache hit for anthropic model=%s", model)
            return cached

        audit_log("llm_call_start", provider="anthropic", model=model, message_count=len(messages))

        reservation_id = None
        if session_id is not None and agent_id is not None:
            reservation_id = await usage_store.reserve(
                session_id,
                agent_id,
                "anthropic",
                model,
                messages,
                tools or [],
                payload["max_tokens"],
            )

        start = time.monotonic()
        try:
            resp = await _post_with_retry(self.client, "/v1/messages", payload)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            duration_ms = (time.monotonic() - start) * 1000
            metrics.record_llm_call(
                session_id="", model=model, duration_ms=duration_ms, is_error=True
            )
            if reservation_id is not None:
                await usage_store.finish(reservation_id, failed=True)
            audit_log("llm_call_error", provider="anthropic", model=model, error=str(e))
            raise

        duration_ms = (time.monotonic() - start) * 1000
        if data.get("type") == "error" or "content" not in data:
            error = data.get("error") or {}
            detail = error.get("message") if isinstance(error, dict) else str(error)
            metrics.record_llm_call(
                session_id="", model=model, duration_ms=duration_ms, is_error=True
            )
            if reservation_id is not None:
                await usage_store.finish(reservation_id, failed=True)
            audit_log("llm_call_error", provider="anthropic", model=model, error="no_content")
            raise ProviderError(f"Anthropic returned no content for {model}: {detail or data}")

        content, tool_calls = self._from_response_blocks(data.get("content", []))
        usage_in = data.get("usage", {})
        usage = {
            "prompt_tokens": usage_in.get("input_tokens", 0),
            "completion_tokens": usage_in.get("output_tokens", 0),
            "total_tokens": usage_in.get("input_tokens", 0) + usage_in.get("output_tokens", 0),
        }
        metrics.record_llm_call(
            session_id="",
            model=data.get("model", model),
            duration_ms=duration_ms,
            prompt_tokens=usage["prompt_tokens"],
            completion_tokens=usage["completion_tokens"],
        )
        if reservation_id is not None:
            await usage_store.finish(reservation_id, usage, model=data.get("model", model))
        audit_log(
            "llm_call_complete",
            provider="anthropic",
            model=data.get("model", model),
            prompt_tokens=usage["prompt_tokens"],
            completion_tokens=usage["completion_tokens"],
            total_tokens=usage["total_tokens"],
            tool_calls=len(tool_calls),
        )
        response = LLMResponse(
            content=content,
            model=data.get("model", model),
            usage=usage,
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
        reasoning_effort: str | None = None,
        session_id: uuid.UUID | None = None,
        agent_id: str | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Stream via Anthropic SSE — yields text deltas as they arrive."""
        _validate_messages(messages)
        _validate_params(temperature, max_tokens)
        await self._rate_limiter.acquire()

        model = model or self.model
        payload = self._payload(
            messages, model, temperature, max_tokens, tools, reasoning_effort, True
        )

        audit_log(
            "llm_stream_start", provider="anthropic", model=model, message_count=len(messages)
        )

        reservation_id = None
        if session_id is not None and agent_id is not None:
            reservation_id = await usage_store.reserve(
                session_id,
                agent_id,
                "anthropic",
                model,
                messages,
                tools or [],
                payload["max_tokens"],
            )

        text_parts: list[str] = []
        blocks: dict[int, dict[str, Any]] = {}
        input_tokens = 0
        output_tokens = 0
        resolved_model = model
        try:
            async with _stream_with_retry(self.client, "/v1/messages", payload) as resp:
                resp.raise_for_status()
                event_type = ""
                async for line in resp.aiter_lines():
                    if line.startswith("event: "):
                        event_type = line[7:].strip()
                        continue
                    if not line.startswith("data: "):
                        continue
                    try:
                        data = json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(data, dict):
                        continue
                    if event_type == "error" or data.get("type") == "error":
                        # Anthropic can fail mid-stream via an explicit error
                        # event; swallowing it would truncate the answer.
                        error = data.get("error") or {}
                        detail = error.get("message") if isinstance(error, dict) else str(error)
                        raise ProviderError(
                            f"Anthropic stream error for {resolved_model}: {detail or data}"
                        )
                    if event_type == "message_start":
                        msg = data.get("message", {})
                        resolved_model = msg.get("model") or resolved_model
                        input_tokens = msg.get("usage", {}).get("input_tokens", input_tokens)
                    elif event_type == "content_block_start":
                        idx = data.get("index", 0)
                        block = data.get("content_block", {})
                        if block.get("type") == "tool_use":
                            blocks[idx] = {
                                "id": block.get("id", ""),
                                "name": block.get("name", ""),
                                "arguments": "",
                            }
                    elif event_type == "content_block_delta":
                        idx = data.get("index", 0)
                        delta = data.get("delta", {})
                        if delta.get("type") == "text_delta" and delta.get("text"):
                            text_parts.append(delta["text"])
                            yield StreamEvent(type="text", content=delta["text"])
                        elif delta.get("type") == "input_json_delta" and delta.get("partial_json"):
                            blocks.setdefault(idx, {"id": "", "name": "", "arguments": ""})[
                                "arguments"
                            ] += delta["partial_json"]
                    elif event_type == "message_delta":
                        output_tokens = data.get("usage", {}).get("output_tokens", output_tokens)
        except Exception as e:
            if reservation_id is not None:
                await usage_store.finish(reservation_id, failed=True)
            audit_log("llm_stream_error", provider="anthropic", model=model, error=str(e))
            raise

        tool_calls = [
            {
                "id": b["id"],
                "type": "function",
                "function": {"name": b["name"], "arguments": b["arguments"]},
            }
            for _, b in sorted(blocks.items())
            if b.get("name") or b.get("arguments")
        ]
        usage = {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        }
        if reservation_id is not None:
            await usage_store.finish(reservation_id, usage, model=resolved_model)
        audit_log(
            "llm_stream_complete", provider="anthropic", model=model, tool_calls=len(tool_calls)
        )
        yield StreamEvent(
            type="done",
            response=LLMResponse(
                content="".join(text_parts),
                model=resolved_model,
                usage=usage,
                tool_calls=tool_calls,
            ),
        )

    async def close(self) -> None:
        await self.client.aclose()


def get_provider(
    provider: str = "openrouter",
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> LLMProvider:
    """Factory: return the configured provider.

    ``reasoning_effort`` (low|medium|high) optionally pins the reasoning depth;
    otherwise each provider falls back to the ``reasoning_effort`` config key.
    """
    if provider == "openrouter":
        return OpenRouterProvider(
            model=model or "openrouter/free", reasoning_effort=reasoning_effort
        )
    elif provider == "anthropic":
        return AnthropicProvider(
            model=model or AnthropicProvider.DEFAULT_MODEL, reasoning_effort=reasoning_effort
        )
    elif provider == "ollama":
        return OllamaProvider(model=model or "llama3.1", reasoning_effort=reasoning_effort)
    spec = PROVIDER_SPECS.get(provider)
    if spec is not None:
        return DirectProvider(
            spec, model=model or spec.default_model, reasoning_effort=reasoning_effort
        )
    raise ValidationError(f"Unknown provider: {provider}. Choose one of: {', '.join(PROVIDERS)}")
