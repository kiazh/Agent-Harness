"""Tests for deterministic and versioned delegation prompts in orchestrator."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ah.core.orchestrator import Orchestrator


@pytest.fixture
def orchestrator():
    return Orchestrator(agent_factory=MagicMock())


class TestDelegationPromptVersioning:
    """Delegation prompts must be deterministic and versioned."""

    def test_prompt_version_constant_exists(self):
        """Orchestrator must define a prompt version constant."""
        assert hasattr(Orchestrator, "PROMPT_VERSION")
        assert isinstance(Orchestrator.PROMPT_VERSION, str)
        assert len(Orchestrator.PROMPT_VERSION) > 0

    def test_delegation_prompt_is_deterministic(self):
        """Same inputs must produce the same prompt."""
        from ah.core.orchestrator import Orchestrator
        prompt1 = Orchestrator._build_delegation_prompt("agent1", "task1", "context1")
        prompt2 = Orchestrator._build_delegation_prompt("agent1", "task1", "context1")
        assert prompt1 == prompt2

    def test_delegation_prompt_includes_version(self):
        """Prompt must include the version string."""
        from ah.core.orchestrator import Orchestrator
        prompt = Orchestrator._build_delegation_prompt("agent1", "task1", "context1")
        assert Orchestrator.PROMPT_VERSION in prompt

    def test_delegation_prompt_includes_agent_name(self):
        """Prompt must include the agent name."""
        from ah.core.orchestrator import Orchestrator
        prompt = Orchestrator._build_delegation_prompt("my-agent", "task1", "context1")
        assert "my-agent" in prompt

    def test_delegation_prompt_includes_task(self):
        """Prompt must include the task."""
        from ah.core.orchestrator import Orchestrator
        prompt = Orchestrator._build_delegation_prompt("agent1", "my task", "context1")
        assert "my task" in prompt

    def test_delegation_prompt_includes_context(self):
        """Prompt must include the parent context."""
        from ah.core.orchestrator import Orchestrator
        prompt = Orchestrator._build_delegation_prompt("agent1", "task1", "parent context here")
        assert "parent context here" in prompt

    def test_delegation_prompt_empty_context(self):
        """Prompt must handle empty context."""
        from ah.core.orchestrator import Orchestrator
        prompt = Orchestrator._build_delegation_prompt("agent1", "task1", "")
        assert "agent1" in prompt
        assert "task1" in prompt

    def test_delegation_prompt_changes_with_different_inputs(self):
        """Different inputs must produce different prompts."""
        from ah.core.orchestrator import Orchestrator
        prompt1 = Orchestrator._build_delegation_prompt("agent1", "task1", "context1")
        prompt2 = Orchestrator._build_delegation_prompt("agent2", "task1", "context1")
        assert prompt1 != prompt2
