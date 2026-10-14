"""Tests for deterministic and versioned delegation prompts in orchestrator."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from machina.core.orchestrator import Orchestrator


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

    @pytest.mark.parametrize(
        "agent2,expect_equal",
        [
            pytest.param("agent1", True, id="same-inputs-identical"),
            pytest.param("agent2", False, id="different-inputs-differ"),
        ],
    )
    def test_delegation_prompt_determinism(self, agent2, expect_equal):
        """Same inputs must produce the same prompt; different inputs must differ."""
        from machina.core.orchestrator import Orchestrator

        prompt1 = Orchestrator._build_delegation_prompt("agent1", "task1", "context1")
        prompt2 = Orchestrator._build_delegation_prompt(agent2, "task1", "context1")
        assert (prompt1 == prompt2) is expect_equal

    def test_delegation_prompt_includes_version(self):
        """Prompt must include the version string."""
        from machina.core.orchestrator import Orchestrator

        prompt = Orchestrator._build_delegation_prompt("agent1", "task1", "context1")
        assert Orchestrator.PROMPT_VERSION in prompt

    def test_delegation_prompt_includes_agent_name(self):
        """Prompt must include the agent name."""
        from machina.core.orchestrator import Orchestrator

        prompt = Orchestrator._build_delegation_prompt("my-agent", "task1", "context1")
        assert "my-agent" in prompt

    def test_delegation_prompt_includes_task(self):
        """Prompt must include the task."""
        from machina.core.orchestrator import Orchestrator

        prompt = Orchestrator._build_delegation_prompt("agent1", "my task", "context1")
        assert "my task" in prompt

    def test_delegation_prompt_includes_context(self):
        """Prompt must include the parent context."""
        from machina.core.orchestrator import Orchestrator

        prompt = Orchestrator._build_delegation_prompt("agent1", "task1", "parent context here")
        assert "parent context here" in prompt

    def test_delegation_prompt_empty_context(self):
        """Prompt must handle empty context."""
        from machina.core.orchestrator import Orchestrator

        prompt = Orchestrator._build_delegation_prompt("agent1", "task1", "")
        assert "agent1" in prompt
        assert "task1" in prompt
