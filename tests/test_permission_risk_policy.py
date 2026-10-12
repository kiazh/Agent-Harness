"""Routine tools must run without cards; risky effects must still pause."""

import json
import uuid

import pytest

from machina.core.config import config
from machina.permissions.policy import build_request, decide
from machina.permissions.tools import action_for_tool


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "Users" / "shadow" / "project"
    root.mkdir(parents=True)
    monkeypatch.setattr(config, "workspace_root", str(root))
    monkeypatch.setattr(config, "execution_mode", "ask")
    return root


@pytest.mark.parametrize(
    "name,args",
    [
        ("read_file", {"path": "README.md"}),
        ("write_file", {"path": "notes.md", "content": "hello"}),
        ("list_files", {}),
        ("search_files", {"pattern": "credential", "path": "."}),
        ("web_search", {"query": "Python documentation"}),
        ("web_extract", {"url": "https://example.com"}),
        ("search_documents", {"query": "credentials"}),
        ("terminal", {"command": "git status --short"}),
        ("terminal", {"command": "rg permissions ah"}),
        ("terminal", {"command": "pytest tests -q"}),
        ("terminal", {"command": "npm run build"}),
        ("terminal", {"command": "uv run pytest tests"}),
        ("terminal", {"command": "python -m pytest tests"}),
        ("terminal", {"command": "whoami"}),
        ("terminal", {"command": "cat .git/HEAD"}),
    ],
)
def test_default_allows_routine_tools(workspace, name, args):
    request = build_request(tool=name, **action_for_tool(name, args))
    decision = decide(request)
    assert decision.verdict == "allowed", decision.reason


@pytest.mark.parametrize("path", ["docs/credentials.md", "secrets.py", ".env.example"])
def test_security_documentation_is_not_credential_access(workspace, path):
    request = build_request(operation="file.read", targets=[path])
    assert decide(request).verdict == "allowed"


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf .",
        "git reset --hard",
        "git clean -fd",
        "git push origin main",
        "git checkout -- notes.md",
        "git restore notes.md",
        "sudo ls",
        "cat .env",
        "cat /etc/shadow",
        "cat ~/.ssh/id_rsa",
        "cat ../outside.txt",
        "python -c print(1)",
        "npm install",
        "npm run deploy",
        "find . -delete",
        "git -c alias.status=whoami status",
        "curl https://example.com",
        "bash script.sh",
    ],
)
def test_dangerous_or_unclassified_commands_still_ask(workspace, command):
    request = build_request(tool="terminal", **action_for_tool("terminal", {"command": command}))
    assert decide(request).verdict == "pending"


@pytest.mark.parametrize("path", [".env", ".env.local", ".ssh/id_rsa", ".aws/credentials"])
@pytest.mark.parametrize("mode", ["ask", "workspace", "sandbox", "full"])
def test_credentials_always_require_exact_approval(workspace, path, mode):
    request = build_request(operation="file.read", targets=[path], mode=mode)
    assert decide(request).verdict == "pending"


def test_workspace_boundary_still_requires_approval(workspace):
    request = build_request(operation="file.write", targets=["../outside.txt"], content="x")
    assert decide(request).verdict == "pending"


def test_terminal_symlink_to_outside_still_requires_approval(workspace):
    outside = workspace.parent / "private.txt"
    outside.write_text("private")
    (workspace / "link").symlink_to(outside)
    request = build_request(tool="terminal", **action_for_tool("terminal", {"command": "cat link"}))
    assert decide(request).verdict == "pending"


def test_terminal_symlink_to_credentials_still_requires_approval(workspace):
    (workspace / ".env").write_text("fake-secret")
    (workspace / "public").symlink_to(workspace / ".env")
    request = build_request(
        tool="terminal", **action_for_tool("terminal", {"command": "cat public"})
    )
    assert decide(request).verdict == "pending"


def test_grouped_file_option_cannot_use_full_grant_to_read_credentials(workspace):
    request = build_request(
        tool="terminal",
        mode="full",
        session_id="session",
        **action_for_tool("terminal", {"command": "grep -if.env README.md"}),
    )
    grant = {
        "mode": "full",
        "capability": "session",
        "digest": "full-mode-session",
        "session_id": "session",
    }
    assert decide(request, [grant]).verdict == "pending"


