"""R4-KIMI-ADD: Kimi Code (K3 / K2.x membership models) as an API-key provider.

Officially supported third-party tooling: the Kimi Code docs
(kimi.com/code/docs/en, checked 2026-10-14) state that subscribers "can also
obtain an API Key to integrate Kimi Code's model capabilities into
third-party development tools and platforms", with first-party guides for
Claude Code, OpenCode and Codex. The overseas OpenAI-compatible base is
``https://api.kimi.ai/coding/v1`` (China: ``https://api.kimi.com/coding/v1``)
and the four model ids are ``k3`` (up to 1M per tier; 256K below it),
``k3-256k`` (256K), ``kimi-for-coding`` (K2.8 Preview, up to 1M) and
``kimi-for-coding-highspeed`` (256K).

The docs document a top-level ``reasoning_effort`` parameter (``low`` /
``high`` / ``max``) for K3 and ``kimi-for-coding``, so the spec opts into the
OpenAI-style effort knob instead of inventing a mapping; the harness
vocabulary is low/medium/high, so only low/high (or unset = wire default)
match the documented accepted values.
"""

from __future__ import annotations

import json

import httpx
import pytest

from machina.core.config import DEFAULTS, LEGACY_ENV_VARS, SECRET_KEYS, config
from machina.core.context_windows import context_window_for
from machina.core.exceptions import ValidationError
from machina.core.provider import PROVIDER_SPECS, PROVIDERS, DirectProvider, get_provider
from machina.core.provider.rate_limit import AsyncTokenBucket

KIMI_BASE_URL = "https://api.kimi.ai/coding/v1"
KIMI_MODEL = "kimi-for-coding"


def test_kimi_spec_pins_endpoint_key_and_default_model() -> None:
    spec = PROVIDER_SPECS["kimi"]
    assert spec.base_url == KIMI_BASE_URL
    assert spec.api_key_env == "KIMI_API_KEY"
    assert spec.config_key == "kimi_api_key"
    assert spec.default_model == KIMI_MODEL
    # The Kimi Code docs document top-level `reasoning_effort` (low/high/max)
    # on the OpenAI-compatible endpoint; no value mapping is invented here.
    assert spec.reasoning == "openai"
    assert spec.requires_key is True
    assert "kimi" in PROVIDERS


def test_kimi_key_is_registered_in_config_plumbing() -> None:
    assert DEFAULTS["kimi_api_key"] == ""
    assert LEGACY_ENV_VARS["kimi_api_key"] == "KIMI_API_KEY"
    assert "kimi_api_key" in SECRET_KEYS
    # Secrets never render in reprs.
    from machina.core.config import Config

    assert "kimi_api_key" not in repr(Config())


def test_kimi_context_windows_match_the_documented_model_ids() -> None:
    assert context_window_for("k3") == 1_048_576
    assert context_window_for("k3-256k") == 262_144
    assert context_window_for("kimi-for-coding") == 1_048_576
    assert context_window_for("kimi-for-coding-highspeed") == 262_144


async def test_kimi_resolves_through_the_factory_to_a_direct_provider(monkeypatch) -> None:
    monkeypatch.setenv("KIMI_API_KEY", "kimi-test-key")
    provider = get_provider("kimi")
    try:
        assert isinstance(provider, DirectProvider)
        assert provider.spec.name == "kimi"
        assert provider.model == KIMI_MODEL
        assert provider.api_key == "kimi-test-key"
        assert str(provider.client.base_url).rstrip("/") == KIMI_BASE_URL
    finally:
        await provider.close()


def test_kimi_requires_the_api_key(monkeypatch) -> None:
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    with pytest.raises(ValidationError, match="KIMI_API_KEY"):
        get_provider("kimi")


async def test_kimi_chat_completions_wire_shape(monkeypatch) -> None:
    """E2E payload shape through the real transport with a mocked network."""
    monkeypatch.setenv("KIMI_API_KEY", "kimi-test-key")
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "model": KIMI_MODEL,
                "choices": [{"message": {"content": "OK", "tool_calls": []}}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
            },
        )

    provider = get_provider("kimi")
    assert isinstance(provider, DirectProvider)
    headers = dict(provider.client.headers)
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(
        base_url=provider.spec.base_url,
        headers=headers,
        transport=httpx.MockTransport(handler),
    )
    provider._rate_limiter = AsyncTokenBucket(rate=0, capacity=0)
    try:
        response = await provider.complete([{"role": "user", "content": "r4-kimi-add wire probe"}])
    finally:
        await provider.close()

    assert response.content == "OK"
    assert len(captured) == 1
    request = captured[0]
    assert str(request.url) == f"{KIMI_BASE_URL}/chat/completions"
    assert request.headers["Authorization"] == "Bearer kimi-test-key"
    payload = json.loads(request.content.decode())
    assert payload["model"] == KIMI_MODEL
    assert payload["messages"] == [{"role": "user", "content": "r4-kimi-add wire probe"}]
    # Unset effort means the wire default (Kimi documents high/max defaults);
    # the harness must not send a value it did not resolve.
    assert "reasoning_effort" not in payload


async def test_kimi_reasoning_effort_uses_the_documented_top_level_param(monkeypatch) -> None:
    monkeypatch.setenv("KIMI_API_KEY", "kimi-test-key")
    provider = get_provider("kimi", reasoning_effort="high")
    try:
        assert provider._effort_payload(None) == {"reasoning_effort": "high"}
    finally:
        await provider.close()


def test_kimi_runtime_readiness_reports_key_state(monkeypatch) -> None:
    from machina.core import runtime as runtime_mod

    monkeypatch.setattr(config, "_session_overrides", {"provider": "kimi"})
    monkeypatch.setenv("KIMI_API_KEY", "kimi-test-key")
    report = runtime_mod._detect_providers()
    assert report["chat_provider"] == "ready"
    assert report["chat_provider_selected"] == "kimi"
    assert report["chat_provider_reason"] == ""

    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    report = runtime_mod._detect_providers()
    assert report["chat_provider"] == "unavailable"
    assert report["chat_provider_reason"] == "KIMI_API_KEY not set"


def test_kimi_is_reserved_against_user_added_shadowing() -> None:
    from machina.security.providers import RESERVED_NAMES

    assert "kimi" in RESERVED_NAMES
