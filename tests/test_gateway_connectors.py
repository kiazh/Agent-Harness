"""Read-only MCP inspection uses one runtime inventory, without DB or SDK startup."""

from __future__ import annotations

import builtins
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from machina.connectors.manager import ConnectorManager
from machina.connectors.models import ConnectorConfig
from machina.connectors.registry import ConnectorRegistry
from machina.connectors.service import ConnectorService
from machina.core.runtime import RuntimeServices
from machina.gateway.errors import INVALID_PARAMS, METHOD_NOT_FOUND, NOT_FOUND, UNAUTHORIZED
from machina.gateway.server import Gateway


class WireHarness:
    """Encode requests and responses as the stdio transport does."""

    def __init__(self, runtime, *, owns_db=False):
        self.frames = []
        self.gateway = Gateway(
            lambda frame: self.frames.append(json.loads(json.dumps(frame))),
            owns_db=owns_db,
            runtime=runtime,
        )
        self.gateway._auth_token = None

    async def call(self, method, params=None):
        request_id = len(self.frames) + 1
        await self.gateway.handle_line(
            json.dumps(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
            )
        )
        return next(frame for frame in self.frames if frame.get("id") == request_id)


def inspection_runtime():
    registry = ConnectorRegistry()
    registry.add(
        ConnectorConfig(
            id="alpha",
            display_name="Alpha",
            command="never-launch",
            argv=["private-argument"],
            cwd="/private/cwd",
            secret_refs=["PRIVATE_TOKEN"],
        )
    )
    registry.add(ConnectorConfig(id="disabled", command="never-launch", enabled=False))

    def forbidden_launch(_config):
        pytest.fail("inspection launched an MCP connection")

    service = ConnectorService(manager=ConnectorManager(registry, forbidden_launch))
    return RuntimeServices(_connector_service=service), service


async def test_empty_runtime_inventory_is_lazy_and_does_not_require_sdk(monkeypatch, tmp_path):
    real_import = builtins.__import__

    def without_sdk(name, *args, **kwargs):
        if name == "mcp" or name.startswith("mcp."):
            pytest.fail("inspection imported the optional MCP SDK")
        return real_import(name, *args, **kwargs)

    # Hermetic inventory: never read the operator's real ~/.machina/mcp.json.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(builtins, "__import__", without_sdk)
    runtime = RuntimeServices()
    harness = WireHarness(runtime)
    assert runtime._connector_service is None
    assert not harness.gateway._db_ready
    assert (await harness.call("mcp.list"))["result"] == {"servers": []}
    shared_service = runtime.get_connector_service()
    shared_service.registry.add(ConnectorConfig(id="added", command="never-launch"))
    servers = (await harness.call("mcp.list"))["result"]["servers"]
    assert [(server["id"], server["state"]) for server in servers] == [("added", "disconnected")]
    assert runtime.get_connector_service() is shared_service


async def test_list_and_status_share_sanitized_inventory_without_launching():
    runtime, service = inspection_runtime()
    first, second = WireHarness(runtime), WireHarness(runtime)
    servers = (await first.call("mcp.list"))["result"]["servers"]
    assert [(server["id"], server["state"]) for server in servers] == [
        ("alpha", "disconnected"),
        ("disabled", "disabled"),
    ]
    server = (await second.call("mcp.status", {"id": "alpha"}))["result"]["server"]
    assert server == servers[0]
    assert server["display_name"] == "Alpha"
    assert not {"argv", "cwd", "secret_refs", "env_allowlist"}.intersection(server)
    assert "private-argument" not in json.dumps(servers)
    service.registry.update("alpha", enabled=False)
    assert (await first.call("mcp.status", {"id": "alpha"}))["result"]["server"][
        "state"
    ] == "disabled"
    assert not service.manager._connections


@pytest.mark.parametrize("params", [{}, {"id": " "}, {"id": 1}, {"id": "a" * 65}])
async def test_status_validates_id_through_rpc(params):
    runtime, _service = inspection_runtime()
    response = await WireHarness(runtime).call("mcp.status", params)
    assert response["error"]["code"] == INVALID_PARAMS


async def test_unknown_server_reports_not_found():
    runtime, _service = inspection_runtime()
    response = await WireHarness(runtime).call("mcp.status", {"id": "missing"})
    assert response["error"]["code"] == NOT_FOUND


async def test_management_requires_gateway_auth_and_exposes_no_raw_call():
    runtime, _service = inspection_runtime()
    harness = WireHarness(runtime)
    harness.gateway._auth_token = "fixture-token"
    assert (await harness.call("mcp.list"))["error"]["code"] == UNAUTHORIZED
    assert (await harness.call("mcp.list", {"token": "fixture-token"}))["result"]["servers"]
    for method in (
        "mcp.configure",
        "mcp.connect",
        "mcp.preview",
        "mcp.disconnect",
        "mcp.tools",
        "mcp.remove",
    ):
        assert (await harness.call(method))["error"]["code"] == UNAUTHORIZED
    assert (await harness.call("mcp.connect", {"token": "fixture-token"}))["error"][
        "code"
    ] == INVALID_PARAMS
    assert (await harness.call("mcp.call", {"token": "fixture-token"}))["error"][
        "code"
    ] == METHOD_NOT_FOUND


async def test_gateway_close_does_not_create_unused_connector_service():
    runtime = RuntimeServices()
    await WireHarness(runtime, owns_db=True).gateway.close()
    assert runtime._connector_service is None


def test_actual_stdio_gateway_reports_empty_harness_inventory_without_initializing(tmp_path):
    token = "connector-wire-fixture-token"
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "mcp.list", "params": {"token": token}},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "mcp.status",
            "params": {"id": "missing", "token": token},
        },
        {"jsonrpc": "2.0", "id": 3, "method": "shutdown", "params": {"token": token}},
    ]
    process = subprocess.run(
        [sys.executable, "-m", "tests.support.gateway_connectors_wire"],
        input="".join(json.dumps(request) + "\n" for request in requests),
        text=True,
        capture_output=True,
        timeout=15,
        cwd=Path(__file__).resolve().parents[1],
        env={
            **os.environ,
            "MCP_INSPECTION_TEST_STATE": str(tmp_path),
            "MACHINA_GATEWAY_TOKEN": token,
            "MACHINA_GATEWAY_NO_SCHEDULER": "1",
        },
    )
    assert process.returncode == 0, process.stderr
    frames = [json.loads(line) for line in process.stdout.splitlines()]
    assert frames[0] == {"jsonrpc": "2.0", "id": 1, "result": {"servers": []}}
    assert frames[1]["id"] == 2 and frames[1]["error"]["code"] == NOT_FOUND
    assert frames[2] == {"jsonrpc": "2.0", "id": 3, "result": {}}
