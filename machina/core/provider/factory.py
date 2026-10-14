"""Provider factory — map a provider name to a configured transport."""

from __future__ import annotations

from machina.core.exceptions import ValidationError
from machina.core.provider.anthropic import AnthropicProvider
from machina.core.provider.base import LLMProvider
from machina.core.provider.direct import DirectProvider
from machina.core.provider.ollama import OllamaProvider
from machina.core.provider.openrouter import OpenRouterProvider
from machina.core.provider.specs import PROVIDER_SPECS, PROVIDERS


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
    elif provider == "ollama":
        return OllamaProvider(model=model or "llama3.1", reasoning_effort=reasoning_effort)
    spec = PROVIDER_SPECS.get(provider)
    if spec is not None:
        return DirectProvider(
            spec, model=model or spec.default_model, reasoning_effort=reasoning_effort
        )
    raise ValidationError(f"Unknown provider: {provider}. Choose one of: {', '.join(PROVIDERS)}")
