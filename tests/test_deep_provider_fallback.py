"""OpenRouter fallback/retry contracts at the HTTP transport boundary."""

from __future__ import annotations

import json
import uuid

import httpx
import pytest

from ah.core import provider as provider_module
from ah.core.exceptions import RateLimitError
from ah.core.models import ToolDefinition
from ah.core.provider import AsyncTokenBucket, OpenRouterProvider

MESSAGES = [{"role": "user", "content": "hello"}]
TOOLS = [ToolDefinition("clock", "Read the time", {"type": "object", "properties": {}})]
QUOTA_HEADERS = {"x-ratelimit-remaining": "0", "x-ratelimit-limit": "50"}


def _sse(model: str) -> httpx.Response:
    chunks = [
        {"model": model, "choices": [{"delta": {"content": "hello"}}]},
        {"choices": [], "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}},
    ]
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
    return httpx.Response(200, text=body + "data: [DONE]\n\n")


@pytest.fixture
async def make_provider(monkeypatch):
    clients = []
    delays = []

    async def record_delay(seconds):
        delays.append(seconds)

    monkeypatch.setattr(provider_module.asyncio, "sleep", record_delay)

    def make(handler, model="vendor/paid-model"):
        provider = OpenRouterProvider.__new__(OpenRouterProvider)
        provider.model = model
        provider.api_key = "test-key"
        provider._default_effort = ""
        provider._rate_limiter = AsyncTokenBucket(rate=1000, capacity=1000)
        provider.client = httpx.AsyncClient(
            base_url=OpenRouterProvider.BASE_URL, transport=httpx.MockTransport(handler)
        )
        clients.append(provider.client)
        return provider, delays

    yield make
    for client in clients:
        await client.aclose()


async def test_paid_stream_404_with_tools_retries_plain_chat(make_provider):
    """A single candidate must remain eligible for the plain-chat fallback."""
    requests = []

    def handle(request):
        payload = json.loads(request.content)
        requests.append(payload)
        return httpx.Response(404) if "tools" in payload else _sse(payload["model"])

    provider, _ = make_provider(handle)
    events = [event async for event in provider.stream_complete(MESSAGES, tools=TOOLS)]

    assert events[-1].response.content == "hello"
    assert events[-1].response.usage == {
        "prompt_tokens": 2,
        "completion_tokens": 1,
        "total_tokens": 3,
    }
    assert ["tools" in payload for payload in requests] == [True, False]


async def test_free_stream_retries_last_candidate_without_tools(make_provider, monkeypatch):
    """A chain longer than two candidates must reach its last plain-chat retry."""
    monkeypatch.setattr(provider_module, "_FREE_FALLBACK_MODELS", ("a/model:free", "b/model:free"))
    requests = []

    def handle(request):
        payload = json.loads(request.content)
        requests.append(payload)
        return httpx.Response(404) if "tools" in payload else _sse(payload["model"])

    provider, _ = make_provider(handle, "openrouter/free")
    events = [event async for event in provider.stream_complete(MESSAGES, tools=TOOLS)]

    assert events[-1].response.model == "b/model:free"
    assert events[-1].response.content == "hello"
    assert [(p["model"], "tools" in p) for p in requests] == [
        ("openrouter/free", True),
        ("a/model:free", True),
        ("b/model:free", True),
        ("b/model:free", False),
    ]


async def test_paid_stream_retries_429_with_retry_after(make_provider):
    """Streaming must honor the same paid retry policy as complete()."""
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(429, headers={"retry-after": "7"})
        return _sse("vendor/paid-model")

    provider, delays = make_provider(handle)
    events = [event async for event in provider.stream_complete(MESSAGES)]

    assert events[-1].response.content == "hello"
    assert len(requests) == 2
    assert delays == [7.0]


async def test_paid_stream_stops_after_two_rate_limit_retries(make_provider):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(429, headers={"retry-after": "3"})

    provider, delays = make_provider(handle)
    with pytest.raises(httpx.HTTPStatusError) as error:
        _ = [event async for event in provider.stream_complete(MESSAGES)]

    assert error.value.response.status_code == 429
    assert len(requests) == 3
    assert delays == [3.0, 3.0]


class _UsageLedger:
    """In-memory stand-in for the external accounting store."""

    def __init__(self):
        self.statuses = {}

    async def reserve(self, *args):
        reservation = uuid.uuid4()
        self.statuses[reservation] = "reserved"
        return reservation

    async def finish(self, reservation, usage=None, *, failed=False, model=None):
        self.statuses[reservation] = "error" if failed else "complete"


@pytest.mark.parametrize("stream", [False, True])
async def test_daily_quota_error_finishes_reservation_without_rotating(
    make_provider, monkeypatch, stream
):
    """Daily quota exhaustion must surface its type and finalize usage."""
    requests = []
    ledger = _UsageLedger()
    monkeypatch.setattr(provider_module, "usage_store", ledger)

    def handle(request):
        requests.append(request)
        return httpx.Response(429, headers=QUOTA_HEADERS)

    provider, delays = make_provider(handle, "openrouter/free")
    kwargs = {"session_id": uuid.uuid4(), "agent_id": "agent"}
    with pytest.raises(RateLimitError) as error:
        if stream:
            _ = [event async for event in provider.stream_complete(MESSAGES, **kwargs)]
        else:
            await provider.complete(MESSAGES, **kwargs)

    assert error.value.daily_limit == 50
    assert len(requests) == 1
    assert delays == []
    assert list(ledger.statuses.values()) == ["error"]


async def test_plain_chat_stream_failure_surfaces_the_retry_error(make_provider):
    """A failed plain-chat retry must not re-raise the original tools error."""

    def handle(request):
        payload = json.loads(request.content)
        return httpx.Response(404 if "tools" in payload else 503)

    provider, _ = make_provider(handle)
    with pytest.raises(httpx.HTTPStatusError) as error:
        _ = [event async for event in provider.stream_complete(MESSAGES, tools=TOOLS)]

    assert error.value.response.status_code == 503


@pytest.mark.parametrize("stream", [False, True])
async def test_plain_chat_quota_error_finishes_reservation(make_provider, monkeypatch, stream):
    """Quota errors from the stripped-tools retry retain their type/accounting."""
    monkeypatch.setattr(provider_module, "_FREE_FALLBACK_MODELS", ())
    ledger = _UsageLedger()
    monkeypatch.setattr(provider_module, "usage_store", ledger)
    requests = []

    def handle(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if "tools" in payload:
            return httpx.Response(404)
        return httpx.Response(429, headers=QUOTA_HEADERS)

    provider, delays = make_provider(handle, "openrouter/free")
    kwargs = {"tools": TOOLS, "session_id": uuid.uuid4(), "agent_id": "agent"}
    with pytest.raises(RateLimitError):
        if stream:
            _ = [event async for event in provider.stream_complete(MESSAGES, **kwargs)]
        else:
            await provider.complete(MESSAGES, **kwargs)

    assert ["tools" in payload for payload in requests] == [True, False]
    assert delays == []
    assert list(ledger.statuses.values()) == ["error"]
