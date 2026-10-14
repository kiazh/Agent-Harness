"""Free-model routing: the generic ``custom`` OpenAI-compatible provider and
the ``opencode`` (OpenCode Zen) preset.

Transport probes run the real ``DirectProvider`` machinery against
``httpx.MockTransport`` — no network, deterministic. The Zen base URL pinned
here was probed live on 2026-10-14; see
``docs/verification/proofs/free_models_2026-10-14/`` for the raw evidence
(both ``/inference/v1`` and ``/zen/v1`` served an identical 85-model catalog).
"""

from __future__ import annotations

import json

import httpx
import pytest

from machina.core import runtime as runtime_mod
from machina.core.config import DEFAULTS, LEGACY_ENV_VARS, SECRET_KEYS, Config, config
from machina.core.exceptions import ProviderError, ValidationError
from machina.core.provider import (
    PROVIDER_SPECS,
    PROVIDERS,
    AsyncTokenBucket,
    DirectProvider,
    get_provider,
)

CUSTOM_BASE = "http://127.0.0.1:1234/v1"  # user's local server, any port
MESSAGES = [{"role": "user", "content": "hello from the free-models lane"}]


def _openai_body(content: str = "local answer", model: str = "local-model") -> dict:
    return {
        "model": model,
        "choices": [{"message": {"content": content, "tool_calls": []}}],
        "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
    }


def _sse(*chunks: dict) -> httpx.Response:
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
    return httpx.Response(200, text=body + "data: [DONE]\n\n")


def _set_custom(monkeypatch, base_url: str = CUSTOM_BASE, api_key: str = "") -> None:
    """Pin the custom-provider config through highest-precedence overrides."""
    monkeypatch.setattr(
        config,
        "_session_overrides",
        {"custom_base_url": base_url, "custom_api_key": api_key},
    )


async def _custom_over_transport(
    monkeypatch, handler, model: str = "local-model", api_key: str = ""
):
    """Build through the real factory, then swap in a MockTransport client.

    The replacement client inherits the factory client's headers, so the
    authorization assertions exercise the real construction path.
    """
    _set_custom(monkeypatch, api_key=api_key)
    provider = get_provider("custom", model=model)
    headers = provider.client.headers
    await provider.client.aclose()
    provider._rate_limiter = AsyncTokenBucket(rate=0, capacity=0)
    provider.client = httpx.AsyncClient(
        base_url=CUSTOM_BASE,
        headers=headers,
        transport=httpx.MockTransport(handler),
        timeout=5.0,
    )
    return provider


# ─── generic custom provider: factory + config resolution ───────────────────


def test_custom_requires_base_url(monkeypatch):
    _set_custom(monkeypatch, base_url="")
    with pytest.raises(ValidationError, match="custom_base_url"):
        get_provider("custom", model="local-model")


def test_custom_requires_model(monkeypatch):
    _set_custom(monkeypatch)
    with pytest.raises(ValidationError, match="model"):
        get_provider("custom")


async def test_custom_builds_direct_provider_from_config(monkeypatch):
    _set_custom(monkeypatch)
    provider = get_provider("custom", model="qwen2.5-7b")
    try:
        assert isinstance(provider, DirectProvider)
        assert provider.spec.name == "custom"
        assert provider.model == "qwen2.5-7b"
        # Keyless local server: no bearer, no key error.
        assert provider.api_key == ""
        assert "authorization" not in provider.client.headers
        assert str(provider.client.base_url).rstrip("/") == CUSTOM_BASE
    finally:
        await provider.close()


async def test_custom_key_present_sends_bearer_header(monkeypatch):
    _set_custom(monkeypatch, api_key="local-secret")
    provider = get_provider("custom", model="qwen2.5-7b")
    try:
        assert provider.client.headers["authorization"] == "Bearer local-secret"
    finally:
        await provider.close()


def test_custom_is_not_in_the_fixed_vendor_table():
    assert "custom" in PROVIDERS
    assert "custom" not in PROVIDER_SPECS


# ─── generic custom provider: wire behavior ─────────────────────────────────


