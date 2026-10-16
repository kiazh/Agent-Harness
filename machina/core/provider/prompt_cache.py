"""Provider prompt-cache capability profiles.

A profile records what a provider/model family actually supports so the
transport layer never sends cache fields to endpoints that reject them and
never pretends an unknown endpoint caches. Values encode the state of the
vendors' documented APIs at the recorded verification date; re-verify before
changing payload shapes. Unknown means unsupported until verified.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from machina.core.config import config
from machina.core.exceptions import ValidationError

__all__ = ["CacheProfile", "cache_profile", "CACHE_NONE"]

logger = logging.getLogger("machina.core.provider")


@dataclass(frozen=True)
class CacheProfile:
    """Per-provider prompt-cache capabilities.

    mode:
        ``"explicit"`` — the client marks breakpoints (Anthropic-style
        ``cache_control`` blocks; breakpoint count and minimum prefix apply).
        ``"implicit"`` — the provider caches eligible prefixes automatically;
        the client sends nothing and only parses usage categories.
        ``"none"`` — no prompt caching (also the answer for unverified
        endpoints).
    ``usage_fields`` names the response usage categories that appear when
    caching is active, for ledger attribution.
    """

    mode: str
    min_prefix_tokens: int | None
    max_breakpoints: int
    usage_fields: tuple[str, ...]
    verified_at: str
    notes: str


CACHE_NONE = CacheProfile(
    mode="none",
    min_prefix_tokens=None,
    max_breakpoints=0,
    usage_fields=(),
    verified_at="unverified",
    notes="no verified prompt-cache support",
)

_ANTHROPIC_EXPLICIT = CacheProfile(
    mode="explicit",
    min_prefix_tokens=1024,
    max_breakpoints=4,
    usage_fields=("cache_read_input_tokens", "cache_creation_input_tokens"),
    verified_at="2026-10-15",
    notes=(
        "Anthropic prompt caching: explicit cache_control breakpoints, at most 4; "
        "model-dependent minimum cacheable prefix (1024 conservative); cache "
        "writes are billed above ordinary input on some models"
    ),
)

_OPENAI_IMPLICIT = CacheProfile(
    mode="implicit",
    min_prefix_tokens=1024,
    max_breakpoints=0,
    usage_fields=("prompt_tokens_details.cached_tokens",),
    verified_at="2026-10-15",
    notes="automatic prefix caching; cached input reported as a prompt_tokens subset",
)

_DEEPSEEK_IMPLICIT = CacheProfile(
    mode="implicit",
    min_prefix_tokens=64,
    max_breakpoints=0,
    usage_fields=("prompt_cache_hit_tokens", "prompt_cache_miss_tokens"),
    verified_at="2026-10-15",
    notes="automatic context caching with hit/miss token accounting",
)

_GOOGLE_IMPLICIT = CacheProfile(
    mode="implicit",
    min_prefix_tokens=1024,
    max_breakpoints=0,
    usage_fields=(),
    verified_at="2026-10-15",
    notes=(
        "implicit caching reported via cached_content_token_count on some API "
        "families; explicit cached-content references are not wired"
    ),
)

_OPENROUTER_ANTHROPIC = CacheProfile(
    mode="explicit",
    min_prefix_tokens=1024,
    max_breakpoints=4,
    usage_fields=_ANTHROPIC_EXPLICIT.usage_fields,
    verified_at="2026-10-15",
    notes="OpenRouter pass-through to Anthropic accepts cache_control breakpoints",
)


def _direct_profile(name: str) -> CacheProfile:
    if name == "openai":
        return _OPENAI_IMPLICIT
    if name == "deepseek":
        return _DEEPSEEK_IMPLICIT
    if name == "google":
        return _GOOGLE_IMPLICIT
    return CACHE_NONE


def cache_profile(provider: Any, model: str | None = None) -> CacheProfile:
    """Resolve the cache profile for *provider* (and its selected model).

    Shared transports resolve by their configured spec name; anything not
    explicitly verified gets :data:`CACHE_NONE` (conservative no-feature).
    """
    spec = getattr(provider, "spec", None)
    spec_name = getattr(spec, "name", None)
    if isinstance(spec_name, str) and spec_name:
        return _direct_profile(spec_name.lower())

    provider_name = type(provider).__name__
    if provider_name == "AnthropicProvider":
        return _ANTHROPIC_EXPLICIT
    if provider_name == "ChatGPTProvider":
        return _OPENAI_IMPLICIT
    if provider_name == "OpenRouterProvider":
        candidate = (model or getattr(provider, "model", "") or "").lower()
        if "anthropic" in candidate or "claude" in candidate:
            return _OPENROUTER_ANTHROPIC
        for marker in ("openai/", "gpt-", "/gpt", "deepseek", "gemini", "google/"):
            if marker in candidate:
                return _OPENAI_IMPLICIT
        return CACHE_NONE
    if provider_name == "CopilotProvider":
        # Copilot's transport is OpenAI-shaped with cached-token details on
        # supported models; treat as implicit reporting only.
        return _OPENAI_IMPLICIT
    if provider_name == "OllamaProvider":
        return CACHE_NONE
    return CACHE_NONE


# ---------------------------------------------------------------------------
# Prompt-cache breakpoints (E1 pilot — ``prompt_cache_enabled``, default OFF)
#
# The stable prefix (system prompt + tool schemas) is resent on every call.
# When enabled, the two ends that bound it get an Anthropic-style ephemeral
# ``cache_control`` marker: the final system content block and the last tool
# schema entry. AnthropicProvider sends breakpoints natively; OpenRouter
# passes them through only for models routed to Anthropic ("anthropic" in the
# model id) — Direct providers and Ollama are untouched.
#
# Expected saving is not quantified here: it is bounded by the serving
# model's minimum cacheable prompt length (~1k-4k tokens for current Claude
# models), by real-turn prefix stability, and by the cache-write premium
# until a prefix is read back within the TTL. It stays a pilot (default off)
# until that is measured on real turns.
# ---------------------------------------------------------------------------
def _prompt_cache_breakpoints_enabled() -> bool:
    """Read the pilot toggle live; invalid config degrades to disabled.

    A bad value must never take provider calls down: without breakpoints the
    request is still fully valid (same contract as ``reasoning_effort``).
    """
    try:
        return bool(config.get("prompt_cache_enabled"))
    except (ValidationError, ValueError) as exc:
        logger.warning(
            "Ignoring invalid prompt_cache_enabled config value (%s); "
            "sending requests without cache breakpoints",
            type(exc).__name__,
        )
        return False


def _ephemeral_cache_control() -> dict[str, str]:
    """Fresh Anthropic-style ephemeral breakpoint marker."""
    return {"type": "ephemeral"}


def _openrouter_anthropic_model(model: str | None) -> bool:
    """Whether an OpenRouter model id routes to Anthropic for cache_control."""
    return "anthropic" in (model or "").lower()


def _mark_openrouter_system_breakpoint(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]] | None:
    """Copy *messages* with an ephemeral breakpoint on the system tail.

    Returns None when there is no non-empty system message to mark, so the
    caller can leave the payload untouched. The caller's list and dicts are
    never mutated: breakpoints belong to the wire payload only, not to the
    caller's history.
    """
    last_system = -1
    for index, message in enumerate(messages):
        if message.get("role") == "system":
            last_system = index
    if last_system < 0:
        return None
    message = messages[last_system]
    content = message.get("content")
    if isinstance(content, str):
        if not content:
            return None
        marked: Any = [
            {"type": "text", "text": content, "cache_control": _ephemeral_cache_control()}
        ]
    elif isinstance(content, list) and content and isinstance(content[-1], dict):
        # Already block-form: mark a copy of the final block.
        marked = [
            *content[:-1],
            {**content[-1], "cache_control": _ephemeral_cache_control()},
        ]
    else:
        return None
    out = list(messages)
    out[last_system] = {**message, "content": marked}
    return out


def _mark_last_tool_breakpoint(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Copy *tools* with an ephemeral breakpoint on the final entry."""
    if not tools:
        return tools
    return [*tools[:-1], {**tools[-1], "cache_control": _ephemeral_cache_control()}]


