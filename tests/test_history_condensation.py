"""In-loop tool history stays bounded: stale rounds condense, latest stays full."""

import copy
import json
import os
import sys
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from ah.core.agent import BaseReActAgent, ReActAgent
from ah.core.config import config
from ah.core.models import LLMResponse, Session, StreamEvent
from ah.permissions.broker import set_approval_handler


def _msgs(*contents):
    """Build [user, assistant(tc), tool, assistant(tc), tool, …] history."""
    messages = [{"role": "user", "content": "do things"}]
    for i, content in enumerate(contents):
        tc = {"id": f"c{i}", "type": "function", "function": {"name": "t", "arguments": "{}"}}
        messages.append({"role": "assistant", "content": "", "tool_calls": [tc]})
        messages.append({"role": "tool", "content": content, "tool_call_id": f"c{i}"})
    return messages


def test_stale_rounds_condense_latest_stays_full():
    messages = _msgs("H" * 400 + "M" * 400 + "T" * 400, "fresh" * 200)
    BaseReActAgent._condense_history(messages)
    stale, latest = messages[2]["content"], messages[4]["content"]
    assert "condensed" in stale
    assert stale.startswith("H" * 350)
    assert stale.endswith("T" * 200)
    assert len(stale) < 1000
    assert "condensed" not in latest
    assert latest == "fresh" * 200


def test_short_messages_and_first_round_untouched():
    messages = _msgs("small output")
    BaseReActAgent._condense_history(messages)
    assert messages[2]["content"] == "small output"


def test_positionally_addressed_skill_pages_never_condense():
    page = "Skill ecc-python-testing [0:800/20000]:\n" + "x" * 3000
    resource = "ECC resource agents/planner.md [0:400/9000]:\n" + "y" * 3000
    messages = _msgs(page, resource)
    BaseReActAgent._condense_history(messages)
    assert messages[2]["content"] == page
    # Latest round is untouched regardless; shrink it artificially to prove
    # the page rule is what protects stale pages, not recency.
    messages[4]["content"] = resource
    BaseReActAgent._condense_history(messages)
    assert messages[4]["content"] == resource


def test_no_skill_evidence_injects_nothing():
    from ah.skills.registry import SkillRegistry
    from ah.skills.runtime import automatic_skill_context

    skills = SkillRegistry()
    assert automatic_skill_context("hello!", skills=skills) == ""
    assert automatic_skill_context("blorple snazzlewicket", skills=skills) == ""


def test_system_prompt_carries_general_agent_conduct():
    from ah.core.agent import SYSTEM_PROMPT

    assert "general-purpose agent" in SYSTEM_PROMPT
    assert "Never enumerate" in SYSTEM_PROMPT


@pytest.mark.asyncio
async def test_loop_condenses_earlier_terminal_output(tmp_path, monkeypatch):
    """Two big terminal rounds: the first is condensed, the second stays full."""
    (tmp_path / "work").mkdir()
    monkeypatch.setenv(
        "PATH", str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")
    )
    monkeypatch.setattr(config, "workspace_root", str(tmp_path / "work"))
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
    monkeypatch.setattr(
        "ah.core.agent.context_manager.get_latest_images", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(BaseReActAgent, "_reserve_usage", AsyncMock(return_value=None))
    monkeypatch.setattr(BaseReActAgent, "_finish_usage", AsyncMock())

    async def unexpected_approval(card):
        pytest.fail("ordinary workflow requested approval")

    set_approval_handler(unexpected_approval)
    (tmp_path / "work" / "big.txt").write_text("H" * 400 + "M" * 400 + "T" * 400)

    class Provider:
        calls = []

        async def complete(self, **kwargs):
            self.calls.append(copy.deepcopy(kwargs))
            index = len(self.calls) - 1
            if index < 2:
                return LLMResponse(
                    content="",
                    model="fake",
                    usage={"total_tokens": 1},
                    tool_calls=[
                        {
                            "id": str(index),
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": json.dumps({"path": "big.txt"}),
                            },
                        }
                    ],
                )
            return LLMResponse(content="done", model="fake", usage={"total_tokens": 1})

        async def stream_complete(self, **kwargs):
            yield StreamEvent(type="done", response=await self.complete(**kwargs))

    provider = Provider()
    provider.calls = []
    agent = ReActAgent(provider=provider, allowed_tools=["read_file"])
    try:
        response = await agent.run(session.id, "Dump some output twice", verbose=False)
        assert response.content == "done"
        third_prompt = provider.calls[2]["messages"]
        tools = [m for m in third_prompt if m.get("role") == "tool"]
        assert len(tools) == 2
        assert "condensed" in tools[0]["content"]
        assert tools[0]["content"].startswith("H" * 350)
        assert "condensed" not in tools[1]["content"]
        assert len(tools[1]["content"]) > 700
    finally:
        set_approval_handler(None)
