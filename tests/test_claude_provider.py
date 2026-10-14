"""Claude subscription provider: OAuth identity headers, 401 refresh, SSE.

Probes run the real transport (the full Anthropic Messages path) against
``httpx.MockTransport``; token resolution uses a real store under ``tmp_path``
with a fake HOME. No network in tests.
"""

from __future__ import annotations

import json
import time

import httpx
import pytest

from machina.core.exceptions import ValidationError
from machina.core.provider import ClaudeProvider, get_provider
from machina.core.provider.rate_limit import AsyncTokenBucket
from machina.security import claude_auth
from machina.security.claude_auth import ClaudeAuthError

MESSAGES = [
    {"role": "system", "content": "be brief"},
    {"role": "user", "content": "hello"},
]


def _tokens(*, access: str = "at-1", refresh: str = "rt-1") -> dict:
    return {
        "access_token": access,
        "refresh_token": refresh,
        "expires_at": time.time() + 3600,
        "scopes": claude_auth.SCOPES.split(),
        "subscription_type": "max",
    }


def _message_stream(*deltas: str, model: str = "claude-sonnet-4-5") -> httpx.Response:
    def sse(event: str, data: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(data)}\n\n"

    body = sse(
        "message_start",
        {
            "type": "message_start",
            "message": {"id": "msg_1", "model": model, "usage": {"input_tokens": 7}},
        },
    )
    body += sse(
        "content_block_start",
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    )
    for delta in deltas:
        body += sse(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": delta},
            },
        )
    body += sse("content_block_stop", {"type": "content_block_stop", "index": 0})
    body += sse(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 3},
        },
    )
    body += sse("message_stop", {"type": "message_stop"})
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


@pytest.fixture
def make(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("MACHINA_CLAUDE_AUTH_FILE", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("MACHINA_HOME", raising=False)
    monkeypatch.delenv("MACHINA_MACHINA_HOME", raising=False)
    from machina.core.config import config

    monkeypatch.setattr(config, "machina_home", "", raising=False)
    monkeypatch.setattr(claude_auth, "_read_claude_code_keychain", lambda: None)

    auth_file = tmp_path / "claude.json"
    seen: list[httpx.Request] = []

    def _provider(
        handler,
        *,
        model: str | None = "claude-sonnet-4-5",
        tokens: dict | None = None,
        write_store: bool = True,
    ) -> ClaudeProvider:
        if write_store:
            claude_auth.save_tokens(tokens or _tokens(), auth_file)

        def recording_handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return handler(request)

        # The real constructor builds the client; only its transport is swapped
        # so the OAuth headers/auth flow under test are the production ones.
        provider = ClaudeProvider(
            model=model,
            auth_path=str(auth_file),
            transport=httpx.MockTransport(recording_handler),
        )
        provider._rate_limiter = AsyncTokenBucket(rate=0, capacity=0)
        return provider

    _provider.requests = seen  # type: ignore[attr-defined]
    _provider.auth_file = auth_file  # type: ignore[attr-defined]
    return _provider


async def test_oauth_identity_headers_and_no_dual_auth(make):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/messages"
        assert request.headers["authorization"] == "Bearer at-1"
        assert "x-api-key" not in request.headers
        beta = request.headers["anthropic-beta"]
        assert "claude-code-20250219" in beta and "oauth-2025-04-20" in beta
        assert request.headers["user-agent"].startswith("claude-code/")
        assert request.headers["x-app"] == "cli"
        body = json.loads(request.content)
        assert body["model"] == "claude-sonnet-4-5"
        return _message_stream("machina claude ok")

    provider = make(handler)
    events = [event async for event in provider.stream_complete(MESSAGES)]
    assert [event.type for event in events] == ["text", "done"]
    done = events[-1].response
    assert done.content == "machina claude ok"
    assert done.usage["prompt_tokens"] == 7
    assert done.usage["completion_tokens"] == 3
    await provider.close()


async def test_401_refreshes_once_and_replays(make, monkeypatch):
    refresh_calls: list[dict] = []

    def fake_refresh(tokens, *, path=None, **kwargs):
        refresh_calls.append(tokens)
        updated = dict(tokens)
        updated["access_token"] = "at-fresh"
        updated["last_refresh"] = "now"
        claude_auth.save_tokens(updated, path)
        return claude_auth.load_tokens(path)

    monkeypatch.setattr(claude_auth, "refresh", fake_refresh)
    attempts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request.headers["authorization"])
        if len(attempts) == 1:
            return httpx.Response(401, json={"error": {"type": "authentication_error"}})
        return _message_stream("after refresh")

    provider = make(handler)
    events = [event async for event in provider.stream_complete(MESSAGES)]
    assert events[-1].response.content == "after refresh"
    assert attempts == ["Bearer at-1", "Bearer at-fresh"]
    assert len(refresh_calls) == 1
    assert claude_auth.load_tokens(make.auth_file)["access_token"] == "at-fresh"
    await provider.close()


async def test_401_without_refresh_token_is_typed(make):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"type": "authentication_error"}})

    provider = make(handler, tokens={"access_token": "at-only", "expires_at": time.time() + 3600})
    with pytest.raises(ClaudeAuthError) as excinfo:
        async for _ in provider.stream_complete(MESSAGES):
            pass
    assert excinfo.value.code == "claude_unauthorized"
    assert "at-only" not in str(excinfo.value)
    await provider.close()


async def test_missing_credentials_is_typed_with_hint(make):
    provider = make(lambda request: httpx.Response(500), write_store=False)
    with pytest.raises(ClaudeAuthError) as excinfo:
        async for _ in provider.stream_complete(MESSAGES):
            pass
    assert excinfo.value.code == "claude_auth_missing"
    assert "machina auth claude login" in str(excinfo.value)
    await provider.close()


def test_factory_builds_claude_provider_and_validates_model():
    provider = get_provider("claude", model="claude/claude-opus-4-6")
    assert isinstance(provider, ClaudeProvider)
    assert provider.model == "claude-opus-4-6"
    with pytest.raises(ValidationError):
        get_provider("claude", model="gpt-4o")


def test_default_model_is_a_claude_id():
    assert ClaudeProvider.DEFAULT_MODEL.startswith("claude")