def _mark_message_tail_breakpoint(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]] | None:
    """Copy *messages* with an ephemeral breakpoint on the final content block.

    The conversation tail is what makes within-turn reuse work: each
    tool-continuation request shares every earlier message byte-for-byte, so a
    breakpoint at the last block lets the provider read the growing prefix
    from cache instead of re-processing it. Returns None when there is nothing
    to mark (empty list, empty text, or unsupported content shape); the
    caller's list and dicts are never mutated.
    """
    if not messages:
        return None
    message = messages[-1]
    content = message.get("content")
    if isinstance(content, str):
        if not content:
            return None
        marked: Any = [
            {"type": "text", "text": content, "cache_control": _ephemeral_cache_control()}
        ]
    elif isinstance(content, list) and content and isinstance(content[-1], dict):
        marked = [
            *content[:-1],
            {**content[-1], "cache_control": _ephemeral_cache_control()},
        ]
    else:
        return None
    out = list(messages)
    out[-1] = {**message, "content": marked}
    return out


def _openrouter_wire_messages(
    messages: list[dict[str, Any]], cache_breakpoints: bool
) -> tuple[list[dict[str, Any]], bool, bool]:
    """Return ``(messages_for_wire, system_marked, tail_marked)`` for OpenRouter.

    With breakpoints enabled the system tail is marked first; when there is no
    system message to mark (the standard turn path builds a single user
    message), the conversation tail is marked instead so within-turn
    continuations can reuse the growing prefix. Copied lists only — the
    caller's history is never mutated and the OFF path stays byte-identical.
    """
    if not cache_breakpoints:
        return messages, False, False
    marked = _mark_openrouter_system_breakpoint(messages)
    if marked is not None:
        return marked, True, False
    tail = _mark_message_tail_breakpoint(messages)
    if tail is not None:
        return tail, False, True
    return messages, False, False


