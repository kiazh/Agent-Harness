"""Gateway inspection reflects real SDK connections to a disposable local fixture."""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

from ah.connectors.models import ConnectorConfig
from ah.connectors.service import ConnectorService
from ah.core.runtime import RuntimeServices
from tests.test_gateway_connectors import WireHarness

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "support" / "mcp_fixture_server.py"


def fixture_runtime(tmp_path):
    pytest.importorskip("mcp")
    service = ConnectorService()
    name = f"inspect-{uuid.uuid4().hex}"
    pidfile = tmp_path / "fixture.pid"
    service.registry.add(
        ConnectorConfig(
            id=name,
            command=sys.executable,
            argv=[str(FIXTURE), "--transport", "stdio", "--name", name, "--pidfile", str(pidfile)],
            cwd=str(ROOT),
            max_retries=0,
            shutdown_timeout=2,
        )
    )
    return RuntimeServices(_connector_service=service), service, name, pidfile


def assert_fixture_exited(pidfile):
    assert pidfile.exists(), "the real fixture did not start"
    pid = int(pidfile.read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_wire_inventory_tracks_real_connected_then_disconnected_server(tmp_path):
    runtime, service, name, pidfile = fixture_runtime(tmp_path)
    harness = WireHarness(runtime)
    try:
        assert (await harness.call("mcp.status", {"id": name}))["result"]["server"][
            "state"
        ] == "disconnected"
        await service.connect(name)
        server = (await harness.call("mcp.list"))["result"]["servers"][0]
        assert server["state"] == "connected"
        assert server["server_name"] == f"fixture-{name}"
        assert server["server_version"] == ""  # This SDK fixture advertises no version.
        assert server["protocol_version"]
        assert (await harness.call("mcp.status", {"id": name}))["result"]["server"] == server
        await service.disconnect(name)
        assert (await harness.call("mcp.list"))["result"]["servers"][0]["state"] == "disconnected"
        assert_fixture_exited(pidfile)
    finally:
        await service.manager.shutdown_all()


@pytest.mark.parametrize("owner", ["gateway", "runtime"])
async def test_owned_connections_are_closed_by_lifecycle(tmp_path, owner):
    runtime, service, name, pidfile = fixture_runtime(tmp_path)
    harness = WireHarness(runtime, owns_db=owner == "gateway")
    try:
        await service.connect(name)
        await harness.gateway.close()
        if owner == "runtime":
            # HTTP adapters borrow the runtime: closing one adapter must not
            # disconnect other consumers before the runtime owner shuts down.
            assert (await service.status(name))["state"] == "connected"
            await runtime.shutdown()
        assert (await service.status(name))["state"] == "disconnected"
        assert_fixture_exited(pidfile)
    finally:
        await service.manager.shutdown_all()
