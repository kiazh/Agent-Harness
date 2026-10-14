"""Gemini provider: Cloud Code Assist wire shape, onboarding, SSE discipline.

Probes run the real transport against ``httpx.MockTransport``; token
resolution uses a real store written under ``tmp_path`` (with a fake HOME so
the gemini-cli fallback stays hermetic). No network in tests.
"""

from __future__ import annotations

import json
import time

import httpx
import pytest

from machina.core.exceptions import ProviderError, RateLimitError
from machina.core.models import LLMResponse, ToolDefinition
from machina.core.provider import GeminiProvider
from machina.core.provider.rate_limit import AsyncTokenBucket
from machina.security import gemini_auth
from machina.security.gemini_auth import GeminiAuthError

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


def _tokens(*, access: str = "at-1", refresh: str = "rt-1", project: str | None = "proj-1") -> dict:
    tokens = {
        "access_token": access,
        "refresh_token": refresh,
        "expires_at": time.time() + 3600,
    }
    if project:
        tokens["project"] = project
    return tokens


def _sse(*chunks: dict) -> httpx.Response:
    body = "".join(f"data: {json.dumps({'response': chunk})}\n\n" for chunk in chunks)
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


def _text_chunk(text: str, *, model: str | None = None) -> dict:
    chunk: dict = {"candidates": [{"content": {"parts": [{"text": text}]}}]}
    if model:
        chunk["modelVersion"] = model
    return chunk


def _finish_chunk(*, reason: str = "STOP", usage: dict | None = None) -> dict:
    chunk: dict = {
        "candidates": [{"content": {"parts": []}, "finishReason": reason}],
    }
    if usage is not None:
        chunk["usageMetadata"] = usage
    return chunk


