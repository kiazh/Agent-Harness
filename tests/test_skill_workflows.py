"""Default skills coordinate through native workflows, including follow-up turns."""

from pathlib import Path

import pytest

from ah.skills.registry import SkillRegistry


def test_coding_workflow_composes_domain_testing_and_verification(tmp_path, monkeypatch):
    from ah.skills.workflow import plan_workflow

    monkeypatch.setenv("AGENT_HARNESS_SKILLS_DIR", str(tmp_path))
    plan = plan_workflow("Implement a Python API with authentication", skills=SkillRegistry())
    names = {s.name for s in plan.skills}
    assert {
        "ecc-python-patterns",
        "ecc-tdd-workflow",
        "ecc-security-review",
        "ecc-verification-loop",
    } <= names
    assert plan.kind == "coding"
    assert len(plan.skills) <= 6


def test_followup_inherits_stack_but_new_explicit_stack_wins(tmp_path, monkeypatch):
    from ah.skills.workflow import plan_workflow

    monkeypatch.setenv("AGENT_HARNESS_SKILLS_DIR", str(tmp_path))
    skills = SkillRegistry()
    history = [{"chunk_type": "user_message", "payload": {"content": "Build a Python API"}}]
    followup = plan_workflow("Now add unit tests", skills=skills, recent=history)
    assert "ecc-python-testing" in {s.name for s in followup.skills}
    changed = plan_workflow("Now design Docker containers", skills=skills, recent=history)
    assert "ecc-docker-patterns" in {s.name for s in changed.skills}
    assert "ecc-python-testing" not in {s.name for s in changed.skills}
    greeting = plan_workflow("hello thanks", skills=skills, recent=history)
    assert not greeting.skills


def test_automatic_workflow_prompt_is_bounded_and_native(tmp_path, monkeypatch):
    from ah.core.assembler import get_token_count
    from ah.skills.runtime import automatic_skill_context

    monkeypatch.setenv("AGENT_HARNESS_SKILLS_DIR", str(tmp_path))
    text = automatic_skill_context(
        "Implement a Python API with authentication", skills=SkillRegistry()
    )
    assert "Workflow" in text and "ecc-verification-loop" in text
    assert "native" in text
    assert get_token_count(text) <= 1200


def test_resource_paths_resolve_skill_relative_names(tmp_path, monkeypatch):
    import ah.skills.registry as module
    from ah.tools.skills import skill_resource

    monkeypatch.setenv("AGENT_HARNESS_SKILLS_DIR", str(tmp_path))
    skills = SkillRegistry()
    skills.load_all()
    monkeypatch.setattr(module, "skill_registry", skills)
    bundle = skills._ecc_bundle
    skill = next(
        s
        for s in skills.list_skills()
        if s.source_type == "builtin-ecc" and bundle.resource_paths(s)
    )
    full = bundle.resource_paths(skill)[0]
    relative = full.removeprefix(
        str(__import__("pathlib").PurePosixPath(skill.metadata["bundle_path"]).parent) + "/"
    )
    assert skill_resource(skill.name, relative) == skill_resource(skill.name, full)


def test_skill_read_search_returns_real_continuation_offsets(tmp_path, monkeypatch):
    import re

    import ah.skills.registry as module
    from ah.tools.skills import skill_read

    skills = SkillRegistry(tmp_path)
    content = "Introduction ß\n\n## Testing\nRun tests before completion.\n"
    skills.create_skill("guide", "A guide", content)
    monkeypatch.setattr(module, "skill_registry", skills)
    text = skill_read("guide", query="Testing", limit=30)
    match = re.search(r"\[(\d+):(\d+)/", text)
    start, end = map(int, match.groups())
    assert content[start:end] in text
    assert "## Testing" in text
    assert start == content.index("## Testing")


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_native_workflow_reads_writes_tests_and_verifies(streaming, tmp_path, monkeypatch):
    """Exercise real skill, file and terminal tools through both agent loops and broker."""
    import copy
    import json
    import os
    import sys
    import uuid
    from unittest.mock import AsyncMock

    from ah.core.agent import BaseReActAgent, ReActAgent
    from ah.core.config import config
    from ah.core.models import LLMResponse, Session, StreamEvent
    from ah.permissions.broker import set_approval_handler

    (tmp_path / "tests").mkdir()
    monkeypatch.setenv(
        "PATH", str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")
    )
    monkeypatch.setattr(config, "workspace_root", str(tmp_path))
    monkeypatch.setattr(config, "execution_mode", "ask")
    monkeypatch.setenv("AGENT_HARNESS_SKILLS_DIR", str(tmp_path / "skills"))
    for field in ("memory_enabled", "rag_enabled", "personality_growth_enabled"):
        monkeypatch.setattr(config, field, False)
    session = Session(id=uuid.uuid4(), context_budget=8000)
    monkeypatch.setattr("ah.core.agent.session_manager.get", AsyncMock(return_value=session))
    monkeypatch.setattr("ah.core.agent.session_manager.update_activity", AsyncMock())
    monkeypatch.setattr(
        "ah.core.agent.context_manager.get_recent_context", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        "ah.core.agent.context_manager.search_archive_text", AsyncMock(return_value=[])
    )
    monkeypatch.setattr("ah.core.agent.context_manager.add_chunks_batch", AsyncMock())
    monkeypatch.setattr(BaseReActAgent, "_reserve_usage", AsyncMock(return_value=None))
    monkeypatch.setattr(BaseReActAgent, "_finish_usage", AsyncMock())

    async def unexpected_approval(card):
        pytest.fail("ordinary workflow requested approval")

    set_approval_handler(unexpected_approval)
    actions = [
        ("skill_read", {"skill_name": "ecc-python-testing", "query": "pytest", "limit": 1500}),
        (
            "write_file",
            {"path": "tests/test_added.py", "content": "def test_sum():\n    assert 2 + 2 == 4\n"},
        ),
        ("terminal", {"command": "python -m pytest tests -q"}),
        ("skill_read", {"skill_name": "ecc-verification-loop", "limit": 500}),
    ]

    class Provider:
        calls = []

        async def complete(self, **kwargs):
            self.calls.append(copy.deepcopy(kwargs))
            index = len(self.calls) - 1
            if index < len(actions):
                name, args = actions[index]
                return LLMResponse(
                    content="",
                    model="fake",
                    usage={"total_tokens": 1},
                    tool_calls=[
                        {
                            "id": str(index),
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)},
                        }
                    ],
                )
            assert any(
                "1 passed" in str(message.get("content", "")) for message in kwargs["messages"]
            )
            return LLMResponse(
                content="Verified: one test passed.", model="fake", usage={"total_tokens": 1}
            )

        async def stream_complete(self, **kwargs):
            yield StreamEvent(type="done", response=await self.complete(**kwargs))

    provider = Provider()
    provider.calls = []
    agent = ReActAgent(
        provider=provider,
        allowed_tools=["skill_read", "skill_resource", "skill_list", "write_file", "terminal"],
    )
    try:
        if streaming:
            events = [
                event
                async for event in agent.run_stream(
                    session.id, "Implement and test a Python function", verbose=False
                )
            ]
            response = next(event.response for event in events if event.type == "done")
        else:
            response = await agent.run(
                session.id, "Implement and test a Python function", verbose=False
            )
        assert response.content == "Verified: one test passed."
        assert not response.needs_approval
        assert (tmp_path / "tests/test_added.py").is_file()
        first = provider.calls[0]["messages"][0]["content"]
        assert "Workflow (coding)" in first and "ecc-verification-loop" in first
    finally:
        set_approval_handler(None)


