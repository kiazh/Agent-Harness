"""Evidence tests for the final-review connector fixes (F03-F06 + screener F1).

Deterministic probes for each confirmed finding:

* F1   — the pattern screener charges repeated-unit size, not only nested counts.
* F03  — failed-add compensation is a revision CAS; a newer concurrent config wins.
* F04  — agent ``mcp_connect`` refuses host stdio under sandbox before any write.
* F05  — connector-store lock acquisition is nonblocking/bounded and off-loop.
* F06  — no code path refreshes credential cache while holding the config lock.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time

import pytest

from machina.connectors.management import add_and_connect, connect_spec
from machina.connectors.models import (
    ConnectionFailed,
    ConnectionState,
    ConnectorToolError,
)
from machina.connectors.policy import validate_arguments
from machina.connectors.registry import ConnectorRegistry
from machina.connectors.service import ConnectorService
from machina.connectors.store import ConnectorStore, ConnectorStoreBusy
from machina.permissions.broker import _execution_context, clear_execution_context
from machina.permissions.policy import build_request

SESSION = "session"
AGENT = "harness"

_POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX flock semantics are required")


def _store(tmp_path) -> ConnectorStore:
    return ConnectorStore(tmp_path / "mcp.json")


@pytest.fixture
def approvals():
    """Activate broker-style execution snapshots for exact requests."""

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


class _Client:
    def __init__(self, config):
        self.config = config
        self.state = ConnectionState.DISCONNECTED
        self.server_name = self.server_version = self.protocol_version = self.last_error = ""

    async def disconnect(self):
        self.state = ConnectionState.DISCONNECTED


# ---------------------------------------------------------------------------
# F1 — screener charges body size x count
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "(" + "ab?cd?" * 25 + ")",
        "x" * 180,  # plain long body
        "|".join(f"{c}?" for c in "abcdefghijklmnopqrstuvwxyz"),  # alternation-heavy
    ],
)
def test_grouped_long_body_under_a_count_is_refused(body):
    pattern = f"({body}){{10000}}"
    assert len(pattern) <= 200
    schema = {"type": "object", "properties": {"v": {"type": "string", "pattern": pattern}}}
    started = time.monotonic()
    # Refused by the admit screen (not merely by a match failure): the value
    # below would not match anyway, so the budget message is the discriminator.
    with pytest.raises(ConnectorToolError, match="compilation cost"):
        validate_arguments("t", {"v": "x"}, schema)
    assert time.monotonic() - started < 1.0


def test_long_body_small_count_and_atom_counts_stay_accepted():
    for pattern, value in (
        (r"^(?:[0-9]{1,3}\.){3}[0-9]{1,3}$", "192.168.0.1"),  # IPv4-style
        (r"^[A-Za-z0-9._:-]{1,128}$", "host:8080"),  # single class, high count
        (r"^(ab){3}$", "ababab"),  # small grouped body
        (r"^(a{100}){10}$", "a" * 1000),  # nested product at the existing budget
    ):
        schema = {"type": "object", "properties": {"v": {"type": "string", "pattern": pattern}}}
        validate_arguments("t", {"v": value}, schema)
        with pytest.raises(ConnectorToolError):
            validate_arguments("t", {"v": "!"}, schema)


# ---------------------------------------------------------------------------
# F03 — revision CAS on failed-add compensation
# ---------------------------------------------------------------------------


async def test_failed_replace_keeps_newer_concurrent_config(tmp_path, approvals):
    from machina.connectors.manager import ConnectorManager

    store = _store(tmp_path)

    class RacingClient(_Client):
        async def connect(self, _secrets):
            # Another process persists a newer revision during our await.
            ConnectorStore(store.path).configure(
                {"id": "docs", "command": sys.executable, "argv": ["--newer"]}
            )
            raise ConnectionFailed("synthetic failure after concurrent update")

    registry = ConnectorRegistry()
    service = ConnectorService(registry, ConnectorManager(registry, RacingClient), store=store)
    await service.configure({"id": "docs", "command": sys.executable, "argv": ["--original"]})

    args = {"name": "docs", "command": sys.executable, "argv": ["--replacement"]}
    approvals(connect_spec(args), "mcp_connect")
    report = await add_and_connect(service, args, agent_id=AGENT, session_id=SESSION)

    assert report["connected"] is False
    assert report["store"].startswith("compensation skipped")
    assert store.load()[1][0].argv == ["--newer"]
    assert service.registry.get("docs").argv == ["--newer"]


async def test_failed_new_add_keeps_newer_concurrent_config(tmp_path, approvals):
    from machina.connectors.manager import ConnectorManager

    store = _store(tmp_path)

    class RacingClient(_Client):
        async def connect(self, _secrets):
            ConnectorStore(store.path).configure(
                {"id": "fresh", "command": sys.executable, "argv": ["--newer"]}
            )
            raise ConnectionFailed("synthetic failure after concurrent update")

    registry = ConnectorRegistry()
    service = ConnectorService(registry, ConnectorManager(registry, RacingClient), store=store)

    args = {"name": "fresh", "command": sys.executable, "argv": ["--replacement"]}
    approvals(connect_spec(args), "mcp_connect")
    report = await add_and_connect(service, args, agent_id=AGENT, session_id=SESSION)

    assert report["connected"] is False
    assert report["store"].startswith("compensation skipped")
    assert [config.argv for config in store.load()[1]] == [["--newer"]]


async def test_failed_add_without_interference_still_compensates(tmp_path, approvals):
    from machina.connectors.manager import ConnectorManager

    store = _store(tmp_path)

    class FailingClient(_Client):
        async def connect(self, _secrets):
            raise ConnectionFailed("synthetic connect failure")

    registry = ConnectorRegistry()
    service = ConnectorService(registry, ConnectorManager(registry, FailingClient), store=store)
    args = {"name": "solo", "command": sys.executable}
    approvals(connect_spec(args), "mcp_connect")
    report = await add_and_connect(service, args, agent_id=AGENT, session_id=SESSION)
    assert report["store"] == "configuration removed"
    assert store.load()[1] == []


# ---------------------------------------------------------------------------
# F04 — sandbox stdio refusal at the common launch boundary
# ---------------------------------------------------------------------------


async def test_agent_mcp_connect_refuses_stdio_in_sandbox(tmp_path, approvals):
    store = _store(tmp_path)
    service = ConnectorService(store=store)
    args = {"name": "local", "command": sys.executable}
    approvals(connect_spec(args), "mcp_connect", mode="sandbox")
    with pytest.raises(ConnectorToolError, match="sandbox"):
        await add_and_connect(service, args, agent_id=AGENT, session_id=SESSION)
    # Typed refusal happens before any store write.
    assert not store.path.exists()


async def test_connect_and_discover_refuses_stdio_in_sandbox(tmp_path):
    store = _store(tmp_path)
    service = ConnectorService(store=store)
    await service.configure({"id": "local", "command": sys.executable})
    with pytest.raises(ConnectorToolError, match="sandbox"):
        await service.connect_and_discover("local", mode="sandbox")
    assert service.manager.connection("local") is None


async def test_connect_and_discover_allows_http_in_sandbox(tmp_path):
    """The sandbox rule is stdio-only; HTTP is not refused by it."""

    class _RefusingClient(_Client):
        async def connect(self, _secrets):
            raise ConnectionFailed("http transport reached")

        async def list_tools(self):
            raise AssertionError("unreachable")

    from machina.connectors.manager import ConnectorManager

    store = _store(tmp_path)
    registry = ConnectorRegistry()
    service = ConnectorService(registry, ConnectorManager(registry, _RefusingClient), store=store)
    await service.configure(
        {"id": "web", "transport": "streamable_http", "endpoint": "https://example.com/mcp"}
    )
    with pytest.raises(ConnectionFailed, match="http transport reached"):
        await service.connect_and_discover("web", mode="sandbox")


# ---------------------------------------------------------------------------
# F05 — bounded, nonblocking, off-loop store lock
# ---------------------------------------------------------------------------


@_POSIX_ONLY
def test_store_flock_uses_nonblocking_acquisition(tmp_path, monkeypatch):
    import fcntl

    import machina.connectors.store as store_module

    flags = []
    real_flock = store_module.fcntl.flock

    def spy(fd, operation):
        flags.append(operation)
        return real_flock(fd, operation)

    monkeypatch.setattr(store_module.fcntl, "flock", spy)
    store = _store(tmp_path)
    store.configure({"id": "fixture", "command": "fixture"})
    assert flags, "configure must acquire the store lock"
    assert any(operation & fcntl.LOCK_NB for operation in flags), (
        "POSIX acquisition must be nonblocking under a deadline"
    )


@_POSIX_ONLY
async def test_async_store_write_stays_off_the_event_loop(tmp_path):
    import fcntl

    store = ConnectorStore(tmp_path / "mcp.json", lock_timeout=5.0)
    service = ConnectorService(store=store)
    lock_path = str(store.path) + ".lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)

    ticks = 0

    async def heartbeat():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.02)
            ticks += 1

    def release():
        time.sleep(0.15)
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    beat = asyncio.create_task(heartbeat())
    threading.Thread(target=release, daemon=True).start()
    try:
        await service.configure({"id": "fixture", "command": "fixture"})
    finally:
        beat.cancel()
    assert ticks >= 2, "the event loop kept running while the store lock was awaited"
    assert store.load()[1][0].id == "fixture"


@_POSIX_ONLY
async def test_async_store_write_times_out_typed_when_lock_is_held(tmp_path):
    import fcntl

    store = ConnectorStore(tmp_path / "mcp.json", lock_timeout=0.2)
    service = ConnectorService(store=store)
    lock_path = str(store.path) + ".lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        with pytest.raises(ConnectorStoreBusy):
            await asyncio.wait_for(
                service.configure({"id": "fixture", "command": "fixture"}), timeout=3
            )
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert not store.path.exists()


# ---------------------------------------------------------------------------
# F06 — configuration lock never held across credential-cache refresh
# ---------------------------------------------------------------------------


async def test_config_lock_not_held_during_auth_cache_refresh(tmp_path):
    from machina.connectors.oauth import OAuthCoordinator
    from machina.connectors.oauth_store import OAuthCredentialStore

    store = _store(tmp_path)
    coordinator = OAuthCoordinator(
        credential_store=OAuthCredentialStore(tmp_path / "oauth", key_provider=lambda: b"k" * 32)
    )
    service = ConnectorService(store=store, oauth_coordinator=coordinator)

    seen: list[bool] = []

    async def probe(connector_id, *, config=None):
        seen.append(service._configuration_lock.locked())

    coordinator.refresh_auth_cache = probe  # type: ignore[assignment]

    await service.configure(
        {
            "id": "gmail",
            "transport": "streamable_http",
            "endpoint": "https://example.com/mcp",
            "oauth": {"client_mode": "auto"},
        }
    )
    seen.clear()
    await service.refresh()
    assert seen and not any(seen), "refresh must release the config lock before auth refresh"

    seen.clear()
    await service.configure({"id": "other", "command": sys.executable})
    assert seen and not any(seen), "configure must not hold the config lock during auth refresh"

    seen.clear()
    await service.remove("other")
    assert seen and not any(seen), "remove must not hold the config lock during auth refresh"


@pytest.mark.parametrize("remove", [False, True])
def test_store_rejects_superseded_compensation(tmp_path, remove):
    from machina.connectors.store import ConnectorRevisionChanged, ConnectorStore

    store = ConnectorStore(tmp_path / "connectors.json")
    fields = {"id": "demo", "transport": "stdio", "command": "echo"}
    first = store.configure(fields)
    newer = store.configure({**fields, "args": ["new"]})
    with pytest.raises(ConnectorRevisionChanged):
        if remove:
            store.remove("demo", expected_revision=first.revision)
        else:
            store.configure(fields, expected_revision=first.revision)
    assert store.load()[1][0] == newer


@pytest.mark.parametrize("remove", [False, True])
async def test_cancelled_store_mutation_preserves_completed_result(tmp_path, monkeypatch, remove):
    from machina.core.background_tasks import CompletedOnCancellation

    store = _store(tmp_path)
    fields = {"id": "demo", "transport": "stdio", "command": "echo"}
    store.configure(fields)
    entered, release = threading.Event(), threading.Event()
    method = "remove" if remove else "configure"
    original = getattr(store, method)

    def gated(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(store, method, gated)
    operation = store.remove_async("demo") if remove else store.configure_async(fields)
    task = asyncio.create_task(operation)
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        release.set()
        with pytest.raises(CompletedOnCancellation) as caught:
            await task
        configs = store.load()[1]
        if remove:
            assert caught.value.result is True
            assert configs == []
        else:
            assert caught.value.result == configs[0]
            assert configs[0].revision == 2
    finally:
        release.set()
