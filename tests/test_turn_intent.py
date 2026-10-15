"""Skill workflows and learned strategy agree about questions versus actions."""

import pytest

from machina.core.intent import conversational
from machina.memory.rsi import task_domain
from machina.skills.registry import SkillRegistry
from machina.skills.workflow import plan_workflow


@pytest.mark.parametrize(
    "query",
    [
        "hi",
        "thanks!",
        "Hello there!",
        "How does your Python testing work?",
        "Do you have Python testing skills?",
        "Is your code working?",
        "Can you explain your research workflow?",
        "What is memory growth?",
    ],
)
def test_conversation_never_starts_skill_work_or_learned_task_strategy(query, tmp_path):
    assert plan_workflow(query, skills=SkillRegistry(tmp_path)).skills == []
    assert task_domain(query) == "general"


@pytest.mark.parametrize(
    "query",
    [
        "What is the bug? Then can you fix the Python code?",
        "Do you have Python testing? Please run the tests.",
        "Hi, can you review the Python code?",
        "Can you fix the Python code?",
        "Do a Python code review",
        "Can you do a Python code review?",
        "Have a look at the Python test failures",
    ],
)
def test_action_after_a_question_or_greeting_still_routes_to_coding(query, tmp_path):
    assert plan_workflow(query, skills=SkillRegistry(tmp_path)).kind == "coding"
    assert task_domain(query) == "coding"


def test_growth_brief_distinguishes_automatic_learning_from_persona_trials():
    from machina.core.self_knowledge import FEATURES

    assert "automatic" in FEATURES["rsi"]
    assert "three human-rated" in FEATURES["rsi"]
    assert "peer chat" in FEATURES["execution"]


async def test_verified_lesson_guidance_includes_failures_and_user_corrections():
    from machina.memory.growth import GrowthStore

    prompt = await GrowthStore().prompt("harness", notes={})
    assert "observed failure" in prompt
    assert "user correction" in prompt


@pytest.mark.parametrize(
    "query",
    [
        "What is the default persona? And can you change it?",
        "What is the config? Then update the timeout.",
        "How does the gateway work? Please enable debug logging.",
        "What is the default model? Can you set it to gpt-4?",
        "Where is the persona stored? Remove the old one.",
        "What is the bug? Then modify the handler.",
        "ok, now disable the feature flag",
    ],
)
def test_mutating_follow_on_action_after_a_question_routes_to_task(query, tmp_path):
    assert conversational(query) is False
    assert plan_workflow(query, skills=SkillRegistry(tmp_path)).kind != "conversation"


# --- Stream B additions (add-only): classify_task keeps every assert above ---


@pytest.mark.parametrize(
    "query,expected",
    [
        ("hi", "conversational"),
        ("What is memory growth?", "explain"),
        ("Where is the persona stored?", "learn-directory"),
        ("dont implement changes just review", "review-readonly"),
        ("Fix this traceback in one file", "debug"),
        ("Implement user login in auth.py", "implement"),
        ("continue", "ambiguous"),
        ("just write the file", "ambiguous"),
    ],
)
def test_classify_task_agrees_with_conversational_routing(query, expected, tmp_path):
    from machina.core.intent import classify_task

    assert classify_task(query) == expected
    if expected == "conversational":
        assert plan_workflow(query, skills=SkillRegistry(tmp_path)).skills == []
    if expected in {"debug", "implement", "review-readonly"}:
        assert conversational(query) is False or expected == "review-readonly"


def test_classify_task_never_guesses_on_bare_follow_ons():
    from machina.core.intent import classify_task

    for query in ["continue", "do it", "go ahead", "proceed", "and then?"]:
        assert classify_task(query) == "ambiguous"


# --- R4-091: bare English-word technologies are topics, not coding intent ---


@pytest.mark.parametrize("query", ["go ahead", "go on", "go", "rust", "swift"])
def test_bare_technology_follow_ons_do_not_activate_the_coding_pipeline(query, tmp_path):
    """Bare-tech requests classify as ambiguous; the workflow classifier must
    not latch onto the coding pipeline from the token alone."""
    plan = plan_workflow(query, skills=SkillRegistry(tmp_path))
    assert plan.kind != "coding"
    coding_stages = {"inspect and plan", "implement and test", "verify"}
    assert not [stage for stage, _ in plan.stages if stage in coding_stages]


@pytest.mark.parametrize(
    "query",
    ["write a Go server", "build a Rust CLI", "implement a Swift app"],
)
def test_technology_in_an_explicit_task_still_activates_coding(query, tmp_path):
    assert plan_workflow(query, skills=SkillRegistry(tmp_path)).kind == "coding"


def test_follow_up_keeps_an_inherited_technology_stack(tmp_path):
    """A "continue" after a Go task inherits the stack without the raw query
    ever naming "go", so the bare-technology gate must not drop it."""
    history = [{"chunk_type": "user_message", "payload": {"content": "Build a Go server"}}]
    plan = plan_workflow("continue", skills=SkillRegistry(tmp_path), recent=history)
    assert plan.kind == "coding"
