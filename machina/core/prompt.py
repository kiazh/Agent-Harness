"""Prompt-section assembly extracted from ``machina/core/agent.py`` (moves-only).

This module owns message-shape logic: turn-capacity helpers, stale-result
condensation, tool-message appending, and the per-section builders that compose
:class:`~machina.core.agent.BaseReActAgent._prepare_context` output.

Every function body is a verbatim move from ``agent.py`` — no logic changes.
``agent.py`` re-exports the underscore aliases so external callers
(``tests/test_review_round2_fixes.py``, ``test_model_context_limits.py``,
``test_history_condensation.py``, ``test_provider_context_audit.py``) keep
working. The per-section builders each return ``(role, content)`` pieces so
callers can run sectioned-token diagnostics
(:func:`prompt_section_tokens`, compatible with
``machina.core.execution_context.count_prompt_sections``) without changing the
assembled prompt text.

One addition on top of the moves: :func:`build_system_prompt` takes an
optional ``triage`` task class and appends a bounded, deterministic workflow
hint for the two classes that have one (``learn-directory``, ``debug``).
Hints are advisory prompt text only — never enforcement, never tool forcing,
never a provider call; any other class (and ``triage=None``) leaves the
assembled text byte-identical (pinned by ``tests/test_v2_triage_hints.py``).
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any

__all__ = [
    "PROVIDER_CONTENT_KEY",
    "TOOL_RESULT_STALE_HEAD",
    "TOOL_RESULT_STALE_TAIL",
    "TOOL_RESULT_STALE_CAP",
    "SKILL_PAGE_RE",
    "_TOOL_RESULT_STALE_HEAD",
    "_TOOL_RESULT_STALE_TAIL",
    "_TOOL_RESULT_STALE_CAP",
    "_SKILL_PAGE_RE",
    "stale_tool_preview",
    "_stale_tool_preview",
    "turn_model",
    "_turn_model",
    "effective_turn_budget",
    "_effective_turn_budget",
    "request_context_tokens",
    "_request_context_tokens",
    "append_tool_messages",
    "condense_history",
    "build_persona_section",
    "build_capability_section",
    "build_skill_section",
    "build_system_prompt",
    "assemble_turn_prompt",
    "wrap_prompt_with_images",
    "build_turn_messages",
    "prompt_section_tokens",
    "tool_message_limit",
]

# Earlier-round tool results shrink to a head+tail preview before the next
# LLM call (append-time caps already bound each result to ~1k / 9k for
# skill pages). Stale rounds keep orientation at roughly two-thirds size.
# (Moved verbatim from agent.py.)
TOOL_RESULT_STALE_HEAD = 350
TOOL_RESULT_STALE_TAIL = 350
TOOL_RESULT_STALE_CAP = TOOL_RESULT_STALE_HEAD + TOOL_RESULT_STALE_TAIL
_TOOL_RESULT_STALE_HEAD = TOOL_RESULT_STALE_HEAD
_TOOL_RESULT_STALE_TAIL = TOOL_RESULT_STALE_TAIL
_TOOL_RESULT_STALE_CAP = TOOL_RESULT_STALE_CAP
# Positionally addressed skill pages must keep resolving their offsets.
SKILL_PAGE_RE = re.compile(r"^(Skill \S+|ECC resource \S+) \[\d+:\d+/\d+\]:")
_SKILL_PAGE_RE = SKILL_PAGE_RE
# Provider-native content blocks ride on ``LLMResponse.raw`` under this key and
# are copied onto the assistant message by :func:`append_tool_messages` so the
# next tool-continuation request can replay them verbatim (Anthropic extended
# thinking requires the original signed ``thinking`` blocks alongside
# ``tool_use``; normalizing to OpenAI-style shapes would drop them).
PROVIDER_CONTENT_KEY = "provider_content"


def stale_tool_preview(content: str) -> str:
    """Head+tail preview of a stale-round tool result (verdicts live in tails)."""
    return (
        f"{content[:TOOL_RESULT_STALE_HEAD]}"
        f"\n…[earlier output condensed to {TOOL_RESULT_STALE_HEAD}+{TOOL_RESULT_STALE_TAIL} chars"
        f" of {len(content)}; re-run the tool for the full output]…\n"
        f"{content[-TOOL_RESULT_STALE_TAIL:]}"
    )


_stale_tool_preview = stale_tool_preview


def turn_model(session, provider) -> str | None:
    """Best-known model id for capacity: session first, then provider/config.

    The session pins the explicit per-turn choice; the provider object is
    built for the session and only backs it up when the session is unpinned.
    """
    from machina.core.config import config

    for candidate in (
        getattr(session, "model", None),
        getattr(provider, "model", None),
    ):
        if isinstance(candidate, str) and candidate:
            return candidate
    try:
        configured = config.get("model")
        return configured if isinstance(configured, str) and configured else None
    except Exception:
        return None


_turn_model = turn_model


def effective_turn_budget(session, provider) -> int:
    """Compatibility name for request capacity, never cumulative spend."""
    from machina.core.context_windows import context_window_for

    current = turn_model(session, provider)
    resolved = getattr(session, "context_window", None)
    tagged = getattr(session, "context_window_model", None)
    if type(resolved) is int and resolved > 0 and tagged == current:
        return resolved
    return context_window_for(current, provider)


_effective_turn_budget = effective_turn_budget


def request_context_tokens(messages, tools, provider) -> int:
    from machina.core.assembler import get_token_count
    from machina.core.config import config
    from machina.core.images import token_messages
    from machina.core.provider.transport import _tools_payload

    messages, visual_tokens = token_messages(messages)

    output = int(config.get("max_tokens"))
    resolver = getattr(type(provider), "reservation_max_tokens", None)
    if resolver is not None:
        output = resolver(provider, output, config.get("reasoning_effort"))
    return (
        get_token_count(json.dumps(messages, ensure_ascii=False))
        + get_token_count(json.dumps(_tools_payload(tools), ensure_ascii=False))
        + 16
        + 8 * len(messages)
        + output
        + visual_tokens
    )


_request_context_tokens = request_context_tokens


def append_tool_messages(
    messages: list[dict[str, Any]], response, tc: dict[str, Any], content: str
) -> None:
    """Append one assistant batch followed by each of its tool results.

    When the provider returned native content blocks (``response.raw`` carries
    :data:`PROVIDER_CONTENT_KEY`), they are copied onto the assistant message
    so the continuation request can replay them verbatim — Anthropic rejects
    tool continuations whose signed ``thinking`` blocks have been dropped.
    """
    # The execution loop replays results in provider order. Only its first
    # result introduces the assistant message; later results share it.
    if not response.tool_calls or tc is response.tool_calls[0]:
        assistant: dict[str, Any] = {
            "role": "assistant",
            "content": response.content,
            "tool_calls": response.tool_calls or [tc],
        }
        raw = getattr(response, "raw", None)
        native = raw.get(PROVIDER_CONTENT_KEY) if isinstance(raw, dict) else None
        if isinstance(native, list) and native:
            assistant[PROVIDER_CONTENT_KEY] = copy.deepcopy(native)
        messages.append(assistant)
    messages.append(
        {
            "role": "tool",
            "content": content,
            "tool_call_id": tc.get("id", ""),
        }
    )


def condense_history(messages: list[dict[str, Any]]) -> None:
    """Shrink stale tool results in place before the next LLM call.

    Tool messages from earlier rounds keep a head+tail preview; the
    latest batch keeps its append-time previews (normally 1000 chars;
    skill pages up to 9000 chars), including every result in that batch.
    Heads preserve listings, tails preserve verdicts ("1 passed"); the
    marker points back at the tool for the full output. Positionally
    addressed skill pages (``Skill name [o:e/n]`` / ``ECC resource``)
    are never condensed — offsets into them must keep resolving.
    Context storage keeps a separate 500-character result preview, not
    the full output. Re-run the tool when more output is needed.
    """
    last_assistant = -1
    for i, message in enumerate(messages):
        if message.get("role") == "assistant" and message.get("tool_calls"):
            last_assistant = i
    for message in messages[: last_assistant + 1]:
        if message.get("role") != "tool":
            continue
        content = message.get("content", "")
        if (
            isinstance(content, str)
            and len(content) > TOOL_RESULT_STALE_CAP
            and not SKILL_PAGE_RE.match(content)
            and "\n…[earlier output condensed to " not in content
        ):
            message["content"] = stale_tool_preview(content)


def build_persona_section(
    soul_spec,
    allowed_tools: list[str] | None,
    growth_notes,
    growth_state: str,
    rsi_choice: dict,
) -> tuple[str, str]:
    """Persona system section as a ``(role, content)`` piece.

    Verbatim move of the ``resolve_persona`` block of ``_prepare_context``.
    """
    from machina.core.config import config
    from machina.soulspec.runtime import resolve_persona

    effective = resolve_persona(
        soul_spec,
        requested_style=config.get("persona_description") or soul_spec.persona.requested_style,
        notes=growth_notes,
        growth_enabled=growth_state == "active",
    )
    system_prompt = effective.render_prompt(
        can_learn=allowed_tools is None or "learn_personality" in allowed_tools,
        rsi_choice=rsi_choice or {},
    )
    return ("system", system_prompt)


def build_capability_section(
    session,
    tool_names: set[str],
    growth_state: str,
) -> tuple[str, str]:
    """Capability-context suffix as a ``(role, content)`` piece.

    Verbatim move of the ``capability_context`` block of ``_prepare_context``.
    """
    from machina.core.self_knowledge import capability_context

    return (
        "system",
        capability_context(
            session,
            tool_names=tool_names,
            growth_state=growth_state,
        ),
    )


def build_skill_section(
    user_message: str,
    session,
    recent,
    allowed_tools: list[str] | None,
    skill_weights: dict,
    activated,
    system_base: str,
) -> tuple[str, str]:
    """Automatic-skill-context suffix as a ``(role, content)`` piece.

    Verbatim move of the ``automatic_skill_context`` block of
    ``_prepare_context`` (budget arithmetic unchanged): ``available`` is
    measured against ``system_base`` (the persona+capability text), exactly
    as the inline version measured ``system_prompt`` at that point.
    """
    from machina.core.assembler import get_token_count
    from machina.skills.runtime import automatic_skill_context

    query_tokens = get_token_count("\n\n## Current Query\n" + user_message)
    available = (
        session.context_budget
        - min(query_tokens, session.context_budget // 2)
        - get_token_count(system_base)
        - 32
    )
    return (
        "system",
        automatic_skill_context(
            user_message,
            budget=min(1200, session.context_budget // 5, max(0, available)),
            can_read=allowed_tools is None or "skill_read" in allowed_tools,
            can_search=allowed_tools is None or "skill_list" in allowed_tools,
            can_resources=allowed_tools is None or "skill_resource" in allowed_tools,
            recent=recent,
            goal=session.goal,
            skill_weights=skill_weights,
            activated=activated,
        ),
    )


# V2 triage-driven methodology hints (advisory text only, deterministic per
# class, no provider calls, no tool forcing). Bounded: <= 60 tokens each
# (pinned by tests/test_v2_triage_hints.py).
_TRIAGE_HINTS: dict[str, str] = {
    "learn-directory": (
        "Workflow hint: this looks like directory learning — call learn_directory "
        "on the target path first and ground your plan in its digest (hotspots, "
        "entry points, tests) before proposing or making changes."
    ),
    "debug": (
        "Workflow hint: this looks like debugging — reproduce, minimise, "
        "hypothesise, then fix with a regression test that fails without the "
        "change."
    ),
}


def build_system_prompt(
    soul_spec,
    allowed_tools: list[str] | None,
    session,
    user_message: str,
    recent,
    growth_notes,
    growth_state: str,
    rsi_choice: dict,
    activated,
    tool_names: set[str],
    *,
    triage: str | None = None,
) -> str:
    """Full system prompt: persona + capability + skills (+ triage hint).

    Calls the per-section builders in the exact order ``_prepare_context``
    used, so the assembled text is byte-identical to the inline version for
    every triage label without a hint — and for ``triage=None``.

    ``triage`` is the advisory task class recorded by the controller wiring
    hook. A matching hint (deterministic, <= 60 tokens) is appended after the
    skill section; it is prompt text only, never enforcement and never a new
    model call.
    """
    _role, persona_text = build_persona_section(
        soul_spec, allowed_tools, growth_notes, growth_state, rsi_choice or {}
    )
    _role, capability_text = build_capability_section(session, tool_names, growth_state)
    system_base = persona_text + capability_text
    _role, skill_text = build_skill_section(
        user_message,
        session,
        recent,
        allowed_tools,
        (rsi_choice or {}).get("skill_weights", {}),
        activated,
        system_base,
    )
    composed = system_base + skill_text
    hint = _TRIAGE_HINTS.get(triage) if triage else None
    if hint:
        composed = f"{composed}\n\n{hint}"
    return composed


def assemble_turn_prompt(
    assembler,
    system_prompt: str,
    goal,
    recent,
    retrieved_chunks,
    query: str,
) -> str:
    """Assemble the turn prompt via ``PromptAssembler`` (verbatim move)."""
    return assembler.assemble(
        system_prompt=system_prompt,
        goal=goal,
        recent_chunks=recent,
        retrieved_chunks=retrieved_chunks,
        query=query,
    )


def wrap_prompt_with_images(prompt: str, images: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Wrap the assembled prompt as provider messages (verbatim move)."""
    from machina.core.images import image_content

    # Keep the latest image-bearing user turn in recent context useful on
    # follow-up requests and after gateway restart. Raw encodings never go
    # through the text assembler, retrieval embeddings, or display history.
    return [{"role": "user", "content": image_content(prompt, images)}]


def build_turn_messages(prompt: str, images: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Composer returning the ``(role, content)`` message list for a turn."""
    return wrap_prompt_with_images(prompt, images)


def prompt_section_tokens(sections: dict[str, str]) -> dict[str, int]:
    """Token count per prompt section (diagnostics only, never mutates input).

    Thin alias over ``execution_context.count_prompt_sections`` so prompt
    callers have one home for both assembly and diagnostics.
    """
    from machina.core.execution_context import count_prompt_sections

    return count_prompt_sections(sections)


def tool_message_limit(tool_name: str | None) -> int:
    """Append-time preview cap for a tool result (verbatim move).

    Skill pages and status snapshots already have bounded output.
    Preserve continuation offsets and complete JSON beyond the preview cap.
    """
    return (
        9000
        if tool_name in ("skill_read", "skill_resource", "harness_status", "search_documents")
        else 1000
    )
