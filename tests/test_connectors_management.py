"""Durable inventory and human-only management; isolated filesystem, no DB."""

import os
import stat

import pytest

from machina.connectors.models import ConfigError
from machina.connectors.service import ConnectorService


def test_store_survives_restart_and_preserves_revisions(tmp_path):
    from machina.connectors.store import ConnectorStore

    path = tmp_path / "mcp.json"
    store = ConnectorStore(path)
    first = store.configure({"id": "fixture", "command": "fixture"})
    second = ConnectorStore(path).configure({"id": "fixture", "command": "updated"})
    generation, configs = store.load()
    assert generation == 2
    assert first.revision == 1 and second.revision == 2
    assert configs[0].command == "updated"
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert store.remove("fixture") is True
    assert store.remove("fixture") is False
    assert store.load() == (3, [])


@pytest.mark.parametrize(
    "fields",
    [{"env": {"TOKEN": "secret"}}, {"owner": "other"}, {"revision": 99}, {"unknown": True}],
)
def test_store_rejects_secret_values_and_authority_widening(tmp_path, fields):
    from machina.connectors.store import ConnectorStore

    store = ConnectorStore(tmp_path / "mcp.json")
    with pytest.raises(ConfigError):
        store.configure({"id": "fixture", "command": "fixture", **fields})
    assert not store.path.exists()


async def test_workers_reload_without_launching_and_drop_stale_catalog(tmp_path):
    from machina.connectors.models import DiscoveredTool
    from machina.connectors.store import ConnectorStore

    path = tmp_path / "mcp.json"
    a = ConnectorService(store=ConnectorStore(path))
    b = ConnectorService(store=ConnectorStore(path))
    await a.configure({"id": "fixture", "command": "fixture"})
    assert (await b.status("fixture"))["revision"] == 1
    b.registry.set_tools("fixture", [DiscoveredTool("fixture", "", "echo")])
    await a.configure({"id": "fixture", "command": "updated"})
    assert (await b.status("fixture"))["revision"] == 2
    assert b.registry.tools_for("fixture") == []
    await a.remove("fixture")
    assert await b.list() == []


