"""Reasoning-effort normalization and precedence for provider calls."""

from __future__ import annotations

from machina.core.config import config
from machina.core.exceptions import ValidationError
from machina.core.provider._log import logger

# ---------------------------------------------------------------------------
# Reasoning effort — per-provider reasoning depth (low|medium|high).
#
# Each provider family exposes this differently, so the mapping lives here
# next to the transport code:
#   openai/deepseek/xai/groq/together .. top-level ``reasoning_effort``
#   openrouter ........................ ``reasoning: {"effort": ...}``
#   anthropic ......................... ``thinking`` budget in tokens
#   ollama ............................ boolean ``think`` flag
#   mistral/google .................... no API knob (model choice expresses it)
# The value is only ever sent when explicitly set (per-call argument or the
# ``reasoning_effort`` config key); unset means "provider default", so existing
# behavior is unchanged.
# ---------------------------------------------------------------------------
REASONING_EFFORTS = ("low", "medium", "high")

# Anthropic thinking budgets per effort level (tokens reserved for thinking).
ANTHROPIC_THINKING_BUDGETS = {"low": 1024, "medium": 4096, "high": 10000}


def normalize_reasoning_effort(value: str | None) -> str:
    """Normalize a reasoning-effort value; ``""``/None means unset (provider default)."""
    if value is None:
        return ""
    normalized = str(value).strip().lower()
    if normalized in ("", "none", "off", "default", "auto"):
        return ""
    if normalized not in REASONING_EFFORTS:
        raise ValidationError(
            f"reasoning_effort must be one of {REASONING_EFFORTS} (or unset), got {value!r}"
        )
    return normalized


def _resolve_effort(explicit: str | None, instance_default: str | None = None) -> str:
    """Effort precedence: per-call argument > provider instance > config key."""
    if explicit is not None:
        return normalize_reasoning_effort(explicit)
    if instance_default:
        return normalize_reasoning_effort(instance_default)
    try:
        return normalize_reasoning_effort(config.get("reasoning_effort"))
    except (ValidationError, ValueError) as exc:
        # A bad value in config.yaml must not fail every call, but it must not
        # vanish silently either: log what was ignored and use the provider
        # default. Anything else (I/O, programming errors) propagates. The
        # exception class is logged instead of its message because the record
        # sanitizer strips exception text (it can echo config/prompt content).
        logger.warning(
            "Ignoring invalid reasoning_effort config value (%s); using provider default",
            type(exc).__name__,
        )
        return ""
