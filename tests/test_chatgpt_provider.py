"""ChatGPT-account provider: Codex protocol request shape + SSE discipline.

Probes run the real transport against ``httpx.MockTransport``; token
resolution uses a real store written under ``tmp_path`` (with a fake HOME so
the Codex CLI fallback stays hermetic). No network in tests.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from machina.core.exceptions import ProviderError, RateLimitError, ValidationError
from machina.core.models import LLMResponse, ToolDefinition
from machina.core.provider import ChatGPTProvider, get_provider
from machina.core.provider.rate_limit import AsyncTokenBucket
from machina.security import chatgpt_auth
from machina.security.chatgpt_auth import ChatGPTAuthError

MESSAGES = [
    {"role": "system", "content": "be brief"},
    {"role": "user", "content": "hello"},
]
TOOLS = [
    ToolDefinition(
        "get_weather",
        "Look up current weather for a city",
        {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
    )
]


def _jwt(payload: dict) -> str:
    def b64(value: dict) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64(payload)}.sig"


def _tokens(
    *, account: str = "acct-provider", access: str | None = None, refresh: str = "rt-1"
) -> dict:
    claims = {
        "https://api.openai.com/auth": {"chatgpt_account_id": account},
        "exp": time.time() + 3600,
    }
    return {
        "access_token": access or _jwt(claims),
        "refresh_token": refresh,
        "id_token": _jwt({"https://api.openai.com/auth": {"chatgpt_account_id": account}}),
    }


def _sse(*events: dict) -> httpx.Response:
    body = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


def _text_stream(
    *deltas: str, model: str = "gpt-6.1-sol", usage: dict | None = None
) -> httpx.Response:
    events = [
        {"type": "response.created", "response": {"id": "resp_1", "model": model}},
        {"type": "response.in_progress"},
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {"type": "message", "id": "msg_1", "role": "assistant", "content": []},
        },
    ]
    for delta in deltas:
        events.append(
            {
                "type": "response.output_text.delta",
                "item_id": "msg_1",
                "output_index": 0,
                "delta": delta,
            }
        )
    events.append({"type": "response.output_text.done", "item_id": "msg_1", "output_index": 0})
    events.append(
        {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {
                "type": "message",
                "id": "msg_1",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "".join(deltas)}],
            },
        }
    )
    final: dict = {"id": "resp_1", "model": model, "status": "completed", "output": []}
    if usage is not None:
        final["usage"] = usage
    events.append({"type": "response.completed", "response": final})
    return _sse(*events)


def _tool_stream() -> httpx.Response:
    call_id = "call_abc123"
    item_id = "fc_abc123"
    return _sse(
        {"type": "response.created", "response": {"id": "resp_2"}},
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {
                "type": "function_call",
                "id": item_id,
                "call_id": call_id,
                "name": "get_weather",
                "arguments": "",
                "status": "in_progress",
            },
        },
        {
            "type": "response.function_call_arguments.delta",
            "item_id": item_id,
            "output_index": 0,
            "delta": '{"city"',
        },
        {
            "type": "response.function_call_arguments.delta",
            "item_id": item_id,
            "output_index": 0,
            "delta": ':"Paris"}',
        },
        {
            "type": "response.function_call_arguments.done",
            "item_id": item_id,
            "output_index": 0,
            "arguments": '{"city":"Paris"}',
        },
        {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {
                "type": "function_call",
                "id": item_id,
                "call_id": call_id,
                "name": "get_weather",
                "arguments": '{"city":"Paris"}',
                "status": "completed",
            },
        },
        {
            "type": "response.completed",
            "response": {
                "id": "resp_2",
                "model": "gpt-6.1-sol",
                "status": "completed",
                "usage": {"input_tokens": 23, "output_tokens": 9, "total_tokens": 32},
            },
        },
    )


@pytest.fixture
def fake_home_env(monkeypatch, tmp_path):
    """Fake HOME with no Machina store and no Codex CLI credentials."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("MACHINA_CHATGPT_AUTH_FILE", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("MACHINA_HOME", raising=False)
    monkeypatch.delenv("MACHINA_MACHINA_HOME", raising=False)
    from machina.core.config import config

    monkeypatch.setattr(config, "machina_home", "", raising=False)
    return home


