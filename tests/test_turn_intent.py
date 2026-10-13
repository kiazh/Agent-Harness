"""Skill workflows and learned strategy agree about questions versus actions."""

import pytest

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
