"""Delegate name-validator tests (Stream A, A4).

The `delegate` tool must validate the target name with zero child work:
no session is created and no provider is touched before the name is known
good. Unknown names fail closed with fuzzy suggestions; `harness-<slug>`
profile scopes are valid delegation targets (resolved via
`AgentRegistry._profile_scope`) and must pass validation.
"""

import pytest

from machina.core.exceptions import ToolError
from machina.tools import agents as agents_tool


class _FakeResult:
    def __init__(self) -> None:
        self.status = "ok"
        self.response = "done"
        self.tokens = 5
        self.needs_approval = []


@pytest.fixture
def mock_orchestrator(monkeypatch):
    """Replace the orchestrator singleton's delegate with a recording fake."""
    from machina.core import orchestrator as orch_module

    calls: list[dict] = []

    async def _fake(agent, task, **kwargs):
        calls.append({"agent": agent, "task": task, **kwargs})
        return _FakeResult()

    monkeypatch.setattr(orch_module.orchestrator, "delegate", _fake)
    return calls


async def test_unknown_name_fails_with_suggestions_and_no_child_work(mock_orchestrator):
    with pytest.raises(ToolError) as exc_info:
        await agents_tool.delegate("resercher", "gather background material")
    message = str(exc_info.value)
    assert "no agent named 'resercher'" in message
    assert "researcher" in message  # fuzzy suggestion preserved
    assert "list_agents" in message
    assert mock_orchestrator == []  # zero child work pre-validation


async def test_profile_scope_name_passes_validation(mock_orchestrator):
    result = await agents_tool.delegate("harness-e2e", "summarise the repo")
    assert mock_orchestrator and mock_orchestrator[0]["agent"] == "harness-e2e"
    assert "harness-e2e" in result


@pytest.mark.parametrize("name", ["harness", "researcher", "coder"])
async def test_builtin_names_pass_validation(name, mock_orchestrator):
    result = await agents_tool.delegate(name, "a self-contained task")
    assert mock_orchestrator and mock_orchestrator[0]["agent"] == name
    assert name in result


async def test_empty_task_still_rejected(mock_orchestrator):
    with pytest.raises(ToolError, match="task must not be empty"):
        await agents_tool.delegate("researcher", "   ")
    assert mock_orchestrator == []


async def test_db_stored_custom_name_passes_validation(monkeypatch, mock_orchestrator):
    """Names missing from the frozen roster get one registry lookup: a
    DB-stored custom agent must still delegate (zero child work only for
    truly unknown names)."""
    from types import SimpleNamespace

    from machina.core import agent_def as agent_def_module

    async def _fake_get(name):
        if name == "my-saved-agent":
            return SimpleNamespace(name="my-saved-agent")
        return None

    monkeypatch.setattr(agent_def_module.agent_registry, "get", _fake_get)
    result = await agents_tool.delegate("my-saved-agent", "a self-contained task")
    assert mock_orchestrator and mock_orchestrator[0]["agent"] == "my-saved-agent"
    assert "my-saved-agent" in result


async def test_unknown_name_with_db_error_still_rejected(monkeypatch, mock_orchestrator):
    from machina.core import agent_def as agent_def_module

    async def _boom(name):
        raise RuntimeError("db down")

    monkeypatch.setattr(agent_def_module.agent_registry, "get", _boom)
    with pytest.raises(ToolError) as exc_info:
        await agents_tool.delegate("not-a-real-agent-xyz", "do work")
    assert "no agent named 'not-a-real-agent-xyz'" in str(exc_info.value)
    assert mock_orchestrator == []  # zero child work