@pytest.fixture
async def make(monkeypatch, tmp_path):
    """Build providers whose token store is a real file under tmp_path."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("MACHINA_CHATGPT_AUTH_FILE", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("MACHINA_HOME", raising=False)
    monkeypatch.delenv("MACHINA_MACHINA_HOME", raising=False)
    from machina.core.config import config

    monkeypatch.setattr(config, "machina_home", "", raising=False)

    auth_file = tmp_path / "chatgpt.json"
    clients: list[httpx.AsyncClient] = []
    seen_requests: list[httpx.Request] = []

    def _provider(
        handler,
        *,
        model: str | None = "gpt-6.1-sol",
        tokens: dict | None = None,
        write_store: bool = True,
        reasoning_effort: str | None = None,
    ) -> ChatGPTProvider:
        if write_store:
            chatgpt_auth.save_tokens(tokens or _tokens(), auth_file)
        provider = ChatGPTProvider(
            model=model, reasoning_effort=reasoning_effort, auth_path=str(auth_file)
        )
        provider._rate_limiter = AsyncTokenBucket(rate=0, capacity=0)

        def recording_handler(request: httpx.Request) -> httpx.Response:
            seen_requests.append(request)
            return handler(request)

        client = httpx.AsyncClient(
            base_url=ChatGPTProvider.BASE_URL,
            transport=httpx.MockTransport(recording_handler),
            timeout=5.0,
        )
        clients.append(client)
        provider.client = client
        return provider

    yield SimpleNamespace(
        provider=_provider,
        requests=seen_requests,
        auth_file=auth_file,
        stored=lambda: chatgpt_auth.load_tokens(auth_file),
    )
    for client in clients:
        await client.aclose()


# ─── request shape + headers ────────────────────────────────────────────────


async def test_request_shape_and_headers(make):
    def handler(request: httpx.Request) -> httpx.Response:
        return _text_stream("hi", usage={"input_tokens": 3, "output_tokens": 1, "total_tokens": 4})

    provider = make.provider(handler)
    events = [event async for event in provider.stream_complete(MESSAGES)]

    assert len(make.requests) == 1
    request = make.requests[0]
    assert request.method == "POST"
    assert str(request.url) == f"{ChatGPTProvider.BASE_URL}/responses"
    assert request.headers["authorization"] == f"Bearer {make.stored()['access_token']}"
    assert request.headers["originator"] == "machina"
    assert request.headers["accept"] == "text/event-stream"
    assert request.headers["chatgpt-account-id"] == "acct-provider"
    assert request.headers["session_id"]
    assert request.headers["user-agent"].startswith("machina/")

    body = json.loads(request.content)
    assert body["model"] == "gpt-6.1-sol"
    assert body["instructions"] == "be brief"
    assert body["store"] is False
    assert body["stream"] is True
    # Codex backend rejects sampling knobs (live 2026-10-14: HTTP 400 on temperature).
    assert "temperature" not in body
    assert "max_output_tokens" not in body
    assert body["input"] == [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hello"}]}
    ]

    assert [event.type for event in events] == ["text", "done"]
    done = events[-1]
    assert isinstance(done.response, LLMResponse)
    assert done.response.content == "hi"
    assert done.response.usage == {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}


async def test_default_instructions_used_when_no_system_message(make):
    def handler(request: httpx.Request) -> httpx.Response:
        return _text_stream("ok", usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})

    provider = make.provider(handler)
    async for _ in provider.stream_complete([{"role": "user", "content": "hi"}]):
        pass
    body = json.loads(make.requests[0].content)
    assert body["instructions"] == ChatGPTProvider.DEFAULT_INSTRUCTIONS


async def test_tools_use_flat_responses_shape(make):
    def handler(request: httpx.Request) -> httpx.Response:
        return _tool_stream()

    provider = make.provider(handler)
    async for _ in provider.stream_complete(MESSAGES, tools=TOOLS):
        pass
    body = json.loads(make.requests[0].content)
    assert body["tools"] == [
        {
            "type": "function",
            "name": "get_weather",
            "description": "Look up current weather for a city",
            "parameters": TOOLS[0].parameters,
            "strict": False,
        }
    ]
    assert body["tool_choice"] == "auto"
    assert body["parallel_tool_calls"] is True


async def test_history_conversion_replays_tool_calls_and_outputs(make):
    def handler(request: httpx.Request) -> httpx.Response:
        return _text_stream(
            "done", usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}
        )

    provider = make.provider(handler)
    history = [
        {"role": "user", "content": "weather?"},
        {
            "role": "assistant",
            "content": "checking",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city":"Paris"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "sunny"},
    ]
    async for _ in provider.stream_complete(history):
        pass
    body = json.loads(make.requests[0].content)
    assert body["input"] == [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "weather?"}],
        },
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "get_weather",
            "arguments": '{"city":"Paris"}',
        },
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "checking"}],
        },
        {"type": "function_call_output", "call_id": "call_1", "output": "sunny"},
    ]


async def test_reasoning_effort_sent_only_when_set(make):
    def handler(request: httpx.Request) -> httpx.Response:
        return _text_stream("ok", usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})

    plain = make.provider(handler)
    async for _ in plain.stream_complete(MESSAGES):
        pass
    effort = make.provider(handler)
    async for _ in effort.stream_complete(MESSAGES, reasoning_effort="high"):
        pass
    await plain.close()
    await effort.close()
    assert "reasoning" not in json.loads(make.requests[0].content)
    assert json.loads(make.requests[1].content)["reasoning"] == {"effort": "high"}


# ─── multimodal user content (R3-F5) ────────────────────────────────────────


def test_multimodal_user_content_maps_to_responses_image_inputs():
    """Text + image blocks become ordered input_text/input_image parts.

    The Responses wire takes the image as a plain ``image_url`` string (never
    the chat-completions ``{"url": ...}`` object), so the attachment keeps its
    image semantics instead of being serialized into a text block.
    """
    _, items = ChatGPTProvider._to_responses_input(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What is in this image?"},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64,c3ludGhldGlj",
                            "detail": "high",
                        },
                    },
                    {"type": "text", "text": "And this one?"},
                    {"type": "image_url", "image_url": "https://example.test/second.png"},
                ],
            }
        ]
    )
    assert items == [
        {
            "type": "message",
            "role": "user",
            "content": [
                {"type": "input_text", "text": "What is in this image?"},
                {
                    "type": "input_image",
                    "image_url": "data:image/png;base64,c3ludGhldGlj",
                    "detail": "high",
                },
                {"type": "input_text", "text": "And this one?"},
                {"type": "input_image", "image_url": "https://example.test/second.png"},
            ],
        }
    ]


def test_plain_string_user_content_keeps_input_text_fallback():
    _, items = ChatGPTProvider._to_responses_input([{"role": "user", "content": "hello"}])
    assert items == [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hello"}]}
    ]


async def test_multimodal_payload_sends_a_real_input_image(make):
    """The outgoing request carries an input_image block, not text flattening."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _text_stream(
            "seen", usage={"input_tokens": 3, "output_tokens": 1, "total_tokens": 4}
        )

    provider = make.provider(handler)
    async for _ in provider.stream_complete(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What is in this image?"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,c3ludGhldGlj"},
                    },
                ],
            }
        ]
    ):
        pass
    body = json.loads(make.requests[0].content)
    assert body["input"][0]["content"] == [
        {"type": "input_text", "text": "What is in this image?"},
        {"type": "input_image", "image_url": "data:image/png;base64,c3ludGhldGlj"},
    ]