def test_explicit_framework_does_not_inherit_prior_language(tmp_path, monkeypatch):
    from ah.skills.workflow import plan_workflow

    monkeypatch.setenv("AGENT_HARNESS_SKILLS_DIR", str(tmp_path))
    history = [{"chunk_type": "user_message", "payload": {"content": "Build a PostgreSQL API"}}]
    plan = plan_workflow("Add FastAPI authentication", skills=SkillRegistry(), recent=history)
    assert "ecc-postgres-patterns" not in {s.name for s in plan.skills}
    assert "ecc-fastapi-patterns" == plan.skills[0].name


def test_local_trigger_is_an_automatically_loaded_anchor(tmp_path, monkeypatch):
    from ah.skills.runtime import automatic_skill_context

    monkeypatch.setenv("AGENT_HARNESS_SKILLS_DIR", str(tmp_path))
    skills = SkillRegistry()
    skills.create_skill(
        "team-workflow",
        "Follow team Python conventions",
        "TEAM RULE: use our existing dependency injection.",
        triggers=["python"],
    )
    text = automatic_skill_context("Implement a Python API", skills=skills)
    assert "TEAM RULE: use our existing dependency injection." in text


@pytest.mark.asyncio
async def test_resource_integrity_is_not_bypassed_by_tool_result_cache(tmp_path, monkeypatch):
    import shutil

    import ah.skills.registry as module
    from ah.skills.bundle import ECCBundle
    from ah.tools.base import registry

    monkeypatch.setenv("AGENT_HARNESS_SKILLS_DIR", str(tmp_path / "local"))
    skills = SkillRegistry()
    skills.load_all()
    source = skills._ecc_bundle.root
    copied = tmp_path / "copied"
    copied.mkdir()
    for name in ["manifest.json", "LICENSE"]:
        shutil.copyfile(source / name, copied / name)
    skills._ecc_bundle = ECCBundle(copied)
    monkeypatch.setattr(module, "skill_registry", skills)
    args = {"skill_name": "ecc-python-patterns", "path": "LICENSE"}
    assert "MIT" in await registry.execute("skill_resource", **args)
    (copied / "LICENSE").write_text("tampered")
    assert "integrity mismatch" in await registry.execute("skill_resource", **args)


def test_followup_uses_latest_stack_instead_of_old_goal(tmp_path, monkeypatch):
    from ah.skills.workflow import plan_workflow

    monkeypatch.setenv("AGENT_HARNESS_SKILLS_DIR", str(tmp_path))
    history = [
        {"chunk_type": "user_message", "payload": {"content": "Now use Python for the API"}},
        {"chunk_type": "user_message", "payload": {"content": "Build a Docker deployment"}},
    ]
    plan = plan_workflow(
        "Now add edge case tests",
        skills=SkillRegistry(),
        recent=history,
        goal="Build a Docker deployment",
    )
    assert plan.skills[0].name == "ecc-python-testing"
    assert "ecc-docker-patterns" not in {s.name for s in plan.skills}


def test_workflow_does_not_add_unrequested_installers_or_animation(tmp_path, monkeypatch):
    from ah.skills.workflow import plan_workflow

    monkeypatch.setenv("AGENT_HARNESS_SKILLS_DIR", str(tmp_path))
    skills = SkillRegistry()
    python = plan_workflow("Implement a Python API with authentication", skills=skills)
    assert "ecc-generating-python-installer" not in {s.name for s in python.skills}
    react = plan_workflow("Build a React component with hooks", skills=skills)
    assert not any("motion-" in s.name for s in react.skills)
