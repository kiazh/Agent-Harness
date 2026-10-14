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
        ("web_crawl", {"url": "https://example.com"}),
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


DANGEROUS_OR_REVIEW_COMMANDS = [
    # dangerous / unclassified commands
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
    # GNU find output-writing primaries write attacker-directed files and
    # must require file-write review, not routine classification.
    "find . -fprint captured.txt",
    "find . -fprint0 captured.bin",
    "find . -fprintf report.txt %p",
    "find . -fls listing.txt",
    "git -c alias.status=whoami status",
    "curl https://example.com",
    "bash script.sh",
    # command options / security settings
    "cp -t/tmp README.md",
    "grep -f.env README.md",
    "pytest -oaddopts=whatever",
    "cp config.txt .git/config",
    "touch .git/config",
    "touch .machina/config.yaml",
]


@pytest.mark.parametrize("command", DANGEROUS_OR_REVIEW_COMMANDS)
def test_dangerous_or_review_commands_ask(workspace, command):
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


def test_full_grant_allows_all_without_approval(workspace):
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
    # Full mode is explicit no-approval authority: a live session grant
    # allows everything without approval cards.
    assert decide(request, [grant]).verdict == "allowed"


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


# 2026-10-14 review R01: recursive searches used to return credential
# material (visible `credentials`/`production.env`, hidden `.env` via
# `grep -d recurse`) without credential_access approval.
@pytest.mark.parametrize(
    "command",
    [
        "rg REVIEW_TOKEN .",
        "rg REVIEW_TOKEN",
        "grep -d recurse REVIEW_TOKEN .",
        "grep -drecurse REVIEW_TOKEN .",
        "grep --directories=recurse REVIEW_TOKEN .",
    ],
)
def test_recursive_search_commands_require_review(workspace, command):
    request = build_request(tool="terminal", **action_for_tool("terminal", {"command": command}))
    decision = decide(request)
    assert decision.verdict == "pending", (command, decision.reason)
    assert "opaque_execution_review" in decision.reason


def test_single_file_search_reads_stay_routine(workspace):
    (workspace / "README.md").write_text("REVIEW_TOKEN=docs\n", encoding="utf-8")
    for command in ("rg REVIEW_TOKEN README.md", "grep REVIEW_TOKEN README.md"):
        request = build_request(
            tool="terminal", **action_for_tool("terminal", {"command": command})
        )
        assert decide(request).verdict == "allowed", command


def test_sensitive_search_operand_requires_credential_access(workspace):
    request = build_request(
        tool="terminal", **action_for_tool("terminal", {"command": "rg REVIEW_TOKEN credentials"})
    )
    decision = decide(request)
    assert decision.verdict == "pending"
    assert "credential_access" in decision.reason


# 2026-10-14 review R02: `cp -R source/. target` overwrites
# target/.git/config exactly like the direct write that already pauses.
def test_recursive_copy_reaching_protected_descendants_requires_review(workspace):
    source_git = workspace / "source" / ".git"
    source_git.mkdir(parents=True)
    (source_git / "config").write_text("attacker-controlled\n", encoding="utf-8")
    target_git = workspace / "target" / ".git"
    target_git.mkdir(parents=True)
    (target_git / "config").write_text("original\n", encoding="utf-8")

    def request(command: str):
        return build_request(tool="terminal", **action_for_tool("terminal", {"command": command}))

    assert decide(request("cp source/.git/config target/.git/config")).verdict == "pending"
    recursive = decide(request("cp -R source/. target"))
    assert recursive.verdict == "pending"
    assert "security_settings" in recursive.reason
    # Directory moves recurse without a flag; same protected descendants.
    assert decide(request("mv source/. target")).verdict == "pending"


def test_recursive_copy_without_protected_descendants_stays_routine(workspace):
    plain = workspace / "plain"
    plain.mkdir()
    (plain / "notes.txt").write_text("hello\n", encoding="utf-8")
    (workspace / "drop").mkdir()
    request = build_request(
        tool="terminal", **action_for_tool("terminal", {"command": "cp -R plain/ drop/"})
    )
    assert decide(request).verdict == "allowed"


# 2026-10-14 adversarial re-verification, R01 follow-up: value-taking options
# consumed no values, so `rg -g '*' REVIEW_TOKEN` read the glob value as the
# pattern and the pattern as a (non-directory) path; the default recursive
# walk then escaped review and returned synthetic credentials from
# `credentials`/`production.env`/`.env` without approval.
@pytest.mark.parametrize(
    "command",
    [
        "rg -g '*' REVIEW_TOKEN",
        "rg --glob '*' REVIEW_TOKEN",
        "rg --iglob '*' REVIEW_TOKEN",
        "rg -g '*' REVIEW_TOKEN .",
        "rg --glob '*' REVIEW_TOKEN .",
        "rg -t md REVIEW_TOKEN",
        "rg --max-depth 1 REVIEW_TOKEN",
        "rg -m 5 REVIEW_TOKEN",
        "rg -j 4 REVIEW_TOKEN",
        "rg -A 2 REVIEW_TOKEN",
        "rg -e REVIEW_TOKEN",
        "rg -f patterns.txt",
    ],
)
def test_rg_value_options_cannot_hide_the_default_walk(workspace, command):
    (workspace / "patterns.txt").write_text("REVIEW_TOKEN\n", encoding="utf-8")
    request = build_request(tool="terminal", **action_for_tool("terminal", {"command": command}))
    decision = decide(request)
    assert decision.verdict == "pending", (command, decision.reason)
    assert "opaque_execution_review" in decision.reason


