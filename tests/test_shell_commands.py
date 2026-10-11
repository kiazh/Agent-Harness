"""Shell grammar obeys broker approval; safe chains stay usable without cards."""

import os
import uuid
from unittest.mock import AsyncMock

import pytest

from ah.core.config import config
from ah.core.exceptions import ValidationError
from ah.permissions import store
from ah.permissions.broker import (
    NeedsApproval,
    clear_execution_context,
    permission_broker,
    set_approval_handler,
)
from ah.permissions.policy import build_request, decide
from ah.permissions.tools import action_for_tool
from ah.tools.terminal import terminal


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "workspace_root", str(tmp_path))
    monkeypatch.setattr(config, "execution_mode", "ask")
    monkeypatch.setattr(config, "terminal_sandbox", "local")
    monkeypatch.setattr(store, "_db", AsyncMock(return_value=None))
    set_approval_handler(None)
    clear_execution_context()
    yield tmp_path
    clear_execution_context()
    set_approval_handler(None)


def request(command):
    return build_request(
        tool="terminal",
        session_id=str(uuid.uuid4()),
        agent_id="shell-test",
        **action_for_tool("terminal", {"command": command}),
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell fixture")
@pytest.mark.parametrize("separator", [" && ", "; ", "\n"])
async def test_reported_safe_chain_executes_without_approval(workspace, separator):
    command = separator.join(['echo "shell ok"', "uname -s", "date -u", "pwd"])
    req = request(command)
    assert decide(req).verdict == "allowed"
    await permission_broker.guard(req)
    result = await terminal(command)
    assert result.startswith("shell ok\n")
    assert str(workspace) in result
    assert not result.is_error


async def test_routine_pipe_has_real_shell_semantics(workspace):
    command = "echo shell-ok | cat"
    await permission_broker.guard(request(command))
    assert await terminal(command) == "shell-ok\n"


@pytest.mark.parametrize(
    "command",
    [
        "echo ok; cat .env",
        "echo ok && sudo true",
        "echo ok; rm -rf unused",
        "echo ok > output.txt",
        'echo "$(pwd)"',
        "echo ok &",
        "echo ok | bash",
    ],
)
async def test_risky_shell_pauses_for_exact_approval(workspace, command):
    req = request(command)
    assert req.shell_payload == command
    assert decide(req).verdict == "pending"
    with pytest.raises(NeedsApproval) as pause:
        await permission_broker.guard(req)
    assert pause.value.approval["operation"] == "process.exec"
    assert command in pause.value.approval["proposal"]["argv"]
    assert not (workspace / "output.txt").exists()


async def test_approved_redirect_executes_and_changed_payload_cannot_reuse_snapshot(workspace):
    command = "echo approved > output.txt"
    req = request(command)
    with pytest.raises(NeedsApproval):
        await permission_broker.guard(req)
    assert await store.resolve_decision(req.request_id, "approved", "tui")
    await permission_broker.guard(req)
    with pytest.raises(ValidationError, match="differs"):
        await terminal("echo changed > other.txt")
    await terminal(command)
    assert (workspace / "output.txt").read_text() == "approved\n"
    assert not (workspace / "other.txt").exists()


async def test_literal_metacharacters_remain_literal_argv(workspace):
    command = "echo 'a;b&c$HOME`pwd`'"
    req = request(command)
    assert req.shell_payload == ""
    await permission_broker.guard(req)
    assert await terminal(command) == "a;b&c$HOME`pwd`\n"


async def test_unbrokered_shell_cannot_launch(workspace):
    with pytest.raises(ValidationError):
        await terminal("echo no > output.txt")
    assert not (workspace / "output.txt").exists()


async def test_approved_command_options_are_not_hard_blocked(workspace):
    command = "git -c color.ui=false --version"
    req = request(command)
    with pytest.raises(NeedsApproval):
        await permission_broker.guard(req)
    assert await store.resolve_decision(req.request_id, "approved", "tui")
    await permission_broker.guard(req)
    assert (await terminal(command)).startswith("git version")