# ─── SSE assembly ───────────────────────────────────────────────────────────


async def test_stream_happy_path_yields_text_then_terminal(make):
    def handler(request: httpx.Request) -> httpx.Response:
        return _text_stream(
            "Hel", "lo", usage={"input_tokens": 12, "output_tokens": 4, "total_tokens": 16}
        )

    provider = make.provider(handler)
    events = [event async for event in provider.stream_complete(MESSAGES)]
    assert [event.content for event in events if event.type == "text"] == ["Hel", "lo"]
    assert events[-1].type == "done"
    assert events[-1].response.content == "Hello"
    assert events[-1].response.model == "gpt-6.1-sol"
    assert events[-1].response.usage == {
        "prompt_tokens": 12,
        "completion_tokens": 4,
        "total_tokens": 16,
    }


async def test_stream_tool_call_accumulates_arguments(make):
    def handler(request: httpx.Request) -> httpx.Response:
        return _tool_stream()

    provider = make.provider(handler)
    events = [event async for event in provider.stream_complete(MESSAGES, tools=TOOLS)]
    assert [event.type for event in events] == ["done"]
    response = events[-1].response
    assert response.usage == {"prompt_tokens": 23, "completion_tokens": 9, "total_tokens": 32}
    assert response.tool_calls == [
        {
            "id": "call_abc123",
            "type": "function",
            "function": {"name": "get_weather", "arguments": '{"city":"Paris"}'},
        }
    ]


