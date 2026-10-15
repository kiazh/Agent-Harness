"""R5-GH (findings 11/12/13): provider transport correctness — RED-first.

G1 (finding 11): identical Ollama calls against two ``base_url``s within the
  response-cache TTL must contact both servers. Pre-fix the cache scope omits
  the endpoint, so the second server is never hit and the first server's body
  is returned.
G2 (finding 12): SSE streams emitting ``data:{...}`` / ``data:[DONE]`` (no
  space after the colon) must be parsed. Pre-fix every provider requires the
  literal ``data: `` prefix, so spaceless frames are dropped and the stream
  reports truncation. Post-fix ONE shared parser (``sse_field`` in
  ``machina/core/provider/sse.py``) serves all six SSE sites.
G3 (finding 13): ``reload_rate_limiters()`` must reach live providers.
  Pre-fix providers hold the construction-time bucket object while reload
  replaces the registry entry, so live providers keep the old budget.

Mocks live only at the transport edge (httpx.MockTransport / stubbed
client.post); cache keys, the SSE parser, and the token buckets are real.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest


@pytest.fixture
def _high_rate_limit(monkeypatch):
    """Fresh, effectively-unbounded buckets + empty response cache per test."""
    from machina.core.config import config
    from machina.core.provider import rate_limit as rl
    from machina.core.provider.caches import _cache_clear

    monkeypatch.setattr(config, "_session_overrides", {"rate_limit_calls_per_minute": 60000})
    saved = dict(rl._RATE_LIMITERS)
    rl._RATE_LIMITERS.clear()
    _cache_clear()
    try:
        yield rl
    finally:
        rl._RATE_LIMITERS.clear()
        rl._RATE_LIMITERS.update(saved)
        _cache_clear()


def _mock_post(response_data: dict) -> MagicMock:
    mock_response = MagicMock()
    mock_response.json.return_value = response_data
    mock_response.raise_for_status = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {}
    return mock_response


# ─── G1: ollama cache must identify the server ─────────────────────────────


def _ollama_handler(tag: str):
    def handler(request: httpx.Request) -> httpx.Response:
        port = request.url.port
        return httpx.Response(
            200,
            json={
                "model": "llama3.1",
                "message": {"content": f"{tag}-{port}", "tool_calls": []},
                "prompt_eval_count": 1,
                "eval_count": 1,
            },
        )

    return handler


def _build_ollama(base_url: str):
    from machina.core.provider.ollama import OllamaProvider

    provider = OllamaProvider(model="llama3.1", base_url=base_url)
    transport = httpx.MockTransport(_ollama_handler("server"))
    provider.client = httpx.AsyncClient(base_url=base_url, transport=transport)
    return provider


async def test_r5_g1_ollama_cache_scoped_by_endpoint(_high_rate_limit):
    """Same call, two base_urls within TTL → both servers are contacted."""
    first = _build_ollama("http://127.0.0.1:11434")
    second = _build_ollama("http://127.0.0.1:11435")
    messages = [{"role": "user", "content": "r5-g1-identical-call"}]
    try:
        one = await first.complete(messages)
        two = await second.complete(messages)
        assert one.content == "server-11434", one.content
        # RED pre-fix: "server-11434" — the second endpoint is never hit.
        assert two.content == "server-11435", two.content
    finally:
        await first.close()
        await second.close()


async def test_r5_g1_ollama_trailing_slash_shares_endpoint(_high_rate_limit):
    """Trailing-slash variants of one endpoint share a cache entry (1 hit)."""
    from machina.core.provider import caches

    hits: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(request.url.port)
        return httpx.Response(
            200,
            json={
                "model": "llama3.1",
                "message": {"content": "ok", "tool_calls": []},
                "prompt_eval_count": 1,
                "eval_count": 1,
            },
        )

    from machina.core.provider.ollama import OllamaProvider

    first = OllamaProvider(model="llama3.1", base_url="http://127.0.0.1:11436")
    second = OllamaProvider(model="llama3.1", base_url="http://127.0.0.1:11436/")
    for provider in (first, second):
        await provider.client.aclose()
        provider.client = httpx.AsyncClient(
            base_url=provider.base_url, transport=httpx.MockTransport(handler)
        )
    messages = [{"role": "user", "content": "r5-g1-slash-variant"}]
    try:
        assert (await first.complete(messages)).content == "ok"
        assert (await second.complete(messages)).content == "ok"
        assert hits == [11436], hits
        assert len(caches._RESPONSE_CACHE) == 1
    finally:
        await first.close()
        await second.close()


# ─── G2: shared SSE field parser, space-tolerant ────────────────────────────


async def test_r5_g2_sse_parser_tolerates_missing_space():
    """The single shared parser accepts `data:X` and `event:X` forms."""
    from machina.core.provider.sse import sse_data, sse_event, sse_field

    assert sse_data('data:{"a": 1}') == '{"a": 1}'
    assert sse_data('data: {"a": 1}') == '{"a": 1}'
    assert sse_data("data:[DONE]") == "[DONE]"
    assert sse_data("data: [DONE]") == "[DONE]"
    assert sse_data("data:  padded") == " padded"
    assert sse_data("data:") == ""
    assert sse_data("data") == ""
    assert sse_data("") is None
    assert sse_data(": keep-alive comment") is None
    assert sse_data("event: message_start") is None
    assert sse_event("event:message_start") == "message_start"
    assert sse_event("event: message_start") == "message_start"
    assert sse_event("event:") == ""
    assert sse_event("data: x") is None
    assert sse_field("data:x", "data") == "x"
    # A single leading UTF-8 BOM is ignored (a BOM-prefixed first frame must
    # not drop its payload); a second BOM is payload.
    assert sse_data('\ufeffdata:{"a": 1}') == '{"a": 1}'
    assert sse_event("\ufeffevent:message_start") == "message_start"
    assert sse_data("\ufeff\ufeffdata: x") is None


class _MockStreamResponse:
    def __init__(self, body: bytes):
        self._body = body
        self.status_code = 200
        self.headers = {}

    def raise_for_status(self):
        pass

    async def aiter_lines(self):
        for line in self._body.decode().splitlines():
            yield line


class _MockStreamContext:
    def __init__(self, body: bytes):
        self._response = _MockStreamResponse(body)

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc):
        return False


def _streaming_client(body: bytes) -> MagicMock:
    client = MagicMock()
    client.stream = MagicMock(return_value=_MockStreamContext(body))
    return client


async def test_r5_g2_direct_stream_no_space_frames(_high_rate_limit):
    """DirectProvider consumes `data:{...}` + `data:[DONE]` without spaces."""
    from machina.core.provider.direct import DirectProvider
    from machina.core.provider.specs import ProviderSpec

    spec = ProviderSpec(
        "r5-gh-sse", "https://r5-gh-sse.invalid/v1", "R5_GH_SSE_KEY", "m", requires_key=False
    )
    provider = DirectProvider.__new__(DirectProvider)
    provider.spec = spec
    provider.api_key = "k"
    provider.model = "m"
    provider._default_effort = ""
    provider._rate_limiter = AsyncMock()
    provider._rate_limit_key = spec.name
    chunk = {"model": "m", "choices": [{"delta": {"content": "Hello"}}]}
    stop = {"model": "m", "choices": [{"delta": {}, "finish_reason": "stop"}]}
    provider.client = _streaming_client(
        f"data:{json.dumps(chunk)}\ndata:{json.dumps(stop)}\ndata:[DONE]\n".encode()
    )
    events = [
        event async for event in provider.stream_complete([{"role": "user", "content": "hi"}])
    ]
    texts = [event.content for event in events if event.type == "text"]
    # RED pre-fix: no text events; the stream raises a truncation ProviderError.
    assert texts == ["Hello"], texts
    done = next(event for event in events if event.type == "done")
    assert done.response.content == "Hello"


async def test_r5_g2_anthropic_stream_no_space_frames(_high_rate_limit):
    """AnthropicProvider consumes `event:X` / `data:X` without spaces."""
    from machina.core.provider.anthropic import AnthropicProvider

    provider = AnthropicProvider.__new__(AnthropicProvider)
    provider.api_key = "k"
    provider.model = "m"
    provider._default_effort = ""
    provider._rate_limiter = AsyncMock()
    provider._rate_limit_key = AnthropicProvider.PROVIDER_NAME
    lines = [
        "event:message_start",
        'data:{"type":"message_start","message":{"model":"m","usage":{"input_tokens":1}}}',
        "event:content_block_delta",
        'data:{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hi"}}',
        "event:message_delta",
        'data:{"type":"message_delta","usage":{"output_tokens":2}}',
        "event:message_stop",
        'data:{"type":"message_stop"}',
    ]
    provider.client = _streaming_client(("\n".join(lines) + "\n").encode())
    events = [
        event async for event in provider.stream_complete([{"role": "user", "content": "hi"}])
    ]
    texts = [event.content for event in events if event.type == "text"]
    # RED pre-fix: event lines are missed, data lines dropped → truncation error.
    assert texts == ["Hi"], texts
    done = next(event for event in events if event.type == "done")
    assert done.response.content == "Hi"


async def test_r5_g2_openrouter_stream_no_space_frames(_high_rate_limit):
    """OpenRouterProvider consumes spaceless `data:` / `data:[DONE]` frames.

    A paid model id keeps the free-tier fallback chain out of the path so the
    assertion isolates the shared SSE parser.
    """
    from machina.core.provider.openrouter import OpenRouterProvider

    provider = OpenRouterProvider.__new__(OpenRouterProvider)
    provider.api_key = "k"
    provider.model = "vendor/paid-model"
    provider._default_effort = ""
    provider._rate_limiter = AsyncMock()
    provider._rate_limit_key = "openrouter"
    chunk = {"model": "vendor/paid-model", "choices": [{"delta": {"content": "Hey"}}]}
    stop = {"model": "vendor/paid-model", "choices": [{"delta": {}, "finish_reason": "stop"}]}
    provider.client = _streaming_client(
        f"data:{json.dumps(chunk)}\ndata:{json.dumps(stop)}\ndata:[DONE]\n".encode()
    )
    events = [
        event async for event in provider.stream_complete([{"role": "user", "content": "hi"}])
    ]
    texts = [event.content for event in events if event.type == "text"]
    assert texts == ["Hey"], texts
    done = next(event for event in events if event.type == "done")
    assert done.response.content == "Hey"


async def test_r5_g2_chatgpt_stream_no_space_bom_frames(_high_rate_limit):
    """ChatGPTProvider consumes spaceless `data:` frames (BOM-prefixed first).

    Behavioral replacement for the removed source scan: drive the real Codex
    Responses stream path so the shared parser is proven by parsed events, not
    by source text. A BOM on the first frame exercises the same normalization
    the scan used to imply.
    """
    from machina.core.provider.chatgpt import ChatGPTProvider

    provider = ChatGPTProvider.__new__(ChatGPTProvider)
    provider.model = ChatGPTProvider.DEFAULT_MODEL
    provider._default_effort = ""
    provider._rate_limiter = AsyncMock()
    provider._rate_limit_key = "chatgpt"
    provider._auth_path = None
    provider._account_hint = MagicMock(return_value="")
    provider._resolve_tokens = AsyncMock(return_value={"access_token": "k", "account_id": "acct"})
    delta = {
        "type": "response.output_text.delta",
        "item_id": "msg_1",
        "output_index": 0,
        "delta": "Hello",
    }
    completed = {
        "type": "response.completed",
        "response": {"id": "r1", "model": provider.model, "status": "completed"},
    }
    provider.client = _streaming_client(
        f"\ufeffdata:{json.dumps(delta)}\ndata:{json.dumps(completed)}\n".encode()
    )
    events = [
        event async for event in provider.stream_complete([{"role": "user", "content": "hi"}])
    ]
    texts = [event.content for event in events if event.type == "text"]
    assert texts == ["Hello"], texts
    done = next(event for event in events if event.type == "done")
    assert done.response.content == "Hello"


async def test_r5_g2_copilot_stream_no_space_frames(_high_rate_limit):
    """CopilotProvider consumes spaceless `data:` / `data:[DONE]` frames."""
    from machina.core.provider.copilot import CopilotProvider

    provider = CopilotProvider.__new__(CopilotProvider)
    provider.model = "gpt-6.1-sol"
    provider._default_effort = ""
    provider._rate_limiter = AsyncMock()
    provider._rate_limit_key = "copilot"
    provider._auth_path = None
    provider._resolve_tokens = AsyncMock(return_value={"token": "k"})
    chunk = {"choices": [{"index": 0, "delta": {"content": "Hey"}}]}
    stop = {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
    provider.client = _streaming_client(
        f"data:{json.dumps(chunk)}\ndata:{json.dumps(stop)}\ndata:[DONE]\n".encode()
    )
    events = [
        event async for event in provider.stream_complete([{"role": "user", "content": "hi"}])
    ]
    texts = [event.content for event in events if event.type == "text"]
    assert texts == ["Hey"], texts
    done = next(event for event in events if event.type == "done")
    assert done.response.content == "Hey"


async def test_r5_g2_ollama_ndjson_needs_no_sse_prefix(_high_rate_limit):
    """Ollama streams NDJSON: a bare JSON line parses with no `data:` prefix.

    Behavioral replacement for the old `sse_data` source scan of ollama.py: if
    the NDJSON path were routed through the SSE parser, this unprefixed line
    would be dropped and no text would be emitted.
    """
    from machina.core.provider.ollama import OllamaProvider

    provider = OllamaProvider.__new__(OllamaProvider)
    provider.model = "llama3.1"
    provider.base_url = "http://127.0.0.1:11434"
    provider._default_effort = ""
    provider._rate_limiter = AsyncMock()
    provider._rate_limit_key = "ollama"
    body = (
        json.dumps({"model": "llama3.1", "message": {"content": "Hi"}, "done": False})
        + "\n"
        + json.dumps({"model": "llama3.1", "message": {"content": ""}, "done": True})
        + "\n"
    )
    provider.client = _streaming_client(body.encode())
    events = [
        event async for event in provider.stream_complete([{"role": "user", "content": "hi"}])
    ]
    texts = [event.content for event in events if event.type == "text"]
    assert texts == ["Hi"], texts
    done = next(event for event in events if event.type == "done")
    assert done.response.content == "Hi"


# ─── G3: reload reaches live providers ───────────────────────────────────────


def _build_direct(key: str):
    from machina.core.provider.direct import DirectProvider
    from machina.core.provider.specs import ProviderSpec

    spec = ProviderSpec(
        key, f"https://{key}.invalid/v1", f"{key.upper()}_KEY", "m", requires_key=False
    )
    return DirectProvider(spec, api_key="k")


async def test_r5_g3_live_provider_tracks_reload(monkeypatch, _high_rate_limit):
    """A constructed provider observes the post-reload budget, not the stale one."""
    from machina.core.config import config
    from machina.core.provider import rate_limit as rl

    key = "r5-gh-g3-live"
    monkeypatch.setattr(config, "_session_overrides", {"rate_limit_calls_per_minute": 60})
    rl._RATE_LIMITERS.pop(key, None)
    provider = _build_direct(key)
    try:
        assert provider._rate_limiter.rate == pytest.approx(1.0)
        assert provider._rate_limiter.capacity == 60
        monkeypatch.setattr(config, "_session_overrides", {"rate_limit_calls_per_minute": 600})
        rl.reload_rate_limiters()
        registry = rl._RATE_LIMITERS[key]
        assert registry.rate == pytest.approx(10.0)
        assert registry.capacity == 600
        live = provider._rate_limiter
        # RED pre-fix: `live` is the construction-time object (rate 1.0).
        assert live is registry
        assert live.rate == pytest.approx(10.0)
        assert live.capacity == 600
    finally:
        rl._RATE_LIMITERS.pop(key, None)
        await provider.client.aclose()


async def test_r5_g3_instance_override_still_honored(_high_rate_limit):
    """Assigning `provider._rate_limiter` (test bypass seam) keeps working."""
    key = "r5-gh-g3-override"
    provider = _build_direct(key)
    try:
        provider._rate_limiter = AsyncMock()
        provider.client.post = AsyncMock(
            return_value=_mock_post(
                {
                    "model": "m",
                    "choices": [{"message": {"content": "OK", "tool_calls": []}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                }
            )
        )
        response = await provider.complete([{"role": "user", "content": "hi"}])
        assert response.content == "OK"
        provider._rate_limiter.acquire.assert_awaited()
    finally:
        from machina.core.provider import rate_limit as rl

        rl._RATE_LIMITERS.pop(key, None)
        await provider.client.aclose()