async def test_management_methods_registered(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from machina.gateway.features import METHODS

    assert {
        "mcp.configure",
        "mcp.remove",
        "mcp.connect",
        "mcp.disconnect",
        "mcp.tools",
    } <= METHODS.keys()


def test_removed_connector_revision_cannot_be_reused(tmp_path):
    from machina.connectors.store import ConnectorStore

    store = ConnectorStore(tmp_path / "mcp.json")
    assert store.configure({"id": "fixture", "command": "fixture"}).revision == 1
    store.remove("fixture")
    assert store.configure({"id": "fixture", "command": "fixture"}).revision == 2


async def test_replacement_and_removal_retire_connection_first(tmp_path):
    from machina.connectors.manager import ConnectorManager
    from machina.connectors.models import ConnectionState
    from machina.connectors.registry import ConnectorRegistry
    from machina.connectors.store import ConnectorStore

    store = ConnectorStore(tmp_path / "mcp.json")
    registry = ConnectorRegistry()
    observed = []

    class Client:
        def __init__(self, config):
            self.config = config
            self.state = ConnectionState.DISCONNECTED
            self.server_name = self.server_version = self.protocol_version = self.last_error = ""

        async def connect(self, _secrets):
            self.state = ConnectionState.CONNECTED

        async def disconnect(self):
            observed.append(store.load()[1][0].command)
            self.state = ConnectionState.DISCONNECTED

    service = ConnectorService(registry, ConnectorManager(registry, Client), store=store)
    await service.configure({"id": "fixture", "command": "original"})
    await service.connect("fixture")
    await service.configure({"id": "fixture", "command": "replacement"})
    assert observed == ["original"]
    assert service.manager.connection("fixture") is None
    await service.connect("fixture")
    await service.remove("fixture")
    assert observed == ["original", "replacement"]
    assert service.manager.connection("fixture") is None


async def test_gateway_config_validation_does_not_persist(tmp_path):
    from types import SimpleNamespace

    from machina.connectors.store import ConnectorStore
    from machina.gateway.errors import INVALID_PARAMS, RpcError
    from machina.gateway.features.connectors import mcp_configure

    store = ConnectorStore(tmp_path / "mcp.json")
    service = ConnectorService(store=store)
    gw = SimpleNamespace(runtime=SimpleNamespace(get_connector_service=lambda: service))
    with pytest.raises(RpcError) as exc:
        await mcp_configure(
            gw, {"config": {"id": "fixture", "command": "fixture", "env": {"TOKEN": "secret"}}}
        )
    assert exc.value.code == INVALID_PARAMS
    assert not store.path.exists()


@pytest.mark.parametrize(
    "payload",
    ["null", "[]", '{"version":1,"generation":1,"configs":[],"revisions":{"fixture":true}}'],
)
def test_corrupt_inventory_fails_closed(tmp_path, payload):
    from machina.connectors.store import ConnectorStore

    store = ConnectorStore(tmp_path / "mcp.json")
    store.path.write_text(payload)
    with pytest.raises(ConfigError):
        store.load()


def test_parallel_workers_do_not_lose_inventory_updates(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from machina.connectors.store import ConnectorStore

    path = tmp_path / "mcp.json"

    def configure(index):
        return ConnectorStore(path).configure({"id": f"fixture-{index}", "command": "fixture"})

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(configure, range(12)))
    generation, configs = ConnectorStore(path).load()
    assert generation == 12
    assert {c.id for c in configs} == {f"fixture-{index}" for index in range(12)}


def test_windows_lock_covers_fixed_byte_and_releases_on_error(tmp_path, monkeypatch):
    import os
    from types import SimpleNamespace

    import machina.connectors.store as store_module

    operations = []

    def locking(fd, operation, count):
        operations.append((operation, count, os.lseek(fd, 0, os.SEEK_CUR), os.fstat(fd).st_size))

    monkeypatch.setattr(store_module, "_WINDOWS", True)
    monkeypatch.setattr(
        store_module,
        "msvcrt",
        SimpleNamespace(LK_LOCK=1, LK_UNLCK=2, locking=locking),
        raising=False,
    )
    store = store_module.ConnectorStore(tmp_path / "mcp.json")
    with pytest.raises(RuntimeError):
        with store._locked():
            raise RuntimeError("aborted update")
    assert operations == [(1, 1, 0, 1), (2, 1, 0, 1)]


def test_windows_persistence_skips_directory_fsync(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import machina.connectors.store as store_module

    monkeypatch.setattr(store_module, "_WINDOWS", True)
    monkeypatch.setattr(
        store_module,
        "msvcrt",
        SimpleNamespace(LK_LOCK=1, LK_UNLCK=2, locking=lambda *args: None),
        raising=False,
    )
    store = store_module.ConnectorStore(tmp_path / "mcp.json")

    def no_fchmod(*args):
        raise AssertionError("Unix-only fchmod attempted")

    monkeypatch.setattr(store_module.os, "fchmod", no_fchmod, raising=False)
    store.configure({"id": "fixture", "command": "fixture"})
    assert store.load()[1][0].id == "fixture"


async def test_restart_loads_inventory_without_constructing_client(tmp_path):
    from machina.connectors.manager import ConnectorManager
    from machina.connectors.registry import ConnectorRegistry
    from machina.connectors.store import ConnectorStore

    store = ConnectorStore(tmp_path / "mcp.json")
    store.configure({"id": "fixture", "command": "fixture"})

    def refuse_launch(config):
        raise AssertionError("inventory loading attempted to construct a transport")

    registry = ConnectorRegistry()
    restarted = ConnectorService(registry, ConnectorManager(registry, refuse_launch), store=store)
    assert (await restarted.list())[0]["state"] == "disconnected"
    assert (await restarted.status("fixture"))["revision"] == 1
    assert restarted.manager.connection("fixture") is None


@pytest.mark.parametrize("operation", ["configure", "remove"])
async def test_failed_retirement_refuses_mutation_and_keeps_client_tracked(tmp_path, operation):
    from machina.connectors.manager import ConnectorManager
    from machina.connectors.models import ConnectionFailed, ConnectionState
    from machina.connectors.registry import ConnectorRegistry
    from machina.connectors.store import ConnectorStore

    class RetiringClient:
        def __init__(self, config):
            self.config = config
            self.state = ConnectionState.CONNECTED
            self.server_name = self.server_version = self.protocol_version = self.last_error = ""

        async def connect(self, secrets):
            pass

        async def disconnect(self):
            self.state = ConnectionState.DEGRADED
            raise ConnectionFailed("shutdown timed out; transport cleanup remains tracked")

    store = ConnectorStore(tmp_path / "mcp.json")
    registry = ConnectorRegistry()
    service = ConnectorService(registry, ConnectorManager(registry, RetiringClient), store=store)
    await service.configure({"id": "fixture", "command": "original"})
    await service.connect("fixture")
    client = service.manager.connection("fixture")
    original_bytes = store.path.read_bytes()
    with pytest.raises(ConnectionFailed, match="shutdown timed out"):
        if operation == "configure":
            await service.configure({"id": "fixture", "command": "replacement"})
        else:
            await service.remove("fixture")
    assert store.path.read_bytes() == original_bytes
    assert service.registry.get("fixture").command == "original"
    assert service.manager.connection("fixture") is client
    assert client.state == ConnectionState.DEGRADED