async def test_stream_without_terminal_event_raises(make):
    truncated = _sse(
        {"type": "response.created", "response": {"id": "resp_1"}},
        {
            "type": "response.output_text.delta",
            "item_id": "msg_1",
            "output_index": 0,
            "delta": "partial",
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return truncated

    provider = make.provider(handler)
    with pytest.raises(ProviderError) as excinfo:
        async for _ in provider.stream_complete(MESSAGES):
            pass
    assert "without a terminal frame" in str(excinfo.value)
    assert "chatgpt" in str(excinfo.value)


async def test_error_event_raises_honestly(make):
    def handler(request: httpx.Request) -> httpx.Response:
        return _sse(
            {"type": "response.created", "response": {"id": "resp_1"}},
            {"type": "error", "code": "server_error", "message": "stream blew up"},
        )

    provider = make.provider(handler)
    with pytest.raises(ProviderError) as excinfo:
        async for _ in provider.stream_complete(MESSAGES):
            pass
    assert "stream blew up" in str(excinfo.value)
    assert "server_error" in str(excinfo.value)


async def test_response_failed_event_raises(make):
    def handler(request: httpx.Request) -> httpx.Response:
        return _sse(
            {
                "type": "response.failed",
                "response": {"status": "failed", "error": {"message": "upstream error"}},
            }
        )

    provider = make.provider(handler)
    with pytest.raises(ProviderError) as excinfo:
        async for _ in provider.stream_complete(MESSAGES):
            pass
    assert "upstream error" in str(excinfo.value)


async def test_response_incomplete_event_raises_with_reason(make):
    def handler(request: httpx.Request) -> httpx.Response:
        return _sse(
            {
                "type": "response.incomplete",
                "response": {
                    "status": "incomplete",
                    "incomplete_details": {"reason": "max_output_tokens"},
                },
            }
        )

    provider = make.provider(handler)
    with pytest.raises(ProviderError) as excinfo:
        async for _ in provider.stream_complete(MESSAGES):
            pass
    assert "max_output_tokens" in str(excinfo.value)


async def test_lifecycle_and_reasoning_deltas_are_not_text(make):
    def handler(request: httpx.Request) -> httpx.Response:
        return _sse(
            {"type": "response.created", "response": {"id": "resp_1"}},
            {"type": "response.in_progress"},
            {"type": "response.reasoning_summary_text.delta", "delta": "thinking hard"},
            {"type": "response.reasoning_text.delta", "delta": "more thinking"},
            {
                "type": "response.output_text.delta",
                "item_id": "m",
                "output_index": 0,
                "delta": "answer",
            },
            {"type": "response.completed", "response": {"id": "resp_1", "model": "gpt-6.1-sol"}},
        )

    provider = make.provider(handler)
    events = [event async for event in provider.stream_complete(MESSAGES)]
    assert [event.content for event in events if event.type == "text"] == ["answer"]


async def test_usage_falls_back_to_estimates_when_absent(make):
    def handler(request: httpx.Request) -> httpx.Response:
        return _text_stream("some answer without usage")

    provider = make.provider(handler)
    events = [event async for event in provider.stream_complete(MESSAGES)]
    usage = events[-1].response.usage
    assert usage["estimated"] is True
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]


async def test_complete_drains_stream_to_llm_response(make):
    def handler(request: httpx.Request) -> httpx.Response:
        return _text_stream(
            "full", usage={"input_tokens": 2, "output_tokens": 3, "total_tokens": 5}
        )

    provider = make.provider(handler)
    response = await provider.complete(MESSAGES)
    assert isinstance(response, LLMResponse)
    assert response.content == "full"
    assert response.usage["total_tokens"] == 5


# ─── 401 -> refresh -> retry once ───────────────────────────────────────────


async def test_401_refreshes_once_then_retries(make, monkeypatch):
    refreshed = _tokens(refresh="rt-2")
    refresh_calls: list[dict] = []

    def fake_refresh(tokens, **kwargs):
        refresh_calls.append(tokens)
        return refreshed

    monkeypatch.setattr(chatgpt_auth, "refresh", fake_refresh)
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] == 1:
            return httpx.Response(401, json={"detail": "expired"})
        return _text_stream(
            "after refresh", usage={"input_tokens": 5, "output_tokens": 2, "total_tokens": 7}
        )

    provider = make.provider(handler)
    stored_access = make.stored()["access_token"]
    events = [event async for event in provider.stream_complete(MESSAGES)]

    assert attempts["count"] == 2
    assert len(refresh_calls) == 1
    assert make.requests[0].headers["authorization"] == f"Bearer {stored_access}"
    assert make.requests[1].headers["authorization"] == f"Bearer {refreshed['access_token']}"
    assert events[-1].response.content == "after refresh"


