"""Regression tests for free-tier 429 / daily-quota handling in the provider.

Context: OpenRouter's free tier allows a small number of requests per day
*account-wide* (not per model). When that quota is gone every free model
returns 429 with ``X-RateLimit-Remaining: 0``. Rotating through the fallback
model chain cannot help and only wastes requests and wall-clock time, so the
provider must stop at the first such response and surface a clear error.

These tests use a fake httpx client — no network access.
"""

from __future__ import annotations

import copy

import pytest

from ah.core.exceptions import RateLimitError
from ah.core.provider import (
    _FREE_FALLBACK_MODELS,
    OpenRouterProvider,
    _is_free_model,
    _quota_exhausted,
    _quota_reset_at,
)

# The exact headers OpenRouter returns once the daily free quota is spent.
EXHAUSTED_HEADERS = {
    "X-RateLimit-Limit": "50",
    "X-RateLimit-Remaining": "0",
    "X-RateLimit-Reset": "1791244800000",  # epoch ms -> 2026-10-06T00:00:00Z
}


class _Headers(dict):
    """Case-insensitive header mapping, matching ``httpx.Headers`` semantics."""

    def __init__(self, values: dict[str, str] | None = None) -> None:
        super().__init__({k.lower(): v for k, v in (values or {}).items()})

    def get(self, key, default=None):  # type: ignore[override]
        return super().get(key.lower(), default)


class _FakeResponse:
    """Minimal httpx.Response stand-in."""

    def __init__(self, status_code: int, headers: dict[str, str] | None = None,
                 payload: dict | None = None, text: str = "") -> None:
        self.status_code = status_code
        self.headers = _Headers(headers)
        self._payload = payload or {}
        self.text = text
        self.request = None

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError(
                f"{self.status_code}", request=self.request, response=self
            )