@pytest.mark.parametrize(
    "command",
    [
        "cp -t/tmp README.md",
        "grep -f.env README.md",
        "pytest -oaddopts=whatever",
        "cp config.txt .git/config",
        "touch .git/config",
        "touch .machina/config.yaml",
    ],
)
def test_command_options_and_security_settings_need_review(workspace, command):
    request = build_request(tool="terminal", **action_for_tool("terminal", {"command": command}))
    assert decide(request).verdict == "pending"


async def test_docker_routine_project_work_has_writable_mount(workspace, monkeypatch):
    from machina.permissions.broker import clear_execution_context, permission_broker
    from machina.tools import terminal as backend

    seen = []

    def capture(argv, *args, **kwargs):
        seen.extend(argv)
        return "ok"

    monkeypatch.setattr(config, "terminal_sandbox", "docker")
    monkeypatch.setattr(backend, "_run_bounded", capture)
    request = build_request(
        tool="terminal",
        backend="sandbox",
        **action_for_tool("terminal", {"command": "touch result.txt"}),
    )
    try:
        await permission_broker.guard(request)
        assert await backend.terminal("touch result.txt") == "ok"
        mount = seen[seen.index("--mount") + 1]
        assert "readonly" not in mount
        assert "--read-only" in seen  # container root remains read-only
    finally:
        clear_execution_context()


def test_extension_approval_binds_arguments(workspace, monkeypatch):
    from machina.tools.base import Tool, registry

    monkeypatch.setitem(
        registry._tools,
        "extension_write",
        Tool(
            name="extension_write",
            description="extension",
            parameters={},
            func=lambda: None,
            effects=("fs.write",),
        ),
    )
    first = build_request(
        tool="extension_write",
        **action_for_tool("extension_write", {"path": "notes.md", "content": "one"}),
    )
    changed = build_request(
        tool="extension_write",
        **action_for_tool("extension_write", {"path": "notes.md", "content": "two"}),
    )
    assert decide(first).verdict == "pending"
    assert first.digest != changed.digest


async def test_real_tool_loop_writes_and_reads_without_approval(workspace, monkeypatch):
    from machina.core.agent import BaseReActAgent
    from machina.core.context import context_manager
    from machina.core.models import LLMResponse
    from machina.permissions.broker import set_approval_handler

    class Agent(BaseReActAgent):
        def __init__(self):
            self.agent_id = "risk-test"
            self.allowed_tools = None

    async def no_db(*args, **kwargs):
        return None

    async def unexpected_card(card):
        pytest.fail("routine tool requested approval")

    monkeypatch.setattr(context_manager, "add_chunk", no_db)
    messages, made = [], []
    set_approval_handler(unexpected_card)
    try:
        for name, args in [
            ("write_file", {"path": "notes.md", "content": "hello"}),
            ("read_file", {"path": "notes.md"}),
        ]:
            response = LLMResponse(
                content="",
                model="test",
                tool_calls=[
                    {
                        "id": name,
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    }
                ],
            )
            events = [
                event
                async for event in Agent()._execute_tool_calls_stream(
                    response, messages, made, uuid.uuid4()
                )
            ]
            assert not any(event.type == "needs_approval" for event in events)
        assert (workspace / "notes.md").read_text() == "hello"
        assert "hello" in made[-1]["result_preview"]
    finally:
        set_approval_handler(None)


async def test_broker_authorized_terminal_uses_project_cwd(workspace):
    from machina.permissions.broker import clear_execution_context, permission_broker
    from machina.tools.terminal import terminal

    request = build_request(tool="terminal", **action_for_tool("terminal", {"command": "pwd"}))
    try:
        await permission_broker.guard(request)
        assert (await terminal("pwd")).strip() == str(workspace)
    finally:
        clear_execution_context()


async def test_project_test_runner_executes_without_card(workspace):
    from machina.permissions.broker import clear_execution_context, permission_broker
    from machina.tools.terminal import terminal

    request = build_request(
        tool="terminal", **action_for_tool("terminal", {"command": "python -m pytest --version"})
    )
    try:
        await permission_broker.guard(request)
        assert "pytest" in await terminal("python -m pytest --version")
    finally:
        clear_execution_context()