def _log_prompt_cache_breakpoints(
    provider: str, model: str, *, system: bool, tools: bool, tail: bool = False
) -> None:
    """One debug line per call when breakpoints attach (pilot visibility).

    The saving expectation is stated, not measured: benefit depends on the
    serving model's minimum cacheable prompt length and real-turn prefix
    stability (follow-up sizing), so no number is invented here.
    """
    if not (system or tools or tail):
        return
    logger.debug(
        "prompt cache breakpoints attached provider=%s model=%s system=%s tools=%s tail=%s; "
        "saving unmeasured (minimum cacheable prompt length and real-turn "
        "prefix stability apply)",
        provider,
        model,
        system,
        tools,
        tail,
    )


def apply_anthropic_breakpoints(
    payload: dict[str, Any],
    *,
    system: str | None,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    profile: CacheProfile,
) -> tuple[bool, bool, bool]:
    """Attach explicit cache_control breakpoints to an Anthropic payload.

    Marks, budget permitting: the system block (block-list form), the final
    tool schema, and the conversation tail — the last message's final content
    block, which every tool-continuation request extends. The payload is
    edited in place; caller message lists are copied, never mutated. Returns
    ``(system_marked, tools_marked, tail_marked)``.
    """
    # Only explicit profiles accept cache fields at all; implicit/none
    # endpoints must never receive them (the module contract).
    if profile.mode != "explicit":
        return False, False, False
    system_marked = tools_marked = tail_marked = False
    used = 0
    if system and used < profile.max_breakpoints:
        payload["system"] = [
            {"type": "text", "text": system, "cache_control": _ephemeral_cache_control()}
        ]
        system_marked = True
        used += 1
    if tools and used < profile.max_breakpoints:
        payload["tools"] = _mark_last_tool_breakpoint(tools)
        tools_marked = True
        used += 1
    if used < profile.max_breakpoints:
        marked = _mark_message_tail_breakpoint(messages)
        if marked is not None:
            payload["messages"] = marked
            tail_marked = True
    return system_marked, tools_marked, tail_marked


def openrouter_cache_wiring(
    messages: list[dict[str, Any]],
    tool_payload: list[dict[str, Any]] | None,
    model: str | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]] | None, tuple[bool, bool, bool]]:
    """Wire prompt-cache breakpoints for one OpenRouter request.

    Gated on ``prompt_cache_enabled`` and an Anthropic-routed model. Returns
    ``(wire_messages, tool_payload, (system_marked, tools_marked, tail_marked))``
    with copied lists only; disabled calls return inputs unchanged.
    """
    enabled = _prompt_cache_breakpoints_enabled() and _openrouter_anthropic_model(model)
    wire_messages, system_marked, tail_marked = _openrouter_wire_messages(messages, enabled)
    tools_marked = False
    if tool_payload is not None and enabled:
        tool_payload = _mark_last_tool_breakpoint(tool_payload)
        tools_marked = True
    return wire_messages, tool_payload, (system_marked, tools_marked, tail_marked)