class _FakeClient:
    """Records every POST and replays a queued response per call.

    The recorded payload is deep-copied because the provider mutates
    ``payload["model"]`` in place while walking the fallback chain.
    """

    def __init__(self, responses: list[_FakeResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def post(self, url, json=None, **kwargs):  # noqa: A002 - httpx kwarg
        self.calls.append({"url": url, "payload": copy.deepcopy(json)})
        if not self.responses:
            raise AssertionError("fake client ran out of queued responses")
        return self.responses.pop(0)


def _provider(client: _FakeClient, model: str = "openrouter/free") -> OpenRouterProvider:
    p = OpenRouterProvider(api_key="test-key", model=model)
    p.client = client  # replace the real AsyncClient
    return p


# ─── unit helpers ───────────────────────────────────────────────────────────


def test_is_free_model_detects_all_free_forms():
    assert _is_free_model("openrouter/free")
    assert _is_free_model("qwen/qwen3.8-27b:free")
    assert _is_free_model("some/model/free")
    assert not _is_free_model("anthropic/claude-3.5-sonnet")
    assert not _is_free_model("")


def test_quota_exhausted_reads_remaining_header():
    assert _quota_exhausted(_FakeResponse(429, EXHAUSTED_HEADERS))
    # Remaining > 0 means per-minute throttle, not daily exhaustion.
    assert not _quota_exhausted(
        _FakeResponse(429, {"X-RateLimit-Remaining": "7"})
    )
    # No header at all: cannot claim exhaustion.
    assert not _quota_exhausted(_FakeResponse(429, {}))
    # Garbage header must not raise.
    assert not _quota_exhausted(_FakeResponse(429, {"X-RateLimit-Remaining": "abc"}))


def test_quota_reset_at_parses_epoch_milliseconds():
    reset = _quota_reset_at(_FakeResponse(429, EXHAUSTED_HEADERS))
    assert reset == pytest.approx(1791244800.0)
    assert _quota_reset_at(_FakeResponse(429, {})) is None


# ─── the regression: one request, not len(fallback chain) ───────────────────


async def test_exhausted_quota_makes_exactly_one_request():
    """A spent daily quota must not fan out across the fallback chain.

    Before the fix this walked all six fallback models with a 2s sleep each,
    wasting ~12s and six requests against an already-exhausted quota.
    """
    client = _FakeClient([_FakeResponse(429, EXHAUSTED_HEADERS)] * 10)
    provider = _provider(client)

    with pytest.raises(RateLimitError) as excinfo:
        await provider.complete(messages=[{"role": "user", "content": "hi"}])

    assert len(client.calls) == 1, f"expected 1 request, made {len(client.calls)}"
    err = excinfo.value
    assert err.status_code == 429
    assert err.reset_at == pytest.approx(1791244800.0)
    assert err.daily_limit == 50
    assert err.remaining == 0
    # The message must be actionable and name the reset time.
    assert "quota exhausted" in str(err).lower()
    assert "50" in str(err)


async def test_exhausted_quota_error_is_not_retried_by_the_chain():
    """Even with a non-free model in the chain, quota exhaustion stops fast."""
    client = _FakeClient([_FakeResponse(429, EXHAUSTED_HEADERS)] * 10)
    provider = _provider(client, model="openrouter/free")

    with pytest.raises(RateLimitError):
        await provider.complete(messages=[{"role": "user", "content": "hi"}])

    # Every recorded attempt must be for the FIRST candidate only.
    assert {c["payload"]["model"] for c in client.calls} == {"openrouter/free"}


async def test_per_minute_throttle_still_uses_fallback_chain():
    """A 429 WITHOUT remaining=0 is a transient throttle — rotation is valid.

    The chain must still be walked so a congested single model can be escaped.
    """
    throttled = {"X-RateLimit-Remaining": "5"}
    ok_payload = {
        "choices": [{"message": {"content": "hello", "tool_calls": []}}],
        "model": "qwen/qwen3.8-27b:free",
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    # first candidate throttled, second candidate succeeds
    client = _FakeClient(
        [
            _FakeResponse(429, throttled),
            _FakeResponse(200, {}, ok_payload),
        ]
    )
    provider = _provider(client)

    response = await provider.complete(messages=[{"role": "user", "content": "hi"}])

    assert response.content == "hello"
    assert len(client.calls) == 2
    assert client.calls[0]["payload"]["model"] == "openrouter/free"
    assert client.calls[1]["payload"]["model"] == _FREE_FALLBACK_MODELS[0]


async def test_rate_limit_error_exposes_status_code_for_duck_typed_detection():
    """ah.core.agent._is_rate_limit_error() duck-types on response.status_code."""
    err = RateLimitError("exhausted", status_code=429)
    assert getattr(getattr(err, "response", None), "status_code", None) == 429


async def test_free_request_clamps_max_tokens_to_1024():
    """Free tier 429s harder on large max_tokens, so the payload is clamped."""
    ok_payload = {
        "choices": [{"message": {"content": "ok", "tool_calls": []}}],
        "model": "openrouter/free",
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    client = _FakeClient([_FakeResponse(200, {}, ok_payload)])
    provider = _provider(client)

    await provider.complete(messages=[{"role": "user", "content": "hi"}], max_tokens=4096)

    assert client.calls[0]["payload"]["max_tokens"] == 1024


# ─── malformed 200 responses ────────────────────────────────────────────────


async def test_http_200_with_error_body_raises_provider_error():
    """OpenRouter returns 200 with an ``error`` object — must not KeyError.

    ``raise_for_status()`` does not catch this, so the old
    ``data["choices"][0]`` crashed with an unhelpful KeyError.
    """
    from ah.core.exceptions import ProviderError

    error_body = {"error": {"message": "model is overloaded", "code": 503}}
    client = _FakeClient([_FakeResponse(200, {}, error_body)])
    provider = _provider(client)

    with pytest.raises(ProviderError) as excinfo:
        await provider.complete(messages=[{"role": "user", "content": "hi"}])

    assert "model is overloaded" in str(excinfo.value)
    assert "no choices" in str(excinfo.value)


async def test_http_200_with_empty_choices_raises_provider_error():
    """An empty choices list must fail cleanly, not with IndexError."""
    from ah.core.exceptions import ProviderError

    client = _FakeClient([_FakeResponse(200, {}, {"choices": [], "model": "openrouter/free"})])
    provider = _provider(client)

    with pytest.raises(ProviderError):
        await provider.complete(messages=[{"role": "user", "content": "hi"}])


async def test_http_200_with_null_choices_raises_provider_error():
    """``choices: null`` is also possible from a gateway — guard it too."""
    from ah.core.exceptions import ProviderError

    client = _FakeClient([_FakeResponse(200, {}, {"choices": None})])
    provider = _provider(client)

    with pytest.raises(ProviderError):
        await provider.complete(messages=[{"role": "user", "content": "hi"}])


async def test_choice_missing_message_does_not_crash():
    """A choice without a ``message`` key must degrade to empty content."""
    client = _FakeClient(
        [
            _FakeResponse(
                200,
                {},
                {"choices": [{}], "model": "openrouter/free", "usage": {}},
            )
        ]
    )
    provider = _provider(client)

    response = await provider.complete(messages=[{"role": "user", "content": "hi"}])

    assert response.content == ""
    assert response.tool_calls == []