async def test_401_twice_raises_typed_error_with_login_hint(make, monkeypatch):
    monkeypatch.setattr(chatgpt_auth, "refresh", lambda tokens, **kwargs: _tokens(refresh="rt-2"))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "expired"})

    provider = make.provider(handler)
    stored_access = make.stored()["access_token"]
    with pytest.raises(ChatGPTAuthError) as excinfo:
        async for _ in provider.stream_complete(MESSAGES):
            pass
    assert excinfo.value.code == "chatgpt_unauthorized"
    assert "codex login" in str(excinfo.value)
    assert stored_access not in str(excinfo.value)


async def test_401_without_refresh_token_never_retries(make, monkeypatch):
    calls = {"refresh": 0}

    def fake_refresh(tokens, **kwargs):  # pragma: no cover - must not be called
        calls["refresh"] += 1
        return tokens

    monkeypatch.setattr(chatgpt_auth, "refresh", fake_refresh)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "expired"})

    provider = make.provider(handler, tokens=_tokens(refresh=""))
    with pytest.raises(ChatGPTAuthError) as excinfo:
        async for _ in provider.stream_complete(MESSAGES):
            pass
    assert excinfo.value.code == "chatgpt_unauthorized"
    assert calls["refresh"] == 0


async def test_refresh_failure_surfaces_typed_with_hint(make, monkeypatch):
    def failing_refresh(tokens, **kwargs):
        raise ChatGPTAuthError(
            "ChatGPT token refresh failed (HTTP 401); run `codex login`", code="invalid_grant"
        )

    monkeypatch.setattr(chatgpt_auth, "refresh", failing_refresh)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "expired"})

    provider = make.provider(handler)
    with pytest.raises(ChatGPTAuthError) as excinfo:
        async for _ in provider.stream_complete(MESSAGES):
            pass
    assert excinfo.value.code == "invalid_grant"
    assert "codex login" in str(excinfo.value)


async def test_missing_credentials_raises_with_login_hint(make):
    provider = make.provider(lambda request: httpx.Response(500), write_store=False)
    with pytest.raises(ChatGPTAuthError) as excinfo:
        async for _ in provider.stream_complete(MESSAGES):
            pass
    assert excinfo.value.code == "chatgpt_auth_missing"
    assert "codex login" in str(excinfo.value)


async def test_429_maps_to_rate_limit_error(make):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"x-codex-primary-reset-at": "1792014849"},
            json={"detail": "usage limit"},
        )

    provider = make.provider(handler)
    with pytest.raises(RateLimitError) as excinfo:
        async for _ in provider.stream_complete(MESSAGES):
            pass
    assert excinfo.value.reset_at == 1792014849
    assert "credentials are still valid" in str(excinfo.value)
    assert excinfo.value.status_code == 429


# ─── factory / registration ─────────────────────────────────────────────────


async def test_factory_resolves_chatgpt_without_api_key(fake_home_env):
    provider = get_provider("chatgpt", model="chatgpt/gpt-6.1-sol")
    try:
        assert isinstance(provider, ChatGPTProvider)
        assert provider.model == "gpt-6.1-sol"
    finally:
        await provider.close()


