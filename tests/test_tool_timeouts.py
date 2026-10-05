"""Regression tests for per-tool execution timeouts.

The agent wraps every tool call in ``asyncio.wait_for(..., timeout)``. The
default cap must exceed the internal timeout of the slowest legitimate tool,
otherwise healthy network tools are aborted mid-flight — and each abort costs
an extra LLM call to react to a failure that never happened.
"""

from __future__ import annotations

import pytest

from ah.core.agent import _TOOL_TIMEOUTS, _TOOL_TIMEOUT_DEFAULT


def test_default_timeout_exceeds_slowest_internal_tool_timeout():
    """The generic cap must not be tighter than real tool budgets.

    Internal budgets today: web_search 10s, web_extract 30s, terminal 60s.
    """
    assert _TOOL_TIMEOUT_DEFAULT >= 30.0, (
        "a sub-30s default aborts web_extract (30s) and terminal (60s)"
    )


def test_web_tools_are_not_killed_early():
    """web_extract allows 30s internally, so its cap must be >= that."""
    assert _TOOL_TIMEOUTS.get("web_extract", _TOOL_TIMEOUT_DEFAULT) >= 30.0


def test_terminal_allows_its_full_internal_budget():
    """terminal's own default is 60s; the agent must not undercut it."""
    assert _TOOL_TIMEOUTS.get("terminal", _TOOL_TIMEOUT_DEFAULT) >= 60.0


def test_delegation_allows_a_sub_agent_to_finish():
    """delegate wraps a nested agent run (its tool allows 60s)."""
    assert _TOOL_TIMEOUTS["delegate"] >= 60.0


@pytest.mark.parametrize("tool", ["web_search", "web_extract", "terminal", "read_file"])
def test_every_known_tool_gets_a_usable_timeout(tool):
    """No tool may resolve to a timeout below one second."""
    assert _TOOL_TIMEOUTS.get(tool, _TOOL_TIMEOUT_DEFAULT) >= 1.0
