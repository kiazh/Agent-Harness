"""Async token-bucket rate limiting (one bucket per provider, process-wide)."""

from __future__ import annotations

import asyncio
import time

from machina.core.config import config
from machina.core.exceptions import ValidationError


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
