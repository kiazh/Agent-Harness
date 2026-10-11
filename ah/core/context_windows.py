"""Per-model context windows and the derived per-turn token budget.

``context_budget`` (default 8000) is a per-turn *spend* cap, not the
provider's context window: every loop iteration re-sends the full history,
so reported totals inflate fast. A fixed 8000 stops long-context models
long before their window fills. This module maps model ids to documented
context windows so the loop can derive a proportional budget instead.

The configured value always wins when it differs from the legacy default:
an explicit setting is a deliberate choice and is only capped by the
agent's absolute ceiling. Unknown models keep the legacy behavior exactly.
"""

from __future__ import annotations

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

# Unknown models behave exactly as before: this window implies exactly the
# legacy floor (32_000 // 4 == 8000), so unmapped ids change nothing.
DEFAULT_CONTEXT_WINDOW = 32_000

# Cumulative spend cap ≈ a quarter of the window. A full window is never
# the right cap because per-iteration re-sends inflate the summed total.
TURN_BUDGET_DIVISOR = 4

# Must match DEFAULTS["context_budget"] in ah.core.config (kept literal to
# avoid an import cycle; a test pins them together).
LEGACY_DEFAULT_BUDGET = 8000


def context_window_for(model: str | None) -> int:
    """Documented context window for *model*, or the default for unknown ids."""
    name = (model or "").lower()
    best = 0
    window = DEFAULT_CONTEXT_WINDOW
    for key, value in MODEL_CONTEXT_WINDOWS.items():
        if key in name and len(key) > best:
            best = len(key)
            window = value
    return window


def turn_token_budget(model: str | None, configured_budget: int = LEGACY_DEFAULT_BUDGET) -> int:
    """Effective per-turn token budget for *model*.

    An explicit configuration (anything but the legacy default) wins
    unchanged. Otherwise the budget scales with the model's window,
    floored at the legacy default. The caller still applies the agent's
    absolute ceiling.
    """
    if configured_budget != LEGACY_DEFAULT_BUDGET:
        return configured_budget
    return max(LEGACY_DEFAULT_BUDGET, context_window_for(model) // TURN_BUDGET_DIVISOR)
