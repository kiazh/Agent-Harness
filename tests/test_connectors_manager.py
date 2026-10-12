"""Lifecycle policy with injected clients; the optional SDK is not used."""

import asyncio
from collections import deque
from types import SimpleNamespace

import pytest

from machina.connectors.manager import ConnectorManager
from machina.connectors.models import (
    AuthRequired,
    ConfigError,
    ConnectionFailed,
    ConnectionState,
    ConnectorConfig,
)
from machina.connectors.registry import ConnectorRegistry


class FakeClient:
    def __init__(self, config, outcomes):
        self.config = config
        self.outcomes = outcomes
        self.state = ConnectionState.DISCONNECTED
        self.server_name = "fixture"
        self.server_version = "1"
        self.protocol_version = "negotiated"
        self.last_error = ""
        self.attempts = 0
        self.closed = 0
        self.connect_entered = None
        self.connect_release = None
        self.close_entered = None
        self.close_release = None

    async def connect(self, _secrets):
        self.attempts += 1
        if self.connect_entered is not None:
            self.connect_entered.set()
            await self.connect_release.wait()
        outcome = self.outcomes.popleft() if self.outcomes else None
        if outcome is not None:
            self.state = ConnectionState.ERROR
            raise outcome
        self.state = ConnectionState.CONNECTED

    async def disconnect(self):
        self.closed += 1
        if self.close_entered is not None:
            self.close_entered.set()
            await self.close_release.wait()
        self.state = ConnectionState.DISCONNECTED


def setup_manager(outcomes=(), **config_fields):
    registry = ConnectorRegistry()
    registry.add(
        ConnectorConfig(
            id="alpha", command="fixture", max_retries=2, backoff_base=0.1, **config_fields
        )
    )
    clients = []
    pending = deque(outcomes)

    def factory(config):
        client = FakeClient(config, pending)
        clients.append(client)
        return client

    return ConnectorManager(registry, factory), clients


def transient(message="temporary connection failure"):
    error = ConnectionFailed(message)
    error.retryable = True
    return error


async def test_transient_retries_are_bounded_with_backoff(monkeypatch):
    from machina.connectors import manager as module

    manager, clients = setup_manager([transient(), transient(), None])
    delays = []

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    monkeypatch.setattr(module.random, "uniform", lambda *_args: 0.05)
    result = await manager.connect("alpha")
    assert result["state"] == "connected"
    assert len(clients) == 1 and clients[0].attempts == 3
    assert delays == pytest.approx([0.15, 0.25])


@pytest.mark.parametrize(
    "error",
    [
        ConfigError("invalid config"),
        AuthRequired("missing auth"),
        ConnectionFailed("permanent refusal"),
    ],
)
async def test_permanent_failures_never_retry(error):
    manager, clients = setup_manager([error] * 4)
    with pytest.raises(ConnectionFailed):
        await manager.connect("alpha")
    assert clients[0].attempts == 1


async def test_permanent_failures_do_not_open_transient_circuit():
    manager, clients = setup_manager([AuthRequired("missing auth")] * 3 + [None])
    for _ in range(3):
        with pytest.raises(ConnectionFailed):
            await manager.connect("alpha")
    assert (await manager.connect("alpha"))["state"] == "connected"
    assert sum(client.attempts for client in clients) == 4


async def test_failed_connection_status_is_visible():
    manager, _clients = setup_manager([ConfigError("invalid config")])
    with pytest.raises(ConnectionFailed):
        await manager.connect("alpha")
    assert manager.status("alpha")["state"] == "error"


async def test_transient_circuit_opens_then_recovers(monkeypatch):
    from machina.connectors import manager as module

    clock = [100.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    manager, clients = setup_manager([transient()] * 3 + [None])
    manager.registry.update("alpha", max_retries=0)
    for _ in range(3):
        with pytest.raises(ConnectionFailed):
            await manager.connect("alpha")
    with pytest.raises(ConnectionFailed, match="circuit open"):
        await manager.connect("alpha")
    assert sum(client.attempts for client in clients) == 3
    clock[0] += 61.0
    assert (await manager.connect("alpha"))["state"] == "connected"


async def test_disabled_inventory_and_status_never_launch():
    manager, clients = setup_manager(enabled=False)
    assert manager.status("alpha")["state"] == "disabled"
    with pytest.raises(ConnectionFailed, match="disabled"):
        await manager.connect("alpha")
    assert clients == []


async def test_connect_is_idempotent_and_shutdown_all_retires_every_client():
    manager, clients = setup_manager()
    manager.registry.add(ConnectorConfig(id="beta", command="fixture"))
    await manager.connect("alpha")
    await manager.connect("alpha")
    await manager.connect("beta")
    assert len(clients) == 2
    await manager.shutdown_all()
    assert all(client.closed == 1 for client in clients)
    assert manager.status("alpha")["state"] == "disconnected"
    assert manager.status("beta")["state"] == "disconnected"


async def test_reconnect_waits_until_previous_transport_has_retired():
    manager, clients = setup_manager()
    await manager.connect("alpha")
    first = clients[0]
    first.close_entered, first.close_release = asyncio.Event(), asyncio.Event()
    closing = asyncio.create_task(manager.disconnect("alpha"))
    await first.close_entered.wait()
    reconnecting = asyncio.create_task(manager.connect("alpha"))
    try:
        await asyncio.sleep(0)
        assert not reconnecting.done(), "new server launched before previous shutdown completed"
        assert len(clients) == 1
    finally:
        first.close_release.set()
        await closing
        await reconnecting


async def test_unknown_connector_never_launches():
    manager, clients = setup_manager()
    with pytest.raises(ConnectionFailed, match="unknown connector"):
        await manager.connect("missing")
    with pytest.raises(ConnectionFailed, match="unknown connector"):
        manager.status("missing")
    assert clients == []