@pytest.fixture
def make(monkeypatch, tmp_path):
    """Build providers whose token store is a real file under tmp_path."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("MACHINA_GEMINI_AUTH_FILE", raising=False)
    monkeypatch.delenv("MACHINA_GEMINI_IMPORT_FILE", raising=False)
    monkeypatch.delenv("MACHINA_HOME", raising=False)
    monkeypatch.delenv("MACHINA_MACHINA_HOME", raising=False)
    from machina.core.config import config

    monkeypatch.setattr(config, "machina_home", "", raising=False)

    auth_file = tmp_path / "gemini.json"
    seen_requests: list[httpx.Request] = []

    def _provider(
        handler,
        *,
        model: str | None = "gemini-2.5-pro",
        tokens: dict | None = None,
        write_store: bool = True,
        onboard_delay: float = 0.0,
    ) -> GeminiProvider:
        if write_store:
            gemini_auth.save_tokens(tokens or _tokens(), auth_file)
        provider = GeminiProvider(model=model, auth_path=str(auth_file))
        provider.ONBOARD_POLL_DELAY = onboard_delay
        provider._rate_limiter = AsyncTokenBucket(rate=0, capacity=0)

        def recording_handler(request: httpx.Request) -> httpx.Response:
            seen_requests.append(request)
            return handler(request)

        provider.client = httpx.AsyncClient(
            base_url=GeminiProvider.BASE_URL,
            headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
            transport=httpx.MockTransport(recording_handler),
        )
        return provider

    _provider.requests = seen_requests  # type: ignore[attr-defined]
    _provider.auth_file = auth_file  # type: ignore[attr-defined]
    return _provider


async def test_text_stream_happy_path(make):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1internal:streamGenerateContent"
        assert request.url.params["alt"] == "sse"
        assert request.headers["authorization"] == "Bearer at-1"
        body = json.loads(request.content)
        assert body["model"] == "gemini-2.5-pro"
        assert body["project"] == "proj-1"
        assert body["user_prompt_id"]
        inner = body["request"]
        assert inner["systemInstruction"]["parts"][0]["text"] == "be brief"
        assert inner["contents"][0] == {"role": "user", "parts": [{"text": "hello"}]}
        assert inner["generationConfig"]["temperature"] == 0.7
        return _sse(
            _text_chunk("machina ", model="gemini-2.5-pro-001"),
            _text_chunk("gemini ok"),
            _finish_chunk(
                usage={
                    "promptTokenCount": 11,
                    "candidatesTokenCount": 4,
                    "totalTokenCount": 15,
                }
            ),
        )

    provider = make(handler)
    events = [event async for event in provider.stream_complete(MESSAGES)]
    assert [event.type for event in events] == ["text", "text", "done"]
    done = events[-1].response
    assert isinstance(done, LLMResponse)
    assert done.content == "machina gemini ok"
    assert done.model == "gemini-2.5-pro-001"
    assert done.usage["prompt_tokens"] == 11
    assert done.usage["completion_tokens"] == 4
    await provider.close()


async def test_tool_call_stream(make):
    def handler(request: httpx.Request) -> httpx.Response:
        return _sse(
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "functionCall": {
                                        "name": "get_weather",
                                        "args": {"city": "Paris"},
                                    }
                                }
                            ]
                        }
                    }
                ]
            },
            _finish_chunk(),
        )

    provider = make(handler)
    events = [event async for event in provider.stream_complete(MESSAGES, tools=TOOLS)]
    done = events[-1].response
    assert done.tool_calls
    call = done.tool_calls[0]
    assert call["function"]["name"] == "get_weather"
    assert json.loads(call["function"]["arguments"]) == {"city": "Paris"}
    await provider.close()


async def test_tools_payload_and_thought_parts_filtered(make):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        decls = body["request"]["tools"][0]["functionDeclarations"]
        assert decls[0]["name"] == "get_weather"
        return _sse(
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"text": "hidden reasoning", "thought": True},
                                {"text": "visible"},
                            ]
                        }
                    }
                ]
            },
            _finish_chunk(),
        )

    provider = make(handler)
    events = [event async for event in provider.stream_complete(MESSAGES, tools=TOOLS)]
    assert "".join(e.content for e in events if e.type == "text") == "visible"
    await provider.close()


async def test_truncated_stream_is_a_typed_error(make):
    def handler(request: httpx.Request) -> httpx.Response:
        return _sse(_text_chunk("partial"))

    provider = make(handler)
    with pytest.raises(ProviderError) as excinfo:
        async for _ in provider.stream_complete(MESSAGES):
            pass
    assert "without a terminal frame" in str(excinfo.value)
    await provider.close()


async def test_401_refreshes_once_and_retries(make, monkeypatch):
    refresh_calls: list[dict] = []

    def fake_refresh(tokens, *, path=None, **kwargs):
        refresh_calls.append(tokens)
        updated = dict(tokens)
        updated["access_token"] = "at-fresh"
        updated["last_refresh"] = "now"
        gemini_auth.save_tokens(updated, path)
        return gemini_auth.load_tokens(path)

    monkeypatch.setattr(gemini_auth, "refresh", fake_refresh)
    attempts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request.headers["authorization"])
        if len(attempts) == 1:
            return httpx.Response(401, json={"error": {"code": 401, "message": "expired"}})
        return _sse(_text_chunk("ok"), _finish_chunk())

    provider = make(handler, write_store=False)
    gemini_auth.save_tokens(_tokens(access="at-stale", project="proj-1"), make.auth_file)
    events = [event async for event in provider.stream_complete(MESSAGES)]
    assert events[-1].response.content == "ok"
    assert attempts == ["Bearer at-stale", "Bearer at-fresh"]
    assert len(refresh_calls) == 1
    assert gemini_auth.load_tokens(make.auth_file)["access_token"] == "at-fresh"
    await provider.close()


async def test_401_without_refresh_token_is_typed(make):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"code": 401}})

    provider = make(handler, write_store=False)
    gemini_auth.save_tokens(
        {"access_token": "at-1", "expires_at": time.time() + 3600, "project": "proj-1"},
        make.auth_file,
    )
    with pytest.raises(GeminiAuthError) as excinfo:
        async for _ in provider.stream_complete(MESSAGES):
            pass
    assert excinfo.value.code == "gemini_unauthorized"
    await provider.close()


async def test_429_is_rate_limit_with_valid_credentials(make):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"code": 429}})

    provider = make(handler)
    with pytest.raises(RateLimitError):
        async for _ in provider.stream_complete(MESSAGES):
            pass
    await provider.close()


async def test_not_eligible_reports_google_reason_and_deprecation(make):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1internal:loadCodeAssist"
        return httpx.Response(
            200,
            json={
                "allowedTiers": [],
                "ineligibleTiers": [
                    {"reasonCode": "INELIGIBLE_TIER", "reasonMessage": "consumer tier retired"}
                ],
            },
        )

    provider = make(handler, write_store=False)
    gemini_auth.save_tokens(_tokens(project=None), make.auth_file)
    with pytest.raises(GeminiAuthError) as excinfo:
        async for _ in provider.stream_complete(MESSAGES):
            pass
    assert excinfo.value.code == "gemini_not_eligible"
    assert "INELIGIBLE_TIER" in str(excinfo.value)
    assert "2026-06-18" in str(excinfo.value)
    await provider.close()


async def test_onboarding_runs_and_persists_project(make):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1internal:loadCodeAssist":
            return httpx.Response(
                200,
                json={
                    "allowedTiers": [
                        {
                            "id": "standard",
                            "isDefault": True,
                            "userDefinedCloudaicompanionProject": False,
                        }
                    ]
                },
            )
        if request.url.path == "/v1internal:onboardUser":
            body = json.loads(request.content)
            assert body["tierId"] == "standard"
            return httpx.Response(200, json={"name": "operations/onboard-1", "done": False})
        if request.url.path == "/v1internal/operations/onboard-1":
            return httpx.Response(
                200,
                json={"done": True, "response": {"cloudaicompanionProject": {"id": "proj-new"}}},
            )
        assert request.url.path == "/v1internal:streamGenerateContent"
        body = json.loads(request.content)
        assert body["project"] == "proj-new"
        return _sse(_text_chunk("onboarded"), _finish_chunk())

    provider = make(handler, write_store=False)
    gemini_auth.save_tokens(_tokens(project=None), make.auth_file)
    events = [event async for event in provider.stream_complete(MESSAGES)]
    assert events[-1].response.content == "onboarded"
    assert gemini_auth.load_tokens(make.auth_file)["project"] == "proj-new"
    await provider.close()


async def test_stored_project_is_reused_without_loadcodeassist(make):
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return _sse(_text_chunk("ok"), _finish_chunk())

    provider = make(handler, tokens=_tokens(project="proj-cached"))
    async for _ in provider.stream_complete(MESSAGES):
        pass
    assert paths == ["/v1internal:streamGenerateContent"]
    await provider.close()


async def test_complete_drains_stream(make):
    def handler(request: httpx.Request) -> httpx.Response:
        return _sse(_text_chunk("dra"), _text_chunk("ined"), _finish_chunk())

    provider = make(handler)
    response = await provider.complete(MESSAGES)
    assert response.content == "drained"
    await provider.close()


async def test_missing_credentials_is_typed_with_hint(make):
    provider = make(lambda request: httpx.Response(500), write_store=False)
    with pytest.raises(GeminiAuthError) as excinfo:
        async for _ in provider.stream_complete(MESSAGES):
            pass
    assert excinfo.value.code == "gemini_auth_missing"
    assert "machina auth gemini login" in str(excinfo.value)
    await provider.close()


def test_factory_builds_gemini_provider():
    from machina.core.provider import get_provider

    provider = get_provider("gemini", model="gemini-2.5-flash")
    assert isinstance(provider, GeminiProvider)
    assert provider.model == "gemini-2.5-flash"
