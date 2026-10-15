"""Direct-provider specs and the chat-provider registry table."""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Direct provider specs — one entry per model family with its own API.
# All of these expose an OpenAI-compatible ``/chat/completions`` endpoint
# (verified against vendor docs), so a single transport class serves them.
# ``reasoning`` controls how ``reasoning_effort`` is sent:
#   "openai" .. top-level ``reasoning_effort`` param (openai, deepseek, xai,
#               groq, together all document it)
#   "none" .... no knob; model choice expresses effort (mistral, google)
# ---------------------------------------------------------------------------
class ProviderSpec:
    """Static config for one direct (OpenAI-compatible) provider family."""

    def __init__(
        self,
        name: str,
        base_url: str,
        api_key_env: str,
        default_model: str,
        reasoning: str = "none",
        description: str = "",
        stream_usage: bool = False,
        requires_key: bool = True,
    ) -> None:
        self.name = name
        self.base_url = base_url
        self.api_key_env = api_key_env
        self.config_key = api_key_env.lower()
        self.default_model = default_model
        self.reasoning = reasoning
        self.description = description
        # Whether the vendor honors stream_options.include_usage. Only set
        # for vendors whose docs confirm it — others 400 on the param.
        self.stream_usage = stream_usage
        # Hosted vendors fail fast when the key env is absent. The custom
        # provider spec (built in factory.py) sets False: keyless local
        # OpenAI-compatible servers are a supported mode.
        self.requires_key = requires_key


PROVIDER_SPECS: dict[str, ProviderSpec] = {
    "openai": ProviderSpec(
        name="openai",
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
        default_model="gpt-4o-mini",
        reasoning="openai",
        description="OpenAI (GPT models)",
        stream_usage=True,
    ),
    "deepseek": ProviderSpec(
        name="deepseek",
        base_url="https://api.deepseek.com",
        api_key_env="DEEPSEEK_API_KEY",
        default_model="deepseek-chat",
        reasoning="openai",
        description="DeepSeek (chat + reasoner)",
        stream_usage=True,
    ),
    "xai": ProviderSpec(
        name="xai",
        base_url="https://api.x.ai/v1",
        api_key_env="XAI_API_KEY",
        default_model="grok-4",
        reasoning="openai",
        description="xAI (Grok models)",
    ),
    "groq": ProviderSpec(
        name="groq",
        base_url="https://api.groq.com/openai/v1",
        api_key_env="GROQ_API_KEY",
        default_model="llama-3.3-70b-versatile",
        reasoning="openai",
        description="Groq (fast inference)",
    ),
    "together": ProviderSpec(
        name="together",
        base_url="https://api.together.xyz/v1",
        api_key_env="TOGETHER_API_KEY",
        default_model="meta-llama/Llama-3.3-70B-Instruct-Turbo",
        reasoning="openai",
        description="Together AI (open models)",
    ),
    "mistral": ProviderSpec(
        name="mistral",
        base_url="https://api.mistral.ai/v1",
        api_key_env="MISTRAL_API_KEY",
        default_model="mistral-small-latest",
        reasoning="none",
        description="Mistral models",
    ),
    "google": ProviderSpec(
        name="google",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        api_key_env="GOOGLE_API_KEY",
        default_model="gemini-2.0-flash",
        reasoning="none",
        description="Google (Gemini models, OpenAI-compat endpoint)",
    ),
    "opencode": ProviderSpec(
        name="opencode",
        # OpenCode Zen gateway. Both candidate bases
        # (https://opencode.ai/inference/v1 and https://opencode.ai/zen/v1)
        # were probed live on 2026-10-14 and served an identical model list;
        # the V2 docs name this one. Override any time with the
        # opencode_base_url config key / OPENCODE_BASE_URL env var.
        base_url="https://opencode.ai/inference/v1",
        api_key_env="OPENCODE_API_KEY",
        default_model="big-pickle",
        reasoning="none",
        description="OpenCode Zen (free + paid models, OpenAI-compatible gateway)",
    ),
    "kimi": ProviderSpec(
        name="kimi",
        # Kimi Code (overseas base; China uses
        # https://api.kimi.com/coding/v1). Subscribers create a key in the
        # Kimi Code Console; the OpenAI-compatible endpoint documents a
        # top-level reasoning_effort (low/high/max) for k3 / k3-256k /
        # kimi-for-coding, so the OpenAI-style knob is wired as-is.
        base_url="https://api.kimi.ai/coding/v1",
        api_key_env="KIMI_API_KEY",
        default_model="kimi-for-coding",
        reasoning="openai",
        description="Kimi Code (K3/K2.8 coding models; membership API key)",
    ),
}

# Every chat provider the harness can route to: OpenRouter (multi-model),
# the direct families above, Anthropic (native API), ChatGPT subscription
# accounts (Codex protocol; auth-file based, no API key), GitHub Copilot
# seats (GitHub token; auth-file based), and "custom" — any
# OpenAI-compatible endpoint configured via CUSTOM_BASE_URL (built dynamically
# in factory.py, so it is deliberately absent from PROVIDER_SPECS, the fixed
# vendor table).
#
# User-added named providers (machina.security.providers store, Phase B) are
# also deliberately absent from this tuple: ``PROVIDERS`` is a registry whose
# ``in`` check consults the store dynamically, so validation sites that do
# ``provider in PROVIDERS`` accept named providers without caching a stale
# list. Iteration yields the built-ins only (pinned by the registry-accuracy
# test); call :func:`provider_names` for a listing that includes named ones.


class _ProviderRegistry(tuple):
    """Built-in provider names with dynamic membership for named providers."""

    def __contains__(self, value: object) -> bool:
        if tuple.__contains__(self, value):
            return True
        if not isinstance(value, str) or not value:
            return False
        return value in _named_provider_names()


def _named_provider_names() -> tuple[str, ...]:
    """Names in the user store; a broken/absent store counts as none.

    Function-level import keeps ``core.provider.specs`` loadable without the
    security layer (same idiom as ``core.config``'s .env resolver).
    """
    try:
        from machina.security.providers import provider_names

        return provider_names()
    except Exception:
        return ()


def provider_names() -> tuple[str, ...]:
    """All selectable provider names: built-ins, then user-added ones."""
    return (*PROVIDERS, *_named_provider_names())


PROVIDERS: tuple[str, ...] = _ProviderRegistry(
    (
        "openrouter",
        *tuple(PROVIDER_SPECS),
        "anthropic",
        "chatgpt",
        "copilot",
        "custom",
        "ollama",
    )
)