async def test_factory_defaults_to_codex_catalog_model(fake_home_env):
    provider = get_provider("chatgpt")
    try:
        assert provider.model == ChatGPTProvider.DEFAULT_MODEL == "gpt-6.1-sol"
    finally:
        await provider.close()


def test_providers_tuple_includes_chatgpt():
    from machina.core.provider import PROVIDERS

    assert "chatgpt" in PROVIDERS


# ─── catalog-backed slug + effort validation ────────────────────────────────

CATALOG_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "model_catalog_chatgpt.json"


def _seed_live_catalog(monkeypatch):
    """Seed the in-process live catalog from the REAL 2026-10-14 fixture.

    Seeded under the account the fixture providers are signed in as
    (``acct-provider``): catalog validation is account-scoped since review
    round 6 R6-1, so the seed must match the provider's token store.
    """
    from machina.core.provider import catalog

    payload = json.loads(CATALOG_FIXTURE.read_text(encoding="utf-8"))
    models = catalog.parse_catalog(payload, source="live")
    monkeypatch.setattr(catalog, "fetch_live_catalog", lambda *args, **kwargs: models)
    catalog.refresh_live_catalog("token-1", "acct-provider", force=True)
    return catalog


async def test_unknown_model_rejected_against_live_catalog(make, monkeypatch):
    _seed_live_catalog(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        return _text_stream("should not be reached")

    provider = make.provider(handler)
    with pytest.raises(ValidationError) as excinfo:
        async for _ in provider.stream_complete(MESSAGES, model="gpt-9-future"):
            pass
    assert "gpt-9-future" in str(excinfo.value)
    assert "available: gpt-6.1-sol, gpt-6-luna" in str(excinfo.value)
    assert make.requests == []  # rejected before any network use
    await provider.close()


async def test_per_model_effort_validation_rejects_ultra(make, monkeypatch):
    _seed_live_catalog(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        return _text_stream("should not be reached")

    provider = make.provider(handler)
    with pytest.raises(ValidationError) as excinfo:
        async for _ in provider.stream_complete(MESSAGES, reasoning_effort="ultra"):
            pass
    message = str(excinfo.value)
    assert "supported: low, medium, high, xhigh, max" in message
    assert make.requests == []
    await provider.close()


async def test_catalog_efforts_reach_the_wire(make, monkeypatch):
    _seed_live_catalog(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return _text_stream("ok", usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})

    provider = make.provider(handler, model="gpt-6-luna")
    async for _ in provider.stream_complete(MESSAGES, reasoning_effort="max"):
        pass
    await provider.close()
    payload = json.loads(make.requests[-1].content)
    assert payload["model"] == "gpt-6-luna"
    assert payload["reasoning"] == {"effort": "max"}


async def test_config_effort_key_is_validated_per_model(make, monkeypatch):
    _seed_live_catalog(monkeypatch)
    from machina.core.config import config

    def handler(request: httpx.Request) -> httpx.Response:
        return _text_stream("ok", usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})

    monkeypatch.setattr(config, "_session_overrides", {"reasoning_effort": "xhigh"})
    provider = make.provider(handler)
    async for _ in provider.stream_complete(MESSAGES):
        pass
    await provider.close()
    assert json.loads(make.requests[-1].content)["reasoning"] == {"effort": "xhigh"}

    # The config layer itself refuses Codex-client-only values before the
    # provider ever sees them (config vocabulary: '', low..high, xhigh, max).
    monkeypatch.setattr(config, "_session_overrides", {"reasoning_effort": "ultra"})
    with pytest.raises(ValueError):
        config.get("reasoning_effort")


async def test_instance_default_effort_is_model_validated(make, monkeypatch):
    _seed_live_catalog(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return _text_stream("ok", usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})

    provider = make.provider(handler, reasoning_effort="max")
    async for _ in provider.stream_complete(MESSAGES):
        pass
    await provider.close()
    assert json.loads(make.requests[-1].content)["reasoning"] == {"effort": "max"}

    bad = make.provider(handler, reasoning_effort="ultra")
    with pytest.raises(ValidationError) as excinfo:
        async for _ in bad.stream_complete(MESSAGES):
            pass
    assert "supported: low, medium, high, xhigh, max" in str(excinfo.value)
    await bad.close()
