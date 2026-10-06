"""Direct providers (one per model family) + per-provider reasoning effort."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


def _mock_post(response_data: dict) -> MagicMock:
    mock_response = MagicMock()
    mock_response.json.return_value = response_data
    mock_response.raise_for_status = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {}
    return mock_response


def _make_direct(provider_name: str, response_data: dict | None = None):
    from ah.core.provider import PROVIDER_SPECS, DirectProvider

    spec = PROVIDER_SPECS[provider_name]
    provider = DirectProvider.__new__(DirectProvider)
    provider.spec = spec
    provider.api_key = "test-key"
    provider.model = spec.default_model
    provider._default_effort = ""
    provider._rate_limiter = AsyncMock()
    provider.client = MagicMock()
    provider.client.post = AsyncMock(
        return_value=_mock_post(
            response_data
            or {
                "model": spec.default_model,
                "choices": [{"message": {"content": "OK", "tool_calls": []}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
            }
        )
    )
    return provider


def _make_anthropic(response_data: dict | None = None):
    from ah.core.provider import AnthropicProvider

    provider = AnthropicProvider.__new__(AnthropicProvider)
    provider.api_key = "test-key"
    provider.model = AnthropicProvider.DEFAULT_MODEL
    provider._default_effort = ""
    provider._rate_limiter = AsyncMock()
    provider.client = MagicMock()
    provider.client.post = AsyncMock(
        return_value=_mock_post(
            response_data
            or {
                "type": "message",
                "model": AnthropicProvider.DEFAULT_MODEL,
                "content": [{"type": "text", "text": "OK"}],
                "usage": {"input_tokens": 5, "output_tokens": 3},
            }
        )
    )
    return provider


# ─── provider registry accuracy ────────────────────────────────────────────


def test_providers_tuple_lists_every_family():
    from ah.core.provider import PROVIDERS

    assert set(PROVIDERS) == {
        "openrouter",
        "openai",
        "anthropic",
        "google",
        "mistral",
        "groq",
        "together",
        "deepseek",
        "xai",
        "ollama",
    }


def test_gateway_reexports_same_provider_list():
    from ah.core.provider import PROVIDERS as core_providers
    from ah.gateway.server import PROVIDERS as gateway_providers

    assert gateway_providers == core_providers


def test_direct_specs_have_accurate_base_urls_and_keys():
    from ah.core.provider import PROVIDER_SPECS

    expected = {
        # name: (base_url, api key env var, default model)
        "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY", "gpt-4o-mini"),
        "deepseek": ("https://api.deepseek.com", "DEEPSEEK_API_KEY", "deepseek-chat"),
        "xai": ("https://api.x.ai/v1", "XAI_API_KEY", "grok-4"),
        "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY", "llama-3.3-70b-versatile"),
        "together": (
            "https://api.together.xyz/v1",
            "TOGETHER_API_KEY",
            "meta-llama/Llama-3.3-70B-Instruct-Turbo",
        ),
        "mistral": ("https://api.mistral.ai/v1", "MISTRAL_API_KEY", "mistral-small-latest"),
        "google": (
            "https://generativelanguage.googleapis.com/v1beta/openai/",
            "GOOGLE_API_KEY",
            "gemini-2.0-flash",
        ),
    }
    assert set(PROVIDER_SPECS) == set(expected)
    for name, (base_url, env_key, model) in expected.items():
        spec = PROVIDER_SPECS[name]
        assert spec.base_url == base_url, name
        assert spec.api_key_env == env_key, name
        assert spec.config_key == env_key.lower(), name
        assert spec.default_model == model, name


def test_config_registers_every_provider_key():
    from ah.core.config import DEFAULTS, LEGACY_ENV_VARS, SECRET_KEYS

    for env_key in (
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GOOGLE_API_KEY",
        "MISTRAL_API_KEY",
        "GROQ_API_KEY",
        "TOGETHER_API_KEY",
        "DEEPSEEK_API_KEY",
        "XAI_API_KEY",
        "COHERE_API_KEY",
    ):
        config_key = env_key.lower()
        assert config_key in DEFAULTS, config_key
        assert LEGACY_ENV_VARS[config_key] == env_key, config_key
        assert config_key in SECRET_KEYS, config_key
    assert DEFAULTS["reasoning_effort"] == ""


# ─── effort normalization ──────────────────────────────────────────────────


def test_normalize_reasoning_effort():
    from ah.core.provider import normalize_reasoning_effort

    assert normalize_reasoning_effort(None) == ""
    assert normalize_reasoning_effort("") == ""
    assert normalize_reasoning_effort("off") == ""
    assert normalize_reasoning_effort("HIGH") == "high"
    assert normalize_reasoning_effort(" Medium ") == "medium"
    assert normalize_reasoning_effort("low") == "low"


def test_normalize_reasoning_effort_rejects_garbage():
    from ah.core.exceptions import ValidationError
    from ah.core.provider import normalize_reasoning_effort

    with pytest.raises(ValidationError):
        normalize_reasoning_effort("ultra")


# ─── factory ───────────────────────────────────────────────────────────────


def test_get_provider_builds_every_family(monkeypatch):
    from ah.core.provider import (
        PROVIDER_SPECS,
        AnthropicProvider,
        DirectProvider,
        OllamaProvider,
        OpenRouterProvider,
        get_provider,
    )

    for name, spec in PROVIDER_SPECS.items():
        monkeypatch.setenv(spec.api_key_env, f"test-{name}-key")
        provider = get_provider(provider=name)
        assert isinstance(provider, DirectProvider)
        assert provider.model == spec.default_model
        assert provider.client.base_url == spec.base_url or str(provider.client.base_url).rstrip(
            "/"
        ) == spec.base_url.rstrip("/")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-or-key")
    assert isinstance(get_provider(provider="openrouter"), OpenRouterProvider)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-ant-key")
    assert isinstance(get_provider(provider="anthropic"), AnthropicProvider)
    assert isinstance(get_provider(provider="ollama"), OllamaProvider)


def test_get_provider_rejects_unknown():
    from ah.core.exceptions import ValidationError
    from ah.core.provider import get_provider

    with pytest.raises(ValidationError, match="Unknown provider"):
        get_provider(provider="skynet")


def test_get_provider_requires_key(monkeypatch):
    from ah.core.exceptions import ValidationError
    from ah.core.provider import PROVIDER_SPECS, get_provider

    for name, spec in PROVIDER_SPECS.items():
        monkeypatch.delenv(spec.api_key_env, raising=False)
        with pytest.raises(ValidationError, match=spec.api_key_env):
            get_provider(provider=name)


# ─── effort payload mapping ────────────────────────────────────────────────


async def _complete_and_capture(provider):
    result = await provider.complete(messages=[{"role": "user", "content": "hi"}])
    assert result.content == "OK"
    return provider.client.post.await_args.kwargs["json"]


@pytest.mark.asyncio
async def test_direct_provider_omits_effort_when_unset():
    provider = _make_direct("deepseek")
    payload = await _complete_and_capture(provider)
    assert "reasoning_effort" not in payload
    assert payload["model"] == "deepseek-chat"


@pytest.mark.asyncio
async def test_direct_provider_sends_effort_for_supported_vendors():
    for name in ("openai", "deepseek", "xai", "groq", "together"):
        provider = _make_direct(name)
        result = await provider.complete(
            messages=[{"role": "user", "content": "hi"}], reasoning_effort="high"
        )
        assert result.content == "OK"
        payload = provider.client.post.await_args.kwargs["json"]
        assert payload["reasoning_effort"] == "high", name


@pytest.mark.asyncio
async def test_direct_provider_skips_effort_where_unsupported():
    for name in ("mistral", "google"):
        provider = _make_direct(name)
        payload = await _complete_and_capture(provider)
        assert "reasoning_effort" not in payload, name


@pytest.mark.asyncio
async def test_direct_provider_reads_effort_from_config(monkeypatch):
    from ah.core.config import config

    monkeypatch.setattr(config, "_session_overrides", {"reasoning_effort": "low"})
    provider = _make_direct("openai")
    payload = await _complete_and_capture(provider)
    assert payload["reasoning_effort"] == "low"


@pytest.mark.asyncio
async def test_openrouter_sends_reasoning_effort_object():
    from ah.core.provider import OpenRouterProvider

    provider = OpenRouterProvider.__new__(OpenRouterProvider)
    provider.api_key = "test-key"
    provider.model = "openai/gpt-4o-mini"
    provider._default_effort = ""
    provider._rate_limiter = AsyncMock()
    provider.client = MagicMock()
    provider.client.post = AsyncMock(
        return_value=_mock_post(
            {
                "model": "openai/gpt-4o-mini",
                "choices": [{"message": {"content": "OK", "tool_calls": []}}],
                "usage": {},
            }
        )
    )
    result = await provider.complete(
        messages=[{"role": "user", "content": "hi"}], reasoning_effort="medium"
    )
    assert result.content == "OK"
    payload = provider.client.post.await_args.kwargs["json"]
    assert payload["reasoning"] == {"effort": "medium"}


@pytest.mark.asyncio
async def test_ollama_maps_effort_to_think_flag():
    from ah.core.provider import OllamaProvider

    assert OllamaProvider._think_flag("") is None
    assert OllamaProvider._think_flag("low") is False
    assert OllamaProvider._think_flag("medium") is True
    assert OllamaProvider._think_flag("high") is True


# ─── anthropic normalization ───────────────────────────────────────────────


def test_anthropic_splits_system_and_tool_messages():
    from ah.core.provider import AnthropicProvider

    system, messages = AnthropicProvider._to_anthropic_messages(
        [
            {"role": "system", "content": "be nice"},
            {"role": "user", "content": "hi"},
            {"role": "tool", "tool_call_id": "t1", "content": "result"},
        ]
    )
    assert system == "be nice"
    assert messages[0] == {"role": "user", "content": "hi"}
    assert messages[1]["content"][0]["type"] == "tool_result"
    assert messages[1]["content"][0]["tool_use_id"] == "t1"


def test_anthropic_thinking_budgets_scale_with_effort():
    from ah.core.provider import AnthropicProvider

    provider = AnthropicProvider.__new__(AnthropicProvider)
    provider._default_effort = ""
    for effort, budget in (("low", 1024), ("medium", 4096), ("high", 10000)):
        thinking, max_tokens, temperature = provider._thinking_payload(effort, 4096, 0.7)
        assert thinking == {"thinking": {"type": "enabled", "budget_tokens": budget}}
        assert max_tokens >= budget + 1024
        assert temperature == 1.0
    thinking, max_tokens, temperature = provider._thinking_payload(None, 4096, 0.7)
    assert thinking == {} and max_tokens == 4096 and temperature == 0.7


@pytest.mark.asyncio
async def test_anthropic_complete_normalizes_blocks_and_usage():
    provider = _make_anthropic(
        {
            "type": "message",
            "model": "claude-3-5-sonnet-20241022",
            "content": [
                {"type": "text", "text": "calling "},
                {
                    "type": "tool_use",
                    "id": "tu1",
                    "name": "read_file",
                    "input": {"path": "a.txt"},
                },
            ],
            "usage": {"input_tokens": 10, "output_tokens": 4},
        }
    )
    result = await provider.complete(messages=[{"role": "user", "content": "hi"}])
    assert result.content == "calling "
    assert result.tool_calls[0]["function"]["name"] == "read_file"
    assert result.usage == {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}
    payload = provider.client.post.await_args.kwargs["json"]
    assert payload["model"] == "claude-3-5-sonnet-20241022"
    assert "thinking" not in payload


@pytest.mark.asyncio
async def test_anthropic_complete_sends_thinking_when_effort_set():
    provider = _make_anthropic()
    await provider.complete(
        messages=[{"role": "user", "content": "hi"}], reasoning_effort="high", max_tokens=4096
    )
    payload = provider.client.post.await_args.kwargs["json"]
    assert payload["thinking"] == {"type": "enabled", "budget_tokens": 10000}
    assert payload["max_tokens"] >= 11024
