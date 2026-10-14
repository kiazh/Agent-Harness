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
}

# Every chat provider the harness can route to: OpenRouter (multi-model),
# the direct families above, Anthropic (native API), local Ollama, ChatGPT
# subscription accounts (Codex protocol; auth-file based, no API key), and
# "custom" — any OpenAI-compatible endpoint configured via CUSTOM_BASE_URL
# (built dynamically in factory.py, so it is deliberately absent from
# PROVIDER_SPECS, the fixed vendor table).
PROVIDERS = ("openrouter", *tuple(PROVIDER_SPECS), "anthropic", "chatgpt", "custom", "ollama")