@pytest.mark.parametrize(
    "command",
    [
        "rg REVIEW_TOKEN README.md",
        "rg -g '*' REVIEW_TOKEN README.md",
        "rg --glob '*' REVIEW_TOKEN README.md",
        "rg -t md REVIEW_TOKEN README.md",
        "rg -m 1 REVIEW_TOKEN README.md",
        "rg -A 2 REVIEW_TOKEN README.md",
        "rg -e REVIEW_TOKEN README.md",
        "rg --regexp=REVIEW_TOKEN README.md",
        "rg -f patterns.txt README.md",
    ],
)
def test_rg_value_options_with_regular_file_operand_stay_routine(workspace, command):
    (workspace / "README.md").write_text("REVIEW_TOKEN=docs\n", encoding="utf-8")
    (workspace / "patterns.txt").write_text("REVIEW_TOKEN\n", encoding="utf-8")
    request = build_request(tool="terminal", **action_for_tool("terminal", {"command": command}))
    assert decide(request).verdict == "allowed", command


# 2026-10-14 adversarial re-verification, R02 follow-up (a): an inspection
# truncated by the entry cap cannot prove the absence of protected
# components; the scan used to fail open and let the copy write a .git
# nested past 4,096 entries without approval.
def test_recursive_copy_beyond_inspection_cap_requires_review(workspace):
    bulk = workspace / "big" / "bulk"
    bulk.mkdir(parents=True)
    for index in range(4200):
        (bulk / f"f{index:04d}").touch()
    deep_git = bulk / "deep" / ".git"
    deep_git.mkdir(parents=True)
    (deep_git / "config").write_text("attacker-controlled\n", encoding="utf-8")
    (workspace / "target").mkdir()
    request = build_request(
        tool="terminal", **action_for_tool("terminal", {"command": "cp -R big/. target"})
    )
    decision = decide(request)
    assert decision.verdict == "pending"
    assert "security_settings" in decision.reason


# 2026-10-14 adversarial re-verification, R02 follow-up (b): the scanner
# skipped symlink entries even when the effective copy dereferences them
# (`cp -L`), so a link to a directory containing .git was copied without
# approval. Descendants of a dereferenced link are written by the copy and
# must be inspected; a link copied as a link (default / -P) writes none.
def test_recursive_copy_dereferencing_symlink_to_protected_dir_requires_review(workspace):
    real = workspace / "real"
    (real / ".git").mkdir(parents=True)
    (real / ".git" / "config").write_text("attacker-controlled\n", encoding="utf-8")
    (workspace / "source").mkdir()
    (workspace / "source" / "evil").symlink_to(real, target_is_directory=True)
    (workspace / "target").mkdir()

    def request(command):
        return build_request(tool="terminal", **action_for_tool("terminal", {"command": command}))

    for command in ("cp -RL source/. target", "cp -R --dereference source/. target"):
        decision = decide(request(command))
        assert decision.verdict == "pending", (command, decision.reason)
        assert "security_settings" in decision.reason
    # Default and -P keep links as links: no descendants are written.
    for command in ("cp -R source/. target", "cp -RP source/. target"):
        assert decide(request(command)).verdict == "allowed", command


def test_recursive_copy_of_link_named_git_requires_review(workspace):
    real_git = workspace / "real-git"
    real_git.mkdir()
    (real_git / "config").write_text("attacker-controlled\n", encoding="utf-8")
    source = workspace / "source2"
    source.mkdir()
    (source / ".git").symlink_to(real_git, target_is_directory=True)
    (workspace / "target2").mkdir()
    request = build_request(
        tool="terminal", **action_for_tool("terminal", {"command": "cp -R source2/. target2"})
    )
    decision = decide(request)
    assert decision.verdict == "pending"
    assert "security_settings" in decision.reason
    # -H follows command-line operand links; the operand itself is the link
    # here, so its reachable target is inspected too.
    protected = workspace / "protected"
    (protected / ".git").mkdir(parents=True)
    (protected / ".git" / "config").write_text("attacker-controlled\n", encoding="utf-8")
    linkdir = workspace / "linkdir"
    linkdir.symlink_to(protected, target_is_directory=True)
    (workspace / "target3").mkdir()
    linked = build_request(
        tool="terminal", **action_for_tool("terminal", {"command": "cp -RH linkdir target3"})
    )
    assert decide(linked).verdict == "pending"


def test_recursive_copy_with_dereferenced_symlink_cycle_terminates(workspace):
    source = workspace / "looped"
    source.mkdir()
    (source / "notes.txt").write_text("hello\n", encoding="utf-8")
    (source / "self").symlink_to(source, target_is_directory=True)
    (workspace / "loop-target").mkdir()
    request = build_request(
        tool="terminal", **action_for_tool("terminal", {"command": "cp -RL looped/. loop-target"})
    )
    assert decide(request).verdict == "allowed"