async def test_custom_keyless_complete_omits_authorization(monkeypatch):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_openai_body())

    provider = await _custom_over_transport(monkeypatch, handler)
    try:
        result = await provider.complete(MESSAGES)
        assert result.content == "local answer"
        assert len(seen) == 1
        assert "authorization" not in seen[0].headers
        assert json.loads(seen[0].content)["model"] == "local-model"
    finally:
        await provider.close()


async def test_custom_with_key_sends_bearer_on_the_wire(monkeypatch):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_openai_body())

    provider = await _custom_over_transport(monkeypatch, handler, api_key="local-secret")
    try:
        await provider.complete(MESSAGES)
        assert seen[0].headers["authorization"] == "Bearer local-secret"
    finally:
        await provider.close()


async def test_custom_stream_completes_on_terminal_marker(monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return _sse(
            {"model": "local-model", "choices": [{"delta": {"content": "hel"}}]},
            {"model": "local-model", "choices": [{"delta": {"content": "lo"}}]},
        )

    provider = await _custom_over_transport(monkeypatch, handler)
    try:
        events = [event async for event in provider.stream_complete(MESSAGES)]
        assert events[-1].type == "done"
        assert events[-1].response.content == "hello"
        # Conservative default: the custom endpoint did not document
        # stream_options.include_usage, so it is never sent.
        assert "stream_options" not in captured
    finally:
        await provider.close()


async def test_custom_stream_clean_eof_without_terminal_marker_raises(monkeypatch):
    chunk = {
        "model": "local-model",
        "choices": [{"delta": {"content": "partial"}, "finish_reason": None}],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=f"data: {json.dumps(chunk)}\n\n")

    provider = await _custom_over_transport(monkeypatch, handler)
    try:
        with pytest.raises(ProviderError, match="terminal"):
            _ = [event async for event in provider.stream_complete(MESSAGES)]
    finally:
        await provider.close()


async def test_custom_401_complete_is_honest_single_attempt(monkeypatch):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            401, json={"error": {"message": "missing or invalid API key", "type": "auth"}}
        )

    provider = await _custom_over_transport(monkeypatch, handler)
    try:
        with pytest.raises(httpx.HTTPStatusError) as error:
            await provider.complete(MESSAGES)
        assert error.value.response.status_code == 401
        # No refresh flow and no retry — exactly one honest attempt.
        assert len(requests) == 1
    finally:
        await provider.close()


async def test_custom_401_stream_is_honest_single_attempt(monkeypatch):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    provider = await _custom_over_transport(monkeypatch, handler)
    try:
        with pytest.raises(httpx.HTTPStatusError) as error:
            _ = [event async for event in provider.stream_complete(MESSAGES)]
        assert error.value.response.status_code == 401
        assert len(requests) == 1
    finally:
        await provider.close()


# ─── opencode preset (OpenCode Zen) ─────────────────────────────────────────


def test_opencode_spec_pins_the_probed_live_zen_base():
    spec = PROVIDER_SPECS["opencode"]
    assert spec.base_url == "https://opencode.ai/inference/v1"
    assert spec.api_key_env == "OPENCODE_API_KEY"
    assert spec.config_key == "opencode_api_key"
    assert spec.default_model == "big-pickle"
    assert spec.requires_key is True


async def test_opencode_resolves_through_the_factory(monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "test-zen-key")
    provider = get_provider("opencode")
    try:
        assert isinstance(provider, DirectProvider)
        assert provider.model == "big-pickle"
        assert provider.api_key == "test-zen-key"
        assert str(provider.client.base_url).rstrip("/") == "https://opencode.ai/inference/v1"
    finally:
        await provider.close()


def test_opencode_requires_key(monkeypatch):
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    with pytest.raises(ValidationError, match="OPENCODE_API_KEY"):
        get_provider("opencode", model="big-pickle")


async def test_opencode_base_url_is_overridable_without_mutating_the_table(monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "test-zen-key")
    monkeypatch.setattr(
        config, "_session_overrides", {"opencode_base_url": "https://opencode.ai/zen/v1"}
    )
    provider = get_provider("opencode", model="big-pickle")
    try:
        assert str(provider.client.base_url).rstrip("/") == "https://opencode.ai/zen/v1"
        # The shared spec table is untouched — the override is a copy.
        assert PROVIDER_SPECS["opencode"].base_url == "https://opencode.ai/inference/v1"
    finally:
        await provider.close()


# ─── runtime status honesty ─────────────────────────────────────────────────


