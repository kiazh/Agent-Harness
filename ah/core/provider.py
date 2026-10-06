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
    "PROVIDERS",
    "PROVIDER_SPECS",
    "ProviderSpec",
    "REASONING_EFFORTS",
    "normalize_reasoning_effort",
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
    except ValidationError:
        # A bad value in config.yaml should surface loudly at call time, not
        # be silently swallowed — re-raise.
        raise
    except Exception:
        return ""

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
    ) -> None:
        self.name = name
        self.base_url = base_url
        self.api_key_env = api_key_env
        self.config_key = api_key_env.lower()
        self.default_model = default_model
        self.reasoning = reasoning
        self.description = description


PROVIDER_SPECS: dict[str, ProviderSpec] = {
    "openai": ProviderSpec(
        name="openai",
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
        default_model="gpt-4o-mini",
        reasoning="openai",
        description="OpenAI (GPT models)",
    ),
    "deepseek": ProviderSpec(
        name="deepseek",
        base_url="https://api.deepseek.com",
        api_key_env="DEEPSEEK_API_KEY",
        default_model="deepseek-chat",
        reasoning="openai",
        description="DeepSeek (chat + reasoner)",
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
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = _tools_payload(tools)
        effort = _resolve_effort(reasoning_effort, self._default_effort)
        if effort:
            payload["reasoning"] = {"effort": effort}

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
        effort = _resolve_effort(reasoning_effort, self._default_effort)
        if effort:
            payload["reasoning"] = {"effort": effort}

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
            "messages": messages,
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
            "messages": messages,
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

    def _cache_key_for(self, messages, tools, model: str) -> str:
        # Namespace by provider so identical model ids on two vendors can't collide.
        return _cache_key(messages, tools, f"{self.spec.name}:{model}")

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

        ckey = self._cache_key_for(messages, tools, model)
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
        # NOTE: no stream_options here — unlike OpenAI proper, several vendors
        # 400 on it. Streamed turns report estimated usage via the reservation,
        # like Ollama streams.
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
        resolved_model = model
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
                    choices = chunk.get("choices", [])
                    if not choices:
                        continue
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
        if reservation_id is not None:
            await usage_store.finish(reservation_id, {}, model=resolved_model)
        audit_log(
            "llm_stream_complete", provider=self.spec.name, model=model, tool_calls=len(tool_calls)
        )
        yield StreamEvent(
            type="done",
            response=LLMResponse(
                content=full_content, model=resolved_model, usage={}, tool_calls=tool_calls
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
            out.append({"role": role, "content": str(msg.get("content", ""))})
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
            payload["system"] = system
        if temperature != 1.0:
            payload["temperature"] = temperature
        if tools:
            payload["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters}
                for t in tools
            ]
        if stream:
            payload["stream"] = True
        return payload

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

        ckey = _cache_key(messages, tools, f"anthropic:{model}")
        cached = _cache_get(ckey)
        if cached is not None:
            logger.debug("cache hit for anthropic model=%s", model)
            return cached

        audit_log("llm_call_start", provider="anthropic", model=model, message_count=len(messages))

        reservation_id = None
        if session_id is not None and agent_id is not None:
            reservation_id = await usage_store.reserve(
                session_id, agent_id, "anthropic", model, messages, tools or [], max_tokens
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
                session_id, agent_id, "anthropic", model, messages, tools or [], max_tokens
            )

        text_parts: list[str] = []
        blocks: dict[int, dict[str, Any]] = {}
        input_tokens = 0
        output_tokens = 0
        resolved_model = model
        try:
            async with self.client.stream("POST", "/v1/messages", json=payload) as resp:
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
