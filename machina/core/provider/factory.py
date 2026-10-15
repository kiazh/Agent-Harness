"""Provider factory — map a provider name to a configured transport."""

from __future__ import annotations

from copy import copy

from machina.core.config import config
from machina.core.exceptions import ValidationError
from machina.core.provider.anthropic import AnthropicProvider
from machina.core.provider.base import LLMProvider
from machina.core.provider.chatgpt import ChatGPTProvider
from machina.core.provider.copilot import CopilotProvider
from machina.core.provider.direct import DirectProvider
from machina.core.provider.ollama import OllamaProvider
from machina.core.provider.openrouter import OpenRouterProvider
from machina.core.provider.specs import PROVIDER_SPECS, ProviderSpec, provider_names


def _custom_provider(model: str | None, reasoning_effort: str | None) -> LLMProvider:
    """Any OpenAI-compatible endpoint: CUSTOM_BASE_URL + optional CUSTOM_API_KEY.

    Keyless is a supported mode (local LM Studio/vLLM/llama.cpp servers and
    the Hermes loopback proxy all accept unauthenticated requests), so the
    dynamic spec sets ``requires_key=False``. Deliberately absent from
    PROVIDER_SPECS: it is built per call from config, not a fixed vendor.
    """
    base_url = str(config.get("custom_base_url") or "").strip()
    if not base_url:
        raise ValidationError(
            "custom provider requires custom_base_url (set the CUSTOM_BASE_URL env var)"
        )
    if not model:
        raise ValidationError(
            "custom provider requires a model id (pass --model or set MACHINA_MODEL)"
        )
    spec = ProviderSpec(
        name="custom",
        base_url=base_url,
        api_key_env="CUSTOM_API_KEY",
        default_model=model,
        description="Custom OpenAI-compatible endpoint",
        requires_key=False,
    )
    return DirectProvider(spec, model=model, reasoning_effort=reasoning_effort)


def _named_provider(
    name: str, model: str | None, reasoning_effort: str | None
) -> LLMProvider | None:
    """User-added named provider from the security store (None when absent).

    Same OpenAI-compatible transport as ``custom``, but with a persisted
    name/base URL and its model catalog pulled from ``GET {base}/models``.
    The key is resolved through the shared secret idiom (env -> .env ->
    backend) under the store-assigned env name; keyless local endpoints are
    a supported mode, matching ``custom``.
    """
    from machina.security.providers import get_provider_entry, resolve_provider_key

    entry = get_provider_entry(name)
    if entry is None:
        return None
    resolved_model = model or str(entry.get("default_model") or "") or _first_known_model(entry)
    if not resolved_model:
        raise ValidationError(
            f"provider {name!r} has no known model yet; refresh its catalog "
            f"(/provider add or /models) or pass an explicit model id"
        )
    spec = ProviderSpec(
        name=name,
        base_url=str(entry.get("base_url") or ""),
        api_key_env=str(entry.get("api_key_env") or ""),
        default_model=resolved_model,
        description="User-added OpenAI-compatible provider",
        requires_key=False,
    )
    key = resolve_provider_key(entry)
    return DirectProvider(
        spec, api_key=key or None, model=resolved_model, reasoning_effort=reasoning_effort
    )


def _first_known_model(entry: dict) -> str:
    models = entry.get("models") if isinstance(entry.get("models"), list) else []
    ids = [str(item.get("id") or "").strip() for item in models if isinstance(item, dict)]
    return next((identifier for identifier in ids if identifier), "")


def _opencode_spec() -> ProviderSpec:
    """The OpenCode Zen preset, honoring an optional base-url override."""
    spec = copy(PROVIDER_SPECS["opencode"])
    override = str(config.get("opencode_base_url") or "").strip()
    if override:
        spec.base_url = override
    return spec


def get_provider(
    provider: str = "openrouter",
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> LLMProvider:
    """Factory: return the configured provider.

    ``reasoning_effort`` (low|medium|high) optionally pins the reasoning depth;
    otherwise each provider falls back to the ``reasoning_effort`` config key.
    """
    if provider == "openrouter":
        return OpenRouterProvider(
            model=model or "openrouter/free", reasoning_effort=reasoning_effort
        )
    elif provider == "anthropic":
        return AnthropicProvider(
            model=model or AnthropicProvider.DEFAULT_MODEL, reasoning_effort=reasoning_effort
        )
    elif provider == "chatgpt":
        # ChatGPT subscription tokens (Codex protocol); no API key involved.
        return ChatGPTProvider(model=model, reasoning_effort=reasoning_effort)
    elif provider == "copilot":
        # GitHub Copilot seat (GitHub token); no API key.
        return CopilotProvider(model=model, reasoning_effort=reasoning_effort)
    elif provider == "custom":
        return _custom_provider(model, reasoning_effort)
    elif provider == "ollama":
        return OllamaProvider(model=model or "llama3.1", reasoning_effort=reasoning_effort)
    spec = PROVIDER_SPECS.get(provider)
    if spec is not None:
        if provider == "opencode":
            spec = _opencode_spec()
        return DirectProvider(
            spec, model=model or spec.default_model, reasoning_effort=reasoning_effort
        )
    named = _named_provider(provider, model, reasoning_effort)
    if named is not None:
        return named
    raise ValidationError(
        f"Unknown provider: {provider}. Choose one of: {', '.join(provider_names())}"
    )
