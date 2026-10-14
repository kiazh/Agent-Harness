"""Agent-callable MCP management tools (mcp_connect / mcp_list / mcp_disconnect).

Unit level: input validation, reused URL/command policy, approval
classification, store compensation on failed connect, removal semantics,
and list redaction. Live level: Machina's own MCP server over stdio through
the real tool path (broker guard -> registry.execute -> ConnectorService),
using the minimal explicit approval handler because a headless run has no
human to click the card.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import sys
import uuid
from pathlib import Path

import pytest

from machina.connectors.management import (
    add_and_connect,
    connect_spec,
    disconnect_spec,
    remove_server,
)
from machina.connectors.models import (
    ConfigError,
    ConnectionFailed,
    ConnectionState,
    ConnectorToolError,
)
from machina.connectors.registry import ConnectorRegistry
from machina.connectors.service import ConnectorService
from machina.connectors.store import ConnectorStore
from machina.permissions.broker import _execution_context
from machina.permissions.policy import build_request, decide
from machina.permissions.tools import action_for_tool

SESSION = "session"
AGENT = "harness"


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    from machina.core.config import config

    monkeypatch.setattr(config, "workspace_root", str(root))
    monkeypatch.setattr(config, "execution_mode", "ask")
    return root


@pytest.fixture
def approvals():
    """Activate broker-style execution snapshots for exact requests.

    ContextVars live per asyncio task, so teardown clears the fixture-side
    context instead of resetting tokens created inside test tasks.
    """
    from machina.permissions.broker import clear_execution_context

    def activate(spec: dict, tool: str, *, mode: str = "ask", session: str = SESSION):
        request = build_request(
            **spec,
            tool=tool,
            mode=mode,
            backend="host",
            agent_id=AGENT,
            session_id=session,
        )
        _execution_context.set(
            {
                "session_id": session,
                "mode": mode,
                "backend": "host",
                "digest": request.digest,
            }
        )

    yield activate
    clear_execution_context()


def _store(tmp_path: Path) -> ConnectorStore:
    return ConnectorStore(tmp_path / "mcp.json")


class FailingClient:
    def __init__(self, config):
        self.config = config
        self.state = ConnectionState.DISCONNECTED
        self.server_name = self.server_version = self.protocol_version = self.last_error = ""

    async def connect(self, _secrets):
        self.state = ConnectionState.ERROR
        self.last_error = "synthetic connect failure"
        raise ConnectionFailed("synthetic connect failure")

    async def disconnect(self):
        self.state = ConnectionState.DISCONNECTED


# ---------------------------------------------------------------------------
# Tool registration and approval classification
# ---------------------------------------------------------------------------


def test_management_tools_registered_with_review_effects():
    import machina.tools.mcp  # noqa: F401 — registers the MCP tool surface
    from machina.tools.base import _cache_policy, registry

    connect = registry.get_tool("mcp_connect")
    disconnect = registry.get_tool("mcp_disconnect")
    assert connect is not None and disconnect is not None
    assert connect.effects == ("net", "exec")
    assert disconnect.effects == ("fs.write",)
    # Declared mutating effects also make the tools uncacheable and
    # cache-flushing even outside the explicit permission branch.
    assert _cache_policy(connect) == (False, True)
    assert _cache_policy(disconnect) == (False, True)
    assert "approval" in connect.description.lower()
    assert "approval" in disconnect.description.lower()


@pytest.mark.parametrize("mode", ["ask", "workspace", "sandbox"])
@pytest.mark.parametrize(
    "tool,args",
    [
        ("mcp_connect", {"name": "docs", "url": "https://example.com/mcp"}),
        ("mcp_connect", {"name": "local", "command": sys.executable}),
        ("mcp_disconnect", {"name": "docs"}),
        ("mcp_disconnect", {"name": "docs", "remove": False}),
    ],
)
def test_management_actions_require_approval(workspace, mode, tool, args):
    spec = action_for_tool(tool, args)
    request = build_request(tool=tool, **spec, mode=mode)
    decision = decide(request)
    assert decision.verdict == "pending", decision.reason
    assert "mcp_external_review" in request.capabilities


def test_full_mode_session_grant_allows_management(workspace):
    spec = action_for_tool("mcp_connect", {"name": "docs", "url": "https://example.com/mcp"})
    request = build_request(tool="mcp_connect", **spec, mode="full")
    grants = [{"mode": "full", "capability": "session", "digest": "full-mode-session"}]
    assert decide(request, grants).verdict == "allowed"


def test_mcp_list_stays_routine(workspace):
    request = build_request(tool="mcp_list", **action_for_tool("mcp_list", {}))
    assert decide(request).verdict == "allowed"


def test_invalid_requests_fail_before_approval():
    with pytest.raises(ConfigError):
        action_for_tool("mcp_connect", {"name": "docs"})
    with pytest.raises(ConfigError):
        action_for_tool("mcp_connect", {})
    with pytest.raises(ConfigError):
        action_for_tool("mcp_disconnect", {"name": "NOT VALID"})


# ---------------------------------------------------------------------------
# Input validation reusing the connector's own policy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        {"name": "docs"},
        {"name": "docs", "url": "https://example.com/mcp", "command": "node"},
        {"name": "Bad Name", "url": "https://example.com/mcp"},
    ],
)
def test_transport_and_name_must_be_valid(args):
    with pytest.raises(ConfigError):
        connect_spec(args)


def test_whitespace_command_is_not_a_transport():
    # Same trim semantics as the human /mcp add flow: blank optional fields
    # are absent, so a URL wins and no stdio launch is defined.
    spec = connect_spec({"name": "docs", "url": "https://example.com/mcp", "command": "  "})
    assert json.loads(spec["content"])["transport"] == "streamable_http"


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/mcp",
        "not a url",
        "https:///mcp",
        "https://user:secret@example.com/mcp",
        "https://example.com/mcp?token=abc",
        "https://example.com/mcp#fragment",
    ],
)
def test_url_policy_is_the_connector_validation(url):
    with pytest.raises(ConfigError):
        connect_spec({"name": "docs", "url": url})


@pytest.mark.parametrize(
    "command",
    ["bash -c 'rm -rf /'", "node server.js", "definitely-not-installed-xyz"],
)
def test_stdio_command_policy_is_the_connector_validation(command):
    with pytest.raises(ConfigError):
        connect_spec({"name": "local", "command": command})


def test_env_allowlist_accepts_names_never_values():
    with pytest.raises(ConfigError, match="invalid environment/secret name"):
        connect_spec(
            {"name": "local", "command": sys.executable, "env_allowlist": ["API_TOKEN=secret"]}
        )
    spec = connect_spec(
        {"name": "local", "command": sys.executable, "env_allowlist": ["API_TOKEN"]}
    )
    content = json.loads(spec["content"])
    assert content["env_allowlist"] == ["API_TOKEN"]
    # The approval card/digest binds the allowlist names (and trust level).
    assert content["trust"] == "untrusted"
    assert "API_TOKEN" in spec["content"]


def test_connect_spec_binds_destination_and_timeouts():
    http = connect_spec({"name": "docs", "url": "https://example.com/mcp"})
    assert http["operation"] == "tool.mcp_connect"
    assert http["network"] == ["https://example.com/mcp"]
    assert http["argv"] == []
    stdio = connect_spec(
        {"name": "local", "command": sys.executable, "argv": ["-m", "pkg"], "connect_timeout": 20}
    )
    assert stdio["argv"][0] == str(Path(shutil.which(sys.executable)).absolute())
    assert stdio["network"] == []
    assert stdio["timeout"] == 20 + 20  # connect + discovery defaults
    with pytest.raises(ConfigError):
        connect_spec({"name": "local", "command": sys.executable, "connect_timeout": 0})


# ---------------------------------------------------------------------------
# Approval snapshot is required and bound to the exact request
# ---------------------------------------------------------------------------


async def test_connect_without_approved_snapshot_never_writes(tmp_path):
    store = _store(tmp_path)
    service = ConnectorService(store=store)
    args = {"name": "local", "command": sys.executable}
    with pytest.raises(ConnectorToolError, match="approved active session"):
        await add_and_connect(service, args, agent_id=AGENT, session_id=SESSION)
    assert not store.path.exists()


async def test_changed_request_refused_after_approval(tmp_path, approvals):
    store = _store(tmp_path)
    service = ConnectorService(store=store)
    approved = connect_spec({"name": "docs", "url": "https://example.com/mcp"})
    approvals(approved, "mcp_connect")
    changed = {"name": "docs", "url": "https://evil.example.net/mcp"}
    with pytest.raises(ConnectorToolError, match="changed after approval"):
        await add_and_connect(service, changed, agent_id=AGENT, session_id=SESSION)
    assert not store.path.exists()


# ---------------------------------------------------------------------------
# Composite add+connect with store compensation
# ---------------------------------------------------------------------------


async def test_failed_connect_removes_new_config(tmp_path, approvals):
    from machina.connectors.manager import ConnectorManager

    store = _store(tmp_path)
    registry = ConnectorRegistry()
    service = ConnectorService(registry, ConnectorManager(registry, FailingClient), store=store)
    args = {"name": "newdocs", "command": sys.executable}
    approvals(connect_spec(args), "mcp_connect")
    report = await add_and_connect(service, args, agent_id=AGENT, session_id=SESSION)
    assert report["connected"] is False
    assert "synthetic connect failure" in report["error"]
    assert report["store"] == "configuration removed"
    generation, configs = store.load()
    assert configs == [] and generation == 2  # configure + compensating remove
    assert service.manager.connection("newdocs") is None
    assert service.registry.get("newdocs") is None


async def test_failed_connect_restores_replaced_config(tmp_path, approvals):
    from machina.connectors.manager import ConnectorManager

    store = _store(tmp_path)
    registry = ConnectorRegistry()
    service = ConnectorService(registry, ConnectorManager(registry, FailingClient), store=store)
    await service.configure({"id": "docs", "command": sys.executable, "argv": ["--original"]})
    args = {"name": "docs", "command": sys.executable, "argv": ["--replacement"]}
    approvals(connect_spec(args), "mcp_connect")
    report = await add_and_connect(service, args, agent_id=AGENT, session_id=SESSION)
    assert report["connected"] is False
    assert report["store"] == "previous configuration restored"
    _, configs = store.load()
    assert configs[0].argv == ["--original"]
    assert service.registry.get("docs").argv == ["--original"]
    assert service.manager.connection("docs") is None


async def test_failed_discovery_compensates_and_retires_connection(tmp_path, approvals):
    from machina.connectors.manager import ConnectorManager

    class DiscoveryFailingClient(FailingClient):
        async def connect(self, _secrets):
            self.state = ConnectionState.CONNECTED

        async def list_tools(self):
            raise ConnectionFailed("tool discovery timed out")

    store = _store(tmp_path)
    registry = ConnectorRegistry()
    service = ConnectorService(
        registry, ConnectorManager(registry, DiscoveryFailingClient), store=store
    )
    args = {"name": "flaky", "command": sys.executable}
    approvals(connect_spec(args), "mcp_connect")
    report = await add_and_connect(service, args, agent_id=AGENT, session_id=SESSION)
    assert report["connected"] is False
    assert "tool discovery timed out" in report["error"]
    assert report["store"] == "configuration removed"
    assert store.load()[1] == []
    assert service.manager.connection("flaky") is None


async def test_successful_add_reports_view_and_discovered_tools(tmp_path, approvals):
    from machina.connectors.manager import ConnectorManager
    from machina.connectors.models import DiscoveredTool

    class DiscoveringClient(FailingClient):
        async def connect(self, _secrets):
            self.state = ConnectionState.CONNECTED

        async def list_tools(self):
            return [DiscoveredTool("docs", "", "search", schema={"type": "object"})], False

    store = _store(tmp_path)
    registry = ConnectorRegistry()
    service = ConnectorService(registry, ConnectorManager(registry, DiscoveringClient), store=store)
    args = {"name": "docs", "command": sys.executable}
    approvals(connect_spec(args), "mcp_connect")
    report = await add_and_connect(service, args, agent_id=AGENT, session_id=SESSION)
    assert report["connected"] is True
    assert report["tools"] == ["docs__search"]
    assert report["server"]["state"] == "connected"
    assert report["server"]["enabled"] is True
    assert store.load()[1][0].id == "docs"


# ---------------------------------------------------------------------------
# Removal semantics (same retirement path as the human /mcp flow)
# ---------------------------------------------------------------------------


async def test_disconnect_removes_config_and_reports_unknown(tmp_path, approvals):
    store = _store(tmp_path)
    service = ConnectorService(store=store)
    await service.configure({"id": "docs", "command": sys.executable})
    args = {"name": "docs"}
    approvals(disconnect_spec(args), "mcp_disconnect")
    report = await remove_server(service, args, agent_id=AGENT, session_id=SESSION)
    assert report == {"removed": True, "server": "docs"}
    assert store.load()[1] == []
    assert service.registry.get("docs") is None

    ghost = {"name": "ghost"}
    approvals(disconnect_spec(ghost), "mcp_disconnect")
    report = await remove_server(service, ghost, agent_id=AGENT, session_id=SESSION)
    assert report["removed"] is False
    assert "unknown MCP server" in report["error"]


async def test_disconnect_without_remove_keeps_configuration(tmp_path, approvals):
    store = _store(tmp_path)
    service = ConnectorService(store=store)
    await service.configure({"id": "docs", "command": sys.executable})
    args = {"name": "docs", "remove": False}
    approvals(disconnect_spec(args), "mcp_disconnect")
    report = await remove_server(service, args, agent_id=AGENT, session_id=SESSION)
    assert report["removed"] is False and report["disconnected"] is False
    assert store.load()[1][0].id == "docs"


# ---------------------------------------------------------------------------
# Listing: management fields, tool counts, no secret material
# ---------------------------------------------------------------------------


async def _agent_scope(agent_id):
    return agent_id, uuid.UUID("123e4567-e89b-42d3-a456-426614174000")


async def test_agent_mcp_list_shows_management_fields(tmp_path, monkeypatch):
    import machina.tools.mcp as mcp_tools
    from machina.connectors.models import DiscoveredTool

    store = _store(tmp_path)
    service = ConnectorService(store=store)
    await service.configure(
        {
            "id": "docs",
            "command": sys.executable,
            "trust": "trusted_read",
            "tool_allowlist": ["search"],
            "env_allowlist": ["API_TOKEN"],
        }
    )
    service.registry.set_tools(
        "docs", [DiscoveredTool("docs", "", name, schema={}) for name in ("search", "extra")]
    )
    monkeypatch.setattr(mcp_tools, "_service", lambda: service)
    monkeypatch.setattr(mcp_tools, "active_agent_scope", lambda: _agent_scope(AGENT))
    payload = json.loads(await mcp_tools.mcp_list())
    server = payload["servers"][0]
    assert server["id"] == "docs" and server["enabled"] is True
    assert server["trust"] == "trusted_read"
    assert server["tool_allowlist"] == ["search"]
    assert server["tool_count"] == 2
    rendered = json.dumps(payload)
    # Secret-bearing allowlists and endpoints stay out of agent listings.
    assert "API_TOKEN" not in rendered
    assert "endpoint" not in rendered and "oauth" not in rendered


async def test_agent_mcp_list_hides_other_agents_servers(monkeypatch):
    import machina.tools.mcp as mcp_tools
    from machina.connectors.models import ConnectorConfig

    service = ConnectorService()
    service.registry.add(ConnectorConfig(id="mine", command=sys.executable))
    service.registry.add(ConnectorConfig(id="theirs", command=sys.executable, owner="other-agent"))
    monkeypatch.setattr(mcp_tools, "_service", lambda: service)
    monkeypatch.setattr(mcp_tools, "active_agent_scope", lambda: _agent_scope(AGENT))
    payload = json.loads(await mcp_tools.mcp_list())
    assert [server["id"] for server in payload["servers"]] == ["mine"]


# ---------------------------------------------------------------------------
# Live: Machina's own MCP server through the real tool path
# ---------------------------------------------------------------------------


async def test_live_self_connect_call_disconnect(monkeypatch, tmp_path):
    """mcp_connect -> tool discovery -> mcp_call machina_status -> disconnect.

    Approval gate: headless run uses the minimal explicit path — an approval
    handler that records the decision through the permissions store (the same
    transition the TUI performs) because no human is present to click.
    Session ownership is stubbed at the DB seam only; everything else
    (broker guard, registry.execute, ConnectorService, real SDK transport,
    real `python -m machina.mcp_server` child) is production code.
    """
    pytest.importorskip("mcp")
    import machina.tools.agents as agents_module
    from machina.core.runtime import runtime_services
    from machina.permissions import store
    from machina.permissions.broker import (
        clear_execution_context,
        permission_broker,
        set_approval_handler,
    )
    from machina.tools.base import registry

    store_file = _store(tmp_path)
    service = ConnectorService(store=store_file)
    monkeypatch.setattr(runtime_services, "_connector_service", service)

    async def fake_fetchval(_query, *_args):
        return AGENT

    monkeypatch.setattr(agents_module.db, "fetchval", fake_fetchval)
    agent_token = agents_module.current_agent_id.set(AGENT)
    session_id = uuid.uuid4()
    session_token = agents_module.current_session_id.set(session_id)

    async def approve(card):
        await store.resolve_decision(card["request_id"], "approved", "live-test")
        return "approved"

    set_approval_handler(approve, principal="live-test")

    async def invoke(tool_name, args):
        """The agent loop's guard+execute seam, verbatim in three lines."""
        spec = action_for_tool(tool_name, args)
        request = build_request(
            **spec,
            tool=tool_name,
            mode="ask",
            backend="host",
            agent_id=AGENT,
            session_id=str(session_id),
        )
        await permission_broker.guard(request)
        return await registry.execute(tool_name, **args)

    try:
        async with asyncio.timeout(120):
            report = json.loads(
                await invoke(
                    "mcp_connect",
                    {
                        "name": "machina-self",
                        "command": sys.executable,
                        "argv": ["-m", "machina.mcp_server"],
                        "connect_timeout": 30,
                        "discovery_timeout": 30,
                        "call_timeout": 30,
                    },
                )
            )
            assert report["connected"] is True, report
            assert report["server"]["state"] == "connected"
            assert sorted(report["tools"]) == [
                "machina-self__machina_chat",
                "machina-self__machina_session_create",
                "machina-self__machina_status",
            ]

            listing = json.loads(await invoke("mcp_list", {}))
            entry = next(s for s in listing["servers"] if s["id"] == "machina-self")
            assert entry["state"] == "connected" and entry["tool_count"] == 3

            outcome = json.loads(
                await invoke(
                    "mcp_call",
                    {"name": "machina-self__machina_status", "arguments": {}},
                )
            )
            assert outcome["ok"] is True, outcome
            assert "Machina status" in outcome["text"]

            report = json.loads(await invoke("mcp_disconnect", {"name": "machina-self"}))
            assert report["removed"] is True
            assert store_file.load()[1] == []
            assert service.manager.connection("machina-self") is None
            remaining = json.loads(await invoke("mcp_list", {}))["servers"]
            assert not any(server["id"] == "machina-self" for server in remaining)
    except TimeoutError:
        pytest.skip("live MCP round trip exceeded 120s in this environment")
    finally:
        set_approval_handler(None)
        clear_execution_context()
        agents_module.current_session_id.reset(session_token)
        agents_module.current_agent_id.reset(agent_token)
        await service.manager.shutdown_all()
        # Never leave a half-configured self server behind after a failure.
        with contextlib.suppress(Exception):
            await service.remove("machina-self")
