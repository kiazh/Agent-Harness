"""Shared transport plumbing for every provider: retry/quota policy, SSE
helpers, and wire-payload shaping. Stateless by design."""

from __future__ import annotations

import asyncio
import datetime
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import httpx

from machina.core.config import MAX_TOKENS, config
from machina.core.exceptions import ProviderError, RateLimitError, ValidationError
from machina.core.models import ToolDefinition
from machina.core.provider._log import logger
from machina.observability.diagnostics import record_failure

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
    """Validate LLM call parameters against the shared output ceiling."""
    if not (0.0 <= temperature <= 2.0):
        raise ValidationError(f"temperature must be between 0.0 and 2.0, got {temperature}")
    if not (1 <= max_tokens <= MAX_TOKENS):
        raise ValidationError(f"max_tokens must be between 1 and {MAX_TOKENS}, got {max_tokens}")


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
