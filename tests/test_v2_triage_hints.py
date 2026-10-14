"""V2: triage-driven methodology hints — advisory, bounded, deterministic.

RED-first (verified failing at HEAD ``e74ed2a``): ``build_system_prompt`` has
no ``triage`` parameter, so the prompt-level pins raise ``TypeError`` and the
end-to-end prompts contain no hint text.

Invariants pinned here:
- hints are ADVISORY text only: appended to the assembled system prompt, never
  a new model call (a tool-free turn still makes exactly one provider call),
  and never tool forcing (no tool calls are injected);
- every other class and ``triage=None`` leave the prompt byte-identical to a
  build without the parameter;
- each hint is bounded (<= 60 tokens) and deterministic;
- both loops (``run`` / ``run_stream``) carry the class hint for the same
  message, through the real ``_prepare_context`` with mocked managers (the
  existing wiring-test pattern).
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from machina.core.assembler import get_token_count
from machina.core.models import LLMResponse, Session, StreamEvent
from machina.core.prompt import build_system_prompt
from machina.soulspec.schema import SoulSpec

MAX_HINT_TOKENS = 60

LEARN_HINT = (
    "Workflow hint: this looks like directory learning — call learn_directory "
    "on the target path first and ground your plan in its digest (hotspots, "
    "entry points, tests) before proposing or making changes."
)
DEBUG_HINT = (
    "Workflow hint: this looks like debugging — reproduce, minimise, "
    "hypothesise, then fix with a regression test that fails without the "
    "change."
)

LEARN_QUERY = "Where is the persona stored?"
DEBUG_QUERY = "Fix this traceback in one file"

# Every non-hinted class plus None/unknown values must be byte-identical.
NO_HINT_TRIAGE = (
    "conversational",
    "explain",
    "review-readonly",
    "implement",
    "ambiguous",
    None,
    "",
    "unknown-class",
)


# --- prompt-level pins ---------------------------------------------------------


def _soul_spec():
    return SoulSpec(
        name="t",
        persona=SoulSpec.Persona(name="t", system_prompt="CORE IDENTITY"),
    )


def _build(*, with_param: bool = False, triage: str | None = None) -> str:
    session = Session(id=uuid.uuid4())
    session.context_budget = 8000
    args = (_soul_spec(), None, session, "hello", [], None, "skipped", {}, [], set())
    if with_param:
        return build_system_prompt(*args, triage=triage)
    return build_system_prompt(*args)


def test_learn_directory_hint_appended_at_the_end():
    base = _build()
    out = _build(with_param=True, triage="learn-directory")
    assert out.startswith(base)
    assert out == base + "\n\n" + LEARN_HINT
    assert out.endswith(LEARN_HINT)


def test_debug_hint_appended_at_the_end():
    base = _build()
    out = _build(with_param=True, triage="debug")
    assert out.startswith(base)
    assert out == base + "\n\n" + DEBUG_HINT


def test_learn_hint_names_the_registered_tool_and_grounding_digest():
    assert "learn_directory" in LEARN_HINT
    assert "digest" in LEARN_HINT


def test_debug_hint_requires_reproduce_minimise_and_a_regression_test():
    assert "reproduce" in DEBUG_HINT
    assert "minimise" in DEBUG_HINT
    assert "regression test" in DEBUG_HINT


@pytest.mark.parametrize("triage", NO_HINT_TRIAGE)
def test_other_classes_and_none_are_byte_identical(triage):
    assert _build(with_param=True, triage=triage) == _build()


def test_none_param_is_identical_to_omitted_param():
    assert _build(with_param=True, triage=None) == _build(with_param=False)


def test_hints_are_class_scoped_and_do_not_leak_across():
    assert LEARN_HINT not in _build(with_param=True, triage="debug")
    assert DEBUG_HINT not in _build(with_param=True, triage="learn-directory")


@pytest.mark.parametrize("hint", [LEARN_HINT, DEBUG_HINT])
def test_hint_length_is_bounded(hint):
    tokens = get_token_count(hint)
    assert 0 < tokens <= MAX_HINT_TOKENS, f"hint is {tokens} tokens"


@pytest.mark.parametrize("triage,hint", [("learn-directory", LEARN_HINT), ("debug", DEBUG_HINT)])
def test_hint_text_is_deterministic(triage, hint):
    assert _build(with_param=True, triage=triage) == _build(with_param=True, triage=triage)


# --- end-to-end: real _prepare_context, mocked managers/provider --------------


def _setup_agent(monkeypatch):
    from machina.core.agent import ReActAgent
    from machina.core.config import config

    for name in (
        "memory_enabled",
        "personality_growth_enabled",
        "rsi_enabled",
        "rag_enabled",
        "memory_consolidation_enabled",
        "learning_review_enabled",
        "compression_enabled",
    ):
        monkeypatch.setattr(config, name, False)
    session = Session(id=uuid.uuid4(), agent_id="v2-hints-test", execution_mode="workspace")
    monkeypatch.setattr("machina.core.agent.session_manager.get", AsyncMock(return_value=session))
    monkeypatch.setattr("machina.core.agent.session_manager.update_activity", AsyncMock())
    for method in (
        "add_chunk",
        "add_chunks_batch",
        "search_archive_text",
        "get_recent_context",
        "get_latest_images",
    ):
        monkeypatch.setattr(
            f"machina.core.agent.context_manager.{method}", AsyncMock(return_value=[])
        )
    monkeypatch.setattr("machina.core.agent.usage_store.reserve", AsyncMock(return_value=None))
    monkeypatch.setattr("machina.core.agent.usage_store.finish", AsyncMock())

    class Provider:
        model = "v2-hint-fixture"

        def __init__(self):
            self.calls = []

        async def complete(self, **kwargs):
            self.calls.append(kwargs)
            return LLMResponse(
                content="Fixture answer", model=self.model, usage={"total_tokens": 5}
            )

        async def stream_complete(self, **kwargs):
            yield StreamEvent(type="done", response=await self.complete(**kwargs))

    provider = Provider()
    agent = ReActAgent(provider=provider, agent_id=session.agent_id)
    return agent, provider, session


@pytest.mark.parametrize(
    "streaming,triage,query,hint",
    [
        (False, "learn-directory", LEARN_QUERY, LEARN_HINT),
        (True, "learn-directory", LEARN_QUERY, LEARN_HINT),
        (False, "debug", DEBUG_QUERY, DEBUG_HINT),
        (True, "debug", DEBUG_QUERY, DEBUG_HINT),
    ],
)
async def test_both_loops_carry_the_class_hint_for_the_same_message(
    monkeypatch, streaming, triage, query, hint
):
    agent, provider, session = _setup_agent(monkeypatch)
    if streaming:
        events = [event async for event in agent.run_stream(session.id, query, verbose=False)]
        done = [event for event in events if event.type == "done"]
        assert done and done[-1].response is not None
        assert done[-1].response.outcome == "completed"
        assert done[-1].response.tool_calls == []  # advisory: no tool forcing
    else:
        response = await agent.run(session.id, query, verbose=False)
        assert response.outcome == "completed"
        assert response.tool_calls == []  # advisory: no tool forcing
    assert agent._last_triage == triage
    assert len(provider.calls) == 1, "advisory hints must not add a model call"
    prompt = provider.calls[0]["messages"][0]["content"]
    assert hint in prompt


@pytest.mark.parametrize("query", ["hi", "What is memory growth?"])
async def test_no_hint_reaches_the_prompt_for_greeting_or_plain_question(monkeypatch, query):
    agent, provider, session = _setup_agent(monkeypatch)
    await agent.run(session.id, query, verbose=False)
    assert agent._last_triage in {"conversational", "explain"}
    prompt = provider.calls[0]["messages"][0]["content"]
    assert "Workflow hint:" not in prompt