def test_status_custom_ready_with_base_url_even_keyless(monkeypatch):
    monkeypatch.setattr(
        config,
        "_session_overrides",
        {"provider": "custom", "custom_base_url": CUSTOM_BASE, "custom_api_key": ""},
    )
    report = runtime_mod._detect_providers()
    assert report["chat_provider"] == "ready"
    assert report["chat_provider_selected"] == "custom"
    assert report["chat_provider_reason"] == "no key (local endpoint)"


def test_status_custom_unavailable_without_base_url(monkeypatch):
    monkeypatch.setattr(config, "_session_overrides", {"provider": "custom", "custom_base_url": ""})
    report = runtime_mod._detect_providers()
    assert report["chat_provider"] == "unavailable"
    assert report["chat_provider_reason"] == "custom_base_url not set"


def test_status_opencode_ready_only_with_key(monkeypatch):
    monkeypatch.setattr(config, "_session_overrides", {"provider": "opencode"})
    monkeypatch.setenv("OPENCODE_API_KEY", "test-zen-key")
    report = runtime_mod._detect_providers()
    assert report["chat_provider"] == "ready"
    assert report["chat_provider_selected"] == "opencode"
    assert report["chat_provider_reason"] == ""

    monkeypatch.delenv("OPENCODE_API_KEY")
    report = runtime_mod._detect_providers()
    assert report["chat_provider"] == "unavailable"
    assert report["chat_provider_reason"] == "OPENCODE_API_KEY not set"


# ─── config plumbing: DEFAULTS + dataclass + env mapping ────────────────────


def test_new_keys_registered_in_all_three_structures():
    assert DEFAULTS["custom_base_url"] == ""
    assert DEFAULTS["custom_api_key"] == ""
    assert DEFAULTS["opencode_api_key"] == ""
    assert DEFAULTS["opencode_base_url"] == ""
    assert LEGACY_ENV_VARS["custom_base_url"] == "CUSTOM_BASE_URL"
    assert LEGACY_ENV_VARS["custom_api_key"] == "CUSTOM_API_KEY"
    assert LEGACY_ENV_VARS["opencode_api_key"] == "OPENCODE_API_KEY"
    assert LEGACY_ENV_VARS["opencode_base_url"] == "OPENCODE_BASE_URL"
    assert "custom_api_key" in SECRET_KEYS
    assert "opencode_api_key" in SECRET_KEYS
    # Endpoints are not secrets: they persist in config.yaml unmasked.
    assert "custom_base_url" not in SECRET_KEYS
    assert "opencode_base_url" not in SECRET_KEYS

    defaults = Config()
    assert defaults.custom_base_url == ""
    assert defaults.custom_api_key == ""
    assert defaults.opencode_api_key == ""
    assert defaults.opencode_base_url == ""
    # Secrets must never render in reprs.
    assert "custom_api_key" not in repr(defaults)
    assert "opencode_api_key" not in repr(defaults)


def test_env_override_resolves_through_config(monkeypatch):
    monkeypatch.setenv("CUSTOM_BASE_URL", "http://127.0.0.1:9999/v1")
    monkeypatch.setenv("CUSTOM_API_KEY", "env-local-key")
    monkeypatch.setenv("OPENCODE_BASE_URL", "https://opencode.ai/zen/v1")
    assert config.get("custom_base_url") == "http://127.0.0.1:9999/v1"
    assert config.get("custom_api_key") == "env-local-key"
    assert config.get("opencode_base_url") == "https://opencode.ai/zen/v1"


def test_machina_prefixed_env_wins_over_legacy(monkeypatch):
    monkeypatch.setenv("CUSTOM_BASE_URL", "http://127.0.0.1:9999/v1")
    monkeypatch.setenv("MACHINA_CUSTOM_BASE_URL", "http://127.0.0.1:8888/v1")
    assert config.get("custom_base_url") == "http://127.0.0.1:8888/v1"


def test_cli_help_lists_both_new_providers():
    from typer.testing import CliRunner

    from machina.cli import app

    result = CliRunner().invoke(app, ["chat", "--help"])
    assert result.exit_code == 0, result.output
    assert "opencode" in result.output
    assert "custom" in result.output
    assert "CUSTOM_BASE_URL" in result.output
