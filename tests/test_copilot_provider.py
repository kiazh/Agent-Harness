"""Copilot provider: wire headers, SSE discipline, typed failures.

Probes run the real transport against ``httpx.MockTransport``; token
resolution uses a real store under ``tmp_path`` with a fake HOME (gh probing
disabled). No network in tests.
"""

from __future__ import annotations

import json

import httpx
import pytest

from machina.core.exceptions import ProviderError, RateLimitError
from machina.core.models import LLMResponse, ToolDefinition
from machina.core.provider import CopilotProvider, get_provider
from machina.core.provider.rate_limit import AsyncTokenBucket
from machina.security import copilot_auth
from machina.security.copilot_auth import CopilotAuthError

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


def _sse(*chunks: dict) -> httpx.Response:
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


def _text_chunk(text: str) -> dict:
    return {"choices": [{"index": 0, "delta": {"content": text}}]}


def _finish_chunk(*, reason: str = "stop", usage: dict | None = None) -> dict:
    chunk: dict = {"choices": [{"index": 0, "delta": {}, "finish_reason": reason}]}
    if usage is not None:
        chunk["usage"] = usage
    return chunk


@pytest.fixture
def make(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("MACHINA_COPILOT_AUTH_FILE", raising=False)
    monkeypatch.delenv("MACHINA_COPILOT_BASE_URL", raising=False)
    monkeypatch.delenv("MACHINA_HOME", raising=False)
    monkeypatch.delenv("MACHINA_MACHINA_HOME", raising=False)
    monkeypatch.setattr(copilot_auth, "_probe_gh_token", lambda: None)
    from machina.core.config import config

    monkeypatch.setattr(config, "machina_home", "", raising=False)

    auth_file = tmp_path / "copilot.json"
    seen: list[httpx.Request] = []

    def _provider(
        handler,
        *,
        model: str | None = "gpt-6.1-sol",
        token: str | None = "gho_test",
        write_store: bool = True,
    ) -> CopilotProvider:
        if write_store:
            copilot_auth.save_tokens({"token": token or "gho_test"}, auth_file)

        def recording_handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return handler(request)

        provider = CopilotProvider(
            model=model,
            auth_path=str(auth_file),
            transport=httpx.MockTransport(recording_handler),
        )
        provider._rate_limiter = AsyncTokenBucket(rate=0, capacity=0)
        return provider

    _provider.requests = seen  # type: ignore[attr-defined]
    _provider.auth_file = auth_file  # type: ignore[attr-defined]
    return _provider


async def test_wire_headers_and_text_stream(make):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/chat/completions"
        assert request.headers["authorization"] == "Bearer gho_test"
        assert request.headers["copilot-integration-id"] == "vscode-chat"
        assert request.headers["editor-version"].startswith("vscode/")
        assert request.headers["x-initiator"] == "agent"
        assert request.headers["user-agent"].startswith("GitHubCopilotChat/")
        body = json.loads(request.content)
        assert body["model"] == "gpt-6.1-sol"
        assert body["stream"] is True
        assert body["messages"][1] == {"role": "user", "content": "hello"}
        return _sse(
            _text_chunk("machina "),
            _text_chunk("copilot ok"),
            _finish_chunk(usage={"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12}),
        )

    provider = make(handler)
    events = [event async for event in provider.stream_complete(MESSAGES)]
    assert [event.type for event in events] == ["text", "text", "done"]
    done = events[-1].response
    assert isinstance(done, LLMResponse)
    assert done.content == "machina copilot ok"
    assert done.usage["prompt_tokens"] == 9
    await provider.close()


async def test_tool_call_accumulation(make):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["tools"][0]["function"]["name"] == "get_weather"
        return _sse(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "function": {"name": "get_weather", "arguments": ""},
                                }
                            ]
                        },
                    }
                ]
            },
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [{"index": 0, "function": {"arguments": '{"city"'}}]
                        },
                    }
                ]
            },
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [{"index": 0, "function": {"arguments": ':"Paris"}'}}]
                        },
                    }
                ]
            },
            _finish_chunk(reason="tool_calls"),
        )

    provider = make(handler)
    events = [event async for event in provider.stream_complete(MESSAGES, tools=TOOLS)]
    calls = events[-1].response.tool_calls
    assert len(calls) == 1
    assert calls[0]["id"] == "call_1"
    assert calls[0]["function"]["name"] == "get_weather"
    assert json.loads(calls[0]["function"]["arguments"]) == {"city": "Paris"}
    await provider.close()


async def test_truncated_stream_is_a_typed_error(make):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=f"data: {json.dumps(_text_chunk('partial'))}\n\n",
            headers={"content-type": "text/event-stream"},
        )

    provider = make(handler)
    with pytest.raises(ProviderError) as excinfo:
        async for _ in provider.stream_complete(MESSAGES):
            pass
    assert "without a terminal frame" in str(excinfo.value)
    await provider.close()


async def test_401_is_typed_with_login_hint(make):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "bad credentials"}})

    provider = make(handler)
    with pytest.raises(CopilotAuthError) as excinfo:
        async for _ in provider.stream_complete(MESSAGES):
            pass
    assert excinfo.value.code == "copilot_unauthorized"
    assert "gh auth login" in str(excinfo.value)
    assert "gho_test" not in str(excinfo.value)
    await provider.close()


async def test_model_not_supported_is_typed_with_menu_hint(make):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": "The requested model is not supported.",
                    "code": "model_not_supported",
                }
            },
        )

    provider = make(handler, model="gpt-6.1-sol")
    with pytest.raises(ProviderError) as excinfo:
        async for _ in provider.stream_complete(MESSAGES):
            pass
    assert "not served for this GitHub account" in str(excinfo.value)
    assert "GET /models" in str(excinfo.value)
    await provider.close()


async def test_403_mentions_the_copilot_seat(make):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    provider = make(handler)
    with pytest.raises(ProviderError) as excinfo:
        async for _ in provider.stream_complete(MESSAGES):
            pass
    assert "Copilot seat" in str(excinfo.value)
    await provider.close()


async def test_429_is_rate_limit_with_valid_credentials(make):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="slow down")

    provider = make(handler)
    with pytest.raises(RateLimitError):
        async for _ in provider.stream_complete(MESSAGES):
            pass
    await provider.close()


async def test_missing_credentials_is_typed_with_hint(make):
    provider = make(lambda request: httpx.Response(500), write_store=False)
    with pytest.raises(CopilotAuthError) as excinfo:
        async for _ in provider.stream_complete(MESSAGES):
            pass
    assert excinfo.value.code == "copilot_auth_missing"
    assert "machina auth copilot login" in str(excinfo.value)
    await provider.close()


async def test_complete_drains_stream(make):
    def handler(request: httpx.Request) -> httpx.Response:
        return _sse(_text_chunk("dra"), _text_chunk("ined"), _finish_chunk())

    provider = make(handler)
    response = await provider.complete(MESSAGES)
    assert response.content == "drained"
    await provider.close()


def test_factory_builds_copilot_provider_and_strips_prefix():
    provider = get_provider("copilot", model="copilot/gpt-6.1-sol")
    assert isinstance(provider, CopilotProvider)
    assert provider.model == "gpt-6.1-sol"


async def test_stream_without_usage_marks_estimate(make):
    def handler(request: httpx.Request) -> httpx.Response:
        return _sse(_text_chunk("x"), _finish_chunk())

    provider = make(handler)
    events = [event async for event in provider.stream_complete(MESSAGES)]
    usage = events[-1].response.usage
    assert usage.get("estimated") is True
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]
    await provider.close()
