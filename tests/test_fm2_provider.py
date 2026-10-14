"""FM2 provider fixes: cache isolation/bounds, honest streaming, retry parity.

Probes run the real provider transports against ``httpx.MockTransport`` —
no network, deterministic, and close to the production call path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from types import SimpleNamespace

import httpx
import pytest
from cachetools import TTLCache

from machina.core import provider as provider_module
from machina.core.exceptions import ProviderError, ValidationError
from machina.core.models import LLMResponse, ToolDefinition
from machina.core.provider import (
    PROVIDER_SPECS,
    AnthropicProvider,
    AsyncTokenBucket,
    DirectProvider,
    LLMProvider,
    OllamaProvider,
    OpenRouterProvider,
)
from machina.core.provider import caches as provider_caches
from machina.core.provider import rate_limit as provider_rate_limit
from machina.core.provider import reasoning as provider_reasoning
from machina.core.provider import transport as provider_transport

MESSAGES = [{"role": "user", "content": "hello"}]
TOOLS = [ToolDefinition("clock", "Read the time", {"type": "object", "properties": {}})]


# ─── transport harness ──────────────────────────────────────────────────────


def _openai_body(
    model: str = "vendor/paid",
    content: str = "original",
    tool_calls: list[dict] | None = None,
) -> dict:
    return {
        "model": model,
        "choices": [{"message": {"content": content, "tool_calls": tool_calls or []}}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
    }


def _sse(*chunks: dict) -> httpx.Response:
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
    return httpx.Response(200, text=body + "data: [DONE]\n\n")


def _anthropic_sse(*events: tuple[str, dict]) -> httpx.Response:
    body = "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events)
    return httpx.Response(200, text=body)


@pytest.fixture
async def make():
    clients: list[httpx.AsyncClient] = []

    def _client(base_url: str, handler) -> httpx.AsyncClient:
        client = httpx.AsyncClient(
            base_url=base_url,
            transport=httpx.MockTransport(handler),
            timeout=5.0,
        )
        clients.append(client)
        return client

    def openrouter(handler, model: str = "vendor/paid") -> OpenRouterProvider:
        provider = OpenRouterProvider(api_key="test-key", model=model)
        provider._rate_limiter = AsyncTokenBucket(rate=0, capacity=0)
        provider.client = _client(OpenRouterProvider.BASE_URL, handler)
        return provider

    def direct(name: str, handler) -> DirectProvider:
        spec = PROVIDER_SPECS[name]
        provider = DirectProvider(spec, api_key="test-key")
        provider._rate_limiter = AsyncTokenBucket(rate=0, capacity=0)
        provider.client = _client(spec.base_url, handler)
        return provider

    def anthropic(handler) -> AnthropicProvider:
        provider = AnthropicProvider(api_key="test-key")
        provider._rate_limiter = AsyncTokenBucket(rate=0, capacity=0)
        provider.client = _client(AnthropicProvider.BASE_URL, handler)
        return provider

    def ollama(handler) -> OllamaProvider:
        provider = OllamaProvider(model="llama3.1")
        provider._rate_limiter = AsyncTokenBucket(rate=0, capacity=0)
        provider.client = _client("http://localhost:11434", handler)
        return provider

    yield SimpleNamespace(openrouter=openrouter, direct=direct, anthropic=anthropic, ollama=ollama)
    for client in clients:
        await client.aclose()


class _UsageLedger:
    """In-memory stand-in for the external accounting store."""

    def __init__(self) -> None:
        self.statuses: dict[uuid.UUID, str] = {}

    async def reserve(self, *args):
        reservation = uuid.uuid4()
        self.statuses[reservation] = "reserved"
        return reservation

    async def finish(self, reservation, usage=None, *, failed=False, model=None):
        self.statuses[reservation] = "error" if failed else "complete"


# ─── W2: cache identity + defensive copies ──────────────────────────────────


async def test_cache_hit_returns_an_independent_copy(make):
    """A mutated response must never poison the cache (same mutable object)."""
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json=_openai_body(
                tool_calls=[
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "clock", "arguments": "{}"},
                    }
                ]
            ),
        )

    provider = make.openrouter(handler)
    first = await provider.complete(MESSAGES)

    # Hostile caller: mutate every mutable container on the response.
    first.content = "poisoned"
    first.usage["prompt_tokens"] = 999_999
    first.raw["choices"][0]["message"]["content"] = "poisoned"
    first.tool_calls[0]["function"]["name"] = "poisoned"

    second = await provider.complete(MESSAGES)
    assert len(requests) == 1, "second identical call must be served from cache"
    assert second is not first
    assert second.content == "original"
    assert second.usage["prompt_tokens"] == 7
    assert second.raw["choices"][0]["message"]["content"] == "original"
    assert second.tool_calls[0]["function"]["name"] == "clock"

    # Mutating one cache hit must not leak into the next one either.
    second.tool_calls[0]["function"]["name"] = "mutated-again"
    second.raw["choices"][0]["message"]["content"] = "mutated-again"
    third = await provider.complete(MESSAGES)
    assert len(requests) == 1
    assert third.content == "original"
    assert third.tool_calls[0]["function"]["name"] == "clock"


async def test_cache_key_scopes_by_caller_identity_when_available(make):
    """Different accounting callers must not share a cached response body."""
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=_openai_body())

    provider = make.openrouter(handler)
    session_a, session_b = uuid.uuid4(), uuid.uuid4()

    await provider.complete(MESSAGES, session_id=session_a, agent_id="agent-a")
    await provider.complete(MESSAGES, session_id=session_b, agent_id="agent-b")
    assert len(requests) == 2, "cross-session cache sharing under-counts usage"

    # Same caller + identical request still hits the cache.
    await provider.complete(MESSAGES, session_id=session_a, agent_id="agent-a")
    assert len(requests) == 2


@pytest.mark.parametrize("family", ["openrouter", "direct", "anthropic", "ollama"])
async def test_cache_identity_scopes_every_provider_family(make, family):
    requests = []

    def handler(request):
        requests.append(request)
        if family == "anthropic":
            return httpx.Response(
                200,
                json={
                    "type": "message",
                    "model": "claude-3-5-sonnet-20241022",
                    "content": [{"type": "text", "text": "ok"}],
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            )
        if family == "ollama":
            return httpx.Response(
                200,
                json={
                    "model": "llama3.1",
                    "message": {"content": "ok"},
                    "prompt_eval_count": 1,
                    "eval_count": 1,
                },
            )
        return httpx.Response(200, json=_openai_body(model="llama-3.3-70b-versatile"))

    if family == "openrouter":
        provider = make.openrouter(handler)
    elif family == "direct":
        provider = make.direct("groq", handler)
    elif family == "anthropic":
        provider = make.anthropic(handler)
    else:
        provider = make.ollama(handler)

    await provider.complete(MESSAGES, session_id=uuid.uuid4(), agent_id="agent-a")
    await provider.complete(MESSAGES, session_id=uuid.uuid4(), agent_id="agent-b")
    assert len(requests) == 2, f"{family} shared a cached body across callers"


def test_cache_key_identity_discriminator_is_stable():
    base = provider_caches._cache_key(MESSAGES, None, "m", {"a": 1})
    scoped = provider_caches._cache_key(MESSAGES, None, "m", {"a": 1}, identity="agent:session")
    assert scoped != base
    assert scoped == provider_caches._cache_key(
        MESSAGES, None, "m", {"a": 1}, identity="agent:session"
    )
    assert base == provider_caches._cache_key(MESSAGES, None, "m", {"a": 1}, identity=None)


# ─── W3: bounded cache with real TTL ────────────────────────────────────────


def test_response_cache_is_size_bounded():
    for index in range(400):
        provider_caches._cache_put(f"fm2-key-{index}", LLMResponse(content=str(index), model="m"))
    assert len(provider_caches._RESPONSE_CACHE) <= 256


def test_response_cache_expires_by_ttl(monkeypatch):
    clock = [1_000.0]
    cache = TTLCache(maxsize=256, ttl=60, timer=lambda: clock[0])
    monkeypatch.setattr(provider_caches, "_RESPONSE_CACHE", cache)

    provider_caches._cache_put("k", LLMResponse(content="v", model="m"))
    assert provider_caches._cache_get("k").content == "v"

    clock[0] += 61.0
    assert provider_caches._cache_get("k") is None


def test_cache_get_missing_key_returns_none():
    assert provider_caches._cache_get("fm2-absent") is None


# ─── W4: every stream path retries 429 with backoff ─────────────────────────


@pytest.fixture
def record_sleep(monkeypatch):
    delays: list[float] = []

    async def record(seconds):
        delays.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", record)
    return delays


async def test_direct_stream_retries_429_with_retry_after(make, record_sleep):
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, headers={"retry-after": "2"})
        return _sse({"model": "llama-3.3-70b-versatile", "choices": [{"delta": {"content": "ok"}}]})

    provider = make.direct("groq", handler)
    events = [event async for event in provider.stream_complete(MESSAGES)]

    assert events[-1].response.content == "ok"
    assert len(requests) == 2
    assert record_sleep == [2.0]


async def test_anthropic_stream_retries_429_with_retry_after(make, record_sleep):
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, headers={"retry-after": "3"})
        return _anthropic_sse(
            (
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "model": "claude-3-5-sonnet-20241022",
                        "usage": {"input_tokens": 4},
                    },
                },
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "hi"},
                },
            ),
            (
                "message_delta",
                {"type": "message_delta", "delta": {}, "usage": {"output_tokens": 1}},
            ),
        )

    provider = make.anthropic(handler)
    events = [event async for event in provider.stream_complete(MESSAGES)]

    assert events[-1].response.content == "hi"
    assert len(requests) == 2
    assert record_sleep == [3.0]


async def test_ollama_stream_retries_429_with_retry_after(make, record_sleep):
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, headers={"retry-after": "1"})
        body = "\n".join(
            [
                json.dumps({"model": "llama3.1", "message": {"content": "hi"}, "done": False}),
                json.dumps({"model": "llama3.1", "message": {"content": "!"}, "done": True}),
            ]
        )
        return httpx.Response(200, text=body + "\n")

    provider = make.ollama(handler)
    events = [event async for event in provider.stream_complete(MESSAGES)]

    assert events[-1].response.content == "hi!"
    assert len(requests) == 2
    assert record_sleep == [1.0]


async def test_direct_stream_gives_up_after_paid_retry_budget(make, record_sleep):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(429, headers={"retry-after": "1"})

    provider = make.direct("groq", handler)
    with pytest.raises(httpx.HTTPStatusError) as error:
        _ = [event async for event in provider.stream_complete(MESSAGES)]

    assert error.value.response.status_code == 429
    assert len(requests) == 3  # initial attempt + _MAX_RETRIES
    assert record_sleep == [1.0, 1.0]


# ─── W5: explicit mid-stream error frames fail the stream honestly ──────────


async def test_openrouter_stream_error_frame_raises(make):
    def handler(request):
        return _sse(
            {"model": "vendor/paid", "choices": [{"delta": {"content": "partial"}}]},
            {"error": {"message": "upstream exploded", "code": 502}},
        )

    provider = make.openrouter(handler)
    with pytest.raises(ProviderError, match="upstream exploded"):
        _ = [event async for event in provider.stream_complete(MESSAGES)]


async def test_direct_stream_error_frame_raises(make):
    def handler(request):
        return _sse({"error": "provider quota exhausted upstream"})

    provider = make.direct("groq", handler)
    with pytest.raises(ProviderError, match="quota exhausted upstream"):
        _ = [event async for event in provider.stream_complete(MESSAGES)]


async def test_anthropic_stream_error_event_raises(make):
    def handler(request):
        return _anthropic_sse(
            (
                "message_start",
                {"type": "message_start", "message": {"model": "claude-3-5-sonnet-20241022"}},
            ),
            (
                "error",
                {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
            ),
        )

    provider = make.anthropic(handler)
    with pytest.raises(ProviderError, match="Overloaded"):
        _ = [event async for event in provider.stream_complete(MESSAGES)]


async def test_ollama_stream_error_frame_raises(make):
    def handler(request):
        body = "\n".join(
            [
                json.dumps({"model": "llama3.1", "message": {"content": "hi"}, "done": False}),
                json.dumps({"error": "model 'ghost' not found"}),
            ]
        )
        return httpx.Response(200, text=body + "\n")

    provider = make.ollama(handler)
    with pytest.raises(ProviderError, match="ghost"):
        _ = [event async for event in provider.stream_complete(MESSAGES)]


async def test_stream_keepalives_and_usage_chunks_stay_tolerated(make):
    """Benign keep-alives/unknown events must not be mistaken for errors."""

    def direct_handler(request):
        body = "".join(
            [
                ": keep-alive\n\n",
                "event: unknown\ndata: {not json}\n\n",
                'data: {"choices": [], "usage": {"prompt_tokens": 4, "completion_tokens": 1}}\n\n',
                'data: {"model": "llama-3.3-70b-versatile", "choices": [{"delta": {"content": "hi"}}]}\n\n',
                "data: [DONE]\n\n",
            ]
        )
        return httpx.Response(200, text=body)

    provider = make.direct("groq", direct_handler)
    events = [event async for event in provider.stream_complete(MESSAGES)]
    assert events[-1].response.content == "hi"

    def anthropic_handler(request):
        return _anthropic_sse(
            (
                "message_start",
                {"type": "message_start", "message": {"model": "claude-3-5-sonnet-20241022"}},
            ),
            ("ping", {"type": "ping"}),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "hi"},
                },
            ),
            ("message_stop", {"type": "message_stop"}),
        )

    provider = make.anthropic(anthropic_handler)
    events = [event async for event in provider.stream_complete(MESSAGES)]
    assert events[-1].response.content == "hi"

    def ollama_handler(request):
        body = "\n".join(
            [
                "keep-alive",
                json.dumps({"model": "llama3.1", "message": {"content": "hi"}, "done": True}),
            ]
        )
        return httpx.Response(200, text=body + "\n")

    provider = make.ollama(ollama_handler)
    events = [event async for event in provider.stream_complete(MESSAGES)]
    assert events[-1].response.content == "hi"


# ─── W5b: clean EOF without a terminal frame is a truncation, not `done` ───


def _partial_sse(model: str) -> httpx.Response:
    chunk = {
        "model": model,
        "choices": [{"delta": {"content": "partial text"}, "finish_reason": None}],
    }
    return httpx.Response(200, text=f"data: {json.dumps(chunk)}\n\n")


async def test_direct_stream_partial_frame_clean_eof_raises(make):
    provider = make.direct("groq", lambda request: _partial_sse("llama-3.3-70b-versatile"))
    with pytest.raises(ProviderError, match="terminal"):
        _ = [event async for event in provider.stream_complete(MESSAGES)]


async def test_openrouter_stream_partial_frame_clean_eof_raises(make):
    provider = make.openrouter(lambda request: _partial_sse("vendor/paid"))
    with pytest.raises(ProviderError, match="terminal"):
        _ = [event async for event in provider.stream_complete(MESSAGES)]


async def test_direct_stream_finish_reason_without_done_completes(make):
    chunk = {
        "model": "llama-3.3-70b-versatile",
        "choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}],
    }
    provider = make.direct(
        "groq", lambda request: httpx.Response(200, text=f"data: {json.dumps(chunk)}\n\n")
    )
    events = [event async for event in provider.stream_complete(MESSAGES)]
    assert events[-1].type == "done"
    assert events[-1].response.content == "ok"


async def test_openrouter_stream_finish_reason_without_done_completes(make):
    chunk = {
        "model": "vendor/paid",
        "choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}],
    }
    provider = make.openrouter(
        lambda request: httpx.Response(200, text=f"data: {json.dumps(chunk)}\n\n")
    )
    events = [event async for event in provider.stream_complete(MESSAGES)]
    assert events[-1].type == "done"
    assert events[-1].response.content == "ok"


# ─── W6: Ollama complete guards a missing message + reservation ─────────────


async def test_ollama_complete_without_message_raises_and_finishes_failed(make, monkeypatch):
    ledger = _UsageLedger()
    monkeypatch.setattr("machina.core.provider.ollama.usage_store", ledger)

    def handler(request):
        # Ollama can answer 200 with no message (e.g. {"done": true}).
        return httpx.Response(200, json={"model": "llama3.1", "done": True})

    provider = make.ollama(handler)
    with pytest.raises(ProviderError):
        await provider.complete(MESSAGES, session_id=uuid.uuid4(), agent_id="agent")

    assert list(ledger.statuses.values()) == ["error"]
    assert len(ledger.statuses) == 1


# ─── N1: fallback results are not cached under the requested model ──────────


async def test_fallback_result_is_not_cached_under_requested_model(make, monkeypatch, record_sleep):
    monkeypatch.setattr("machina.core.provider.openrouter._FREE_FALLBACK_MODELS", ("a/model:free",))
    requested: list[str] = []

    def handler(request):
        payload = json.loads(request.content)
        requested.append(payload["model"])
        if payload["model"] == "openrouter/free":
            return httpx.Response(429, headers={"x-ratelimit-remaining": "5"})
        return httpx.Response(200, json=_openai_body(model="a/model:free", content="from fallback"))

    provider = make.openrouter(handler, model="openrouter/free")
    first = await provider.complete(MESSAGES)
    assert first.model == "a/model:free"

    second = await provider.complete(MESSAGES)
    assert second.model == "a/model:free"
    # Both calls walked the chain; the fallback answer was not cached as if it
    # had come from the requested model.
    assert requested.count("openrouter/free") == 2
    assert requested.count("a/model:free") == 2


async def test_first_candidate_success_is_still_cached(make):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=_openai_body())

    provider = make.openrouter(handler)
    await provider.complete(MESSAGES)
    await provider.complete(MESSAGES)
    assert len(requests) == 1


# ─── N5: Retry-After HTTP-date parsing ──────────────────────────────────────


def test_retry_delay_parses_http_date():
    future = datetime.now(UTC) + timedelta(seconds=30)
    response = httpx.Response(429, headers={"retry-after": format_datetime(future, usegmt=True)})
    delay = provider_transport._retry_delay(0, response, free=False)
    assert 25.0 <= delay <= 30.5


def test_retry_delay_clamps_past_http_date_to_zero():
    past = datetime.now(UTC) - timedelta(seconds=30)
    response = httpx.Response(429, headers={"retry-after": format_datetime(past, usegmt=True)})
    assert provider_transport._retry_delay(0, response, free=False) == 0.0


def test_retry_delay_garbage_falls_back_to_backoff():
    response = httpx.Response(429, headers={"retry-after": "in a bit"})
    delay = provider_transport._retry_delay(0, response, free=False)
    assert 1.0 <= delay <= 2.0  # base 1s * 2**0 + <1s jitter


def test_retry_delay_still_parses_delta_seconds():
    response = httpx.Response(429, headers={"retry-after": "7"})
    assert provider_transport._retry_delay(0, response, free=False) == 7.0


# ─── N6: invalid reasoning_effort config fails loudly, not silently ─────────


def test_resolve_effort_warns_and_falls_back_on_invalid_config(monkeypatch, caplog):
    from machina.core.config import config

    monkeypatch.setattr(config, "_session_overrides", {"reasoning_effort": "ultra"})
    with caplog.at_level(logging.WARNING, logger="machina.core.provider"):
        assert provider_reasoning._resolve_effort(None) == ""
    assert "reasoning_effort" in caplog.text.lower()


def test_resolve_effort_falls_back_on_config_value_error(monkeypatch, caplog):
    from machina.core.config import config

    def broken(key):
        raise ValueError("bad config plumbing")

    monkeypatch.setattr(config, "get", broken)
    with caplog.at_level(logging.WARNING, logger="machina.core.provider"):
        assert provider_reasoning._resolve_effort(None) == ""
    # The safe-logging record factory strips exception text (it can echo
    # secrets), so the warning names the key + exception class only.
    assert "reasoning_effort" in caplog.text
    assert "ValueError" in caplog.text


def test_resolve_effort_propagates_unexpected_config_errors(monkeypatch):
    from machina.core.config import config

    def broken(key):
        raise RuntimeError("backend exploded")

    monkeypatch.setattr(config, "get", broken)
    with pytest.raises(RuntimeError, match="backend exploded"):
        provider_reasoning._resolve_effort(None)


def test_resolve_effort_precedence_unchanged():
    assert provider_reasoning._resolve_effort("high") == "high"
    assert provider_reasoning._resolve_effort(None, "medium") == "medium"
    with pytest.raises(ValidationError):
        provider_reasoning._resolve_effort("ultra")


# ─── N7: close contract on the base; dead embed implementations gone ────────


async def test_base_provider_close_is_a_noop_contract():
    provider = LLMProvider()
    await provider.close()  # must not raise for providers with no resources


def test_dead_embed_implementations_removed():
    assert "embed" not in OpenRouterProvider.__dict__
    assert "embed" not in OllamaProvider.__dict__


# ─── N11: runtime rate-limit reload ─────────────────────────────────────────


def test_reload_rate_limiters_rebuilds_existing_buckets(monkeypatch):
    from machina.core.config import config

    monkeypatch.setattr(config, "_session_overrides", {"rate_limit_calls_per_minute": 30})
    bucket = provider_rate_limit._get_rate_limiter("fm2-reload-test")
    try:
        assert bucket.rate == 0.5
        assert bucket.capacity == 30

        monkeypatch.setattr(config, "_session_overrides", {"rate_limit_calls_per_minute": 120})
        provider_module.reload_rate_limiters()

        rebuilt = provider_rate_limit._RATE_LIMITERS["fm2-reload-test"]
        assert rebuilt is not bucket
        assert rebuilt.rate == 2.0
        assert rebuilt.capacity == 120
        # Buckets for providers that never made a call are not materialized.
        assert "fm2-untouched" not in provider_rate_limit._RATE_LIMITERS
    finally:
        provider_rate_limit._RATE_LIMITERS.pop("fm2-reload-test", None)


def test_reload_rate_limiters_rejects_invalid_config(monkeypatch):
    from machina.core.config import config

    def broken(key):
        return -5

    monkeypatch.setattr(config, "get", broken)
    with pytest.raises(ValidationError):
        provider_module.reload_rate_limiters()


def test_reload_rate_limiters_is_exported():
    assert "reload_rate_limiters" in provider_module.__all__
