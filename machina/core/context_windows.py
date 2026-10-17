"""Model context capacity, independent of cumulative billed token usage."""

from __future__ import annotations

import asyncio
import logging
import time

from cachetools import TTLCache

logger = logging.getLogger(__name__)
_provider_windows: TTLCache = TTLCache(maxsize=4096, ttl=300)
_refresh_after = 0.0

# Documented context windows (tokens) for the harness's curated models.
# Conservative where serving varies (local Ollama entries assume a modest
# num_ctx — raise them if the server is configured larger). Matching is a
# case-insensitive substring on the model id; the longest key wins so
# "mistral-small" beats bare "mistral" (local) for "mistral-small-latest".
MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    # Vendor-documented windows for the ids the picker currently ships
    # (2026-10 catalog). Conservative where the vendor offers a beta/expansion
    # tier: the standard window is used so a turn is never refused against a
    # capacity the model may not actually serve.
    "gemini-2.5-flash": 1_048_576,
    "gemini-2.5-pro": 1_048_576,
    "gemini-2.0-flash": 1_048_576,
    "gpt-4.1": 1_047_576,
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    # GPT-5.x standard API window; ChatGPT catalog windows (272K–500K) win
    # when the chatgpt provider resolves them.
    "gpt-5": 400_000,
    "o3": 200_000,
    "o4-mini": 200_000,
    "grok-4": 256_000,
    "grok-3": 131_072,
    "claude-sonnet-4": 200_000,
    "claude-opus-4": 200_000,
    "claude-haiku-4": 200_000,
    "claude-3-7-sonnet": 200_000,
    "claude-3-5-sonnet": 200_000,
    "llama-3.3-70b": 128_000,
    "mistral-large": 128_000,
    "mistral-small": 128_000,
    # DeepSeek's hosted API is 64K for the chat/reasoner families; V4 reports
    # a 1M window in the OpenRouter catalog (provider-scoped lookup wins).
    "deepseek-v4": 1_048_576,
    "deepseek-v3": 64_000,
    "deepseek-r1": 64_000,
    "deepseek-chat": 64_000,
    "deepseek-reasoner": 64_000,
    # Qwen hosted families. qwen-max is the 32K DashScope entry; the
    # 2.5/3 families are documented at 128K.
    "qwen3": 131_072,
    "qwen2.5": 131_072,
    "qwen-plus": 131_072,
    "qwen-max": 32_768,
    # Kimi Code membership models (kimi.com/code/docs model table): k3 is 1M
    # for higher tiers / 256K below, k3-256k is fixed 256K, kimi-for-coding
    # (K2.8 Preview) is 1M, and its HighSpeed variant is 256K. Longest-key
    # matching keeps k3-256k and kimi-for-coding-highspeed on their own rows.
    "k3-256k": 262_144,
    "k3": 1_048_576,
    "kimi-for-coding-highspeed": 262_144,
    "kimi-for-coding": 1_048_576,
    # Local serving — conservative defaults tied to typical num_ctx.
    # Sized so the derived budget lands exactly on the legacy floor.
    "llama3.1": 32_000,
    "mistral": 32_000,
    "phi3": 32_000,
    "codellama": 16_384,
}

# Unknown metadata has a conservative fallback; no artificial spend cap.
DEFAULT_CONTEXT_WINDOW = 32_000

# Must match DEFAULTS["context_budget"] in machina.core.config (kept literal to
# avoid an import cycle; a test pins them together).
LEGACY_DEFAULT_BUDGET = 8000


def _provider_name(provider) -> str:
    """Normalized provider family name for metadata scoping."""
    if provider is None:
        return ""
    if isinstance(provider, str):
        return provider.lower()
    try:
        name = type(provider).__name__
        if name.endswith("Provider"):
            name = name[: -len("Provider")]
        return name.lower()
    except Exception:
        return ""


def _is_openrouter(provider) -> bool:
    return _provider_name(provider) == "openrouter"


def _is_chatgpt(provider) -> bool:
    return _provider_name(provider) == "chatgpt"


def context_window_for(model: str | None, provider=None) -> int:
    """Documented context window for *model*, or the default for unknown ids.

    Provider-scoped metadata: OpenRouter catalog windows and the ChatGPT
    account catalog windows apply only when *provider* names that family
    (name or instance). The chatgpt lookup is network-free — it reads the
    in-process live catalog, the Codex CLI's local cache, or the bundled
    verified snapshot, in that order. Catalog metadata is authoritative when
    it resolves: the curated table below is only a fallback for providers
    without a catalog, so a stale local entry can never override live
    discovery. Base, local and other direct providers always use the curated
    fallback so one serving's capacity never leaks into another provider's
    estimate.
    """
    # Anything non-string (e.g. test doubles) behaves like an unknown model.
    name = model.lower() if isinstance(model, str) else ""
    if _is_openrouter(provider):
        cached = _provider_windows.get(name)
        if cached is not None:
            return cached
    if _is_chatgpt(provider):
        # Function-level import: context_windows stays a dependency-light
        # leaf for its many lazy importers (see architecture guard).
        from machina.core.provider.catalog import window_for

        window = window_for(name)
        if window:
            return window
    best = 0
    window = DEFAULT_CONTEXT_WINDOW
    for key, value in MODEL_CONTEXT_WINDOWS.items():
        if key in name and len(key) > best:
            best = len(key)
            window = value
    return window


def turn_token_budget(
    model: str | None,
    configured_budget: int = LEGACY_DEFAULT_BUDGET,
    provider=None,
) -> int:
    """Compatibility helper: capacity only; legacy spend setting is ignored."""
    return context_window_for(model, provider)


async def openrouter_context_window(client, model: str) -> int:
    """Cached public catalog metadata; a failed lookup never stops a turn.

    Only ids and valid context_length integers are retained. Errors are logged
    by type, without provider response bodies or credential-bearing URLs.
    The cache is OpenRouter-scoped; other providers never read it.
    """
    global _refresh_after
    name = model.lower()
    if name not in _provider_windows and time.monotonic() >= _refresh_after:
        try:
            response = await asyncio.wait_for(client.get("/models"), timeout=3)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict) or not isinstance(data.get("data"), list):
                raise ValueError("invalid model metadata")
            for entry in data["data"]:
                if not isinstance(entry, dict):
                    continue
                window, identifier = entry.get("context_length"), entry.get("id")
                if isinstance(identifier, str) and type(window) is int and 0 < window < 2**31:
                    _provider_windows[identifier.lower()] = window
            _refresh_after = time.monotonic() + 300
        except Exception as error:
            _refresh_after = time.monotonic() + 30
            logger.warning("Model window metadata unavailable (%s)", type(error).__name__)
    return context_window_for(model, "openrouter")
