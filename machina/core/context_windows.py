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
    "gemini-2.0-flash": 1_048_576,
    "grok-4": 256_000,
    "claude-3-5-sonnet": 200_000,
    "gpt-4o-mini": 128_000,
    "llama-3.3-70b": 128_000,
    "mistral-small": 128_000,
    "deepseek-chat": 64_000,
    "deepseek-reasoner": 64_000,
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


def context_window_for(model: str | None, provider=None) -> int:
    """Documented context window for *model*, or the default for unknown ids.

    OpenRouter catalog metadata is provider-scoped: it only applies when
    *provider* identifies the OpenRouter family (name or instance). Base,
    local and other direct providers always use the curated fallback so one
    serving's capacity never leaks into another provider's estimate.
    """
    # Anything non-string (e.g. test doubles) behaves like an unknown model.
    name = model.lower() if isinstance(model, str) else ""
    if _is_openrouter(provider):
        cached = _provider_windows.get(name)
        if cached is not None:
            return cached
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
