"""Holistic-review L3 regressions: H13, H15, H16, H17 (connectors/OAuth).

Each test reproduces a finding from
``docs/verification/CODEBASE_REVIEW_HOLISTIC_2026-10-15.md`` before the fix:

- H13: an authorized OAuth connect holds the service configuration lock while
  its preflight resolves config through ``service.refresh()`` — the same
  non-reentrant lock — so the dispatch stalls.
- H15: every quiet stdio connector parks a default-executor worker in a
  blocking ``os.read`` on its stderr pipe; enough connectors starve unrelated
  ``to_thread`` work.
- H16: cancelling an authorization does not fence an in-flight store commit;
  the worker can publish credentials after the cancellation was reported.
- H17: a post-rename index-directory fsync failure makes the caller delete the
  envelope the replaced index references.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import errno
import os
import stat
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from machina.connectors.mcp_client import MCPConnection, _PipeStderr
from machina.connectors.models import ConnectionState, ConnectorConfig
from machina.connectors.oauth import OAuthCoordinator, OAuthOwner
from machina.connectors.oauth_models import (
    DiscoveredOAuth,
    OAuthCredentialRecord,
    RegisteredOAuthClient,
)
from machina.connectors.oauth_store import OAuthCredentialStore
from machina.connectors.service import ConnectorService
from machina.connectors.store import ConnectorStore


def _key_provider() -> bytes:
    return b"k" * 32


def _config_fields(**overrides):
    fields = {
        "id": "web",
        "transport": "streamable_http",
        "endpoint": "https://mcp.example.com/mcp",
        "oauth": {"client_mode": "auto"},
        "connect_timeout": 5,
        "call_timeout": 5,
    }
    fields.update(overrides)
    return fields


async def _seed_credential(creds, coordinator, config, *, token="access-1", grant="grant-1"):
    binding = coordinator._binding_for(
        config, issuer="https://issuer.example", client_id="client-1"
    )
    now = datetime.now(UTC)
    record = OAuthCredentialRecord(
        version=1,
        credential_key=binding.credential_key,
        connector_id=config.id,
        config_revision=config.revision,
        resource=binding.resource,
        issuer=binding.issuer,
        client_id=binding.client_id,
        authorization_endpoint="https://issuer.example/authorize",
        token_endpoint="https://issuer.example/token",
        revocation_endpoint="",
        redirect_uri_used="http://127.0.0.1:9/oauth/callback",
        token_endpoint_auth_method="none",
        grant_id=grant,
        generation=1,
        approved_scopes=("read", "write"),
        granted_scopes=("read", "write"),
        access_token=token,
        refresh_token="",
        acquired_at_utc=now.isoformat(),
        expires_at_utc=(now + timedelta(hours=1)).isoformat(),
        refresh_state="idle",
    )
    await creds.save(record, expected_generation=0)
    return binding, record


async def _wait_until(predicate, timeout=5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise TimeoutError("condition never became true")
        await asyncio.sleep(0.01)


class _InstrumentedRefresh:
    """Wraps ``service.refresh`` and records lock ownership per call."""

    def __init__(self, service):
        self.service = service
        self.observations: list[bool] = []
        self._real = service.refresh

    async def __call__(self):
        self.observations.append(self.service._configuration_lock.locked())
        await self._real()


# ---------------------------------------------------------------------------
# H13 — OAuth connect preflight must not re-enter the configuration lock
# ---------------------------------------------------------------------------


async def test_oauth_preflight_never_reenters_service_refresh_under_config_lock(tmp_path):
    """H13 (focused): the real lock is held exactly like connect_authorized
    holds it while dispatching; the real preflight must not call
    ``service.refresh()`` (a non-reentrant lock re-entry that stalls)."""
    store = ConnectorStore(tmp_path / "mcp.json")
    creds = OAuthCredentialStore(tmp_path / "oauth", key_provider=_key_provider)
    coordinator = OAuthCoordinator(credential_store=creds)
    service = ConnectorService(store=store, oauth_coordinator=coordinator)
    try:
        await service.configure(_config_fields())
        config = service.registry.get("web")
        await _seed_credential(creds, coordinator, config)
        await service.refresh()  # populated the auth cache, no lock held
        instrumented = _InstrumentedRefresh(service)
        service.refresh = instrumented  # type: ignore[assignment]

        connection = MCPConnection(config, oauth_coordinator=coordinator)
        async with service._configuration_lock:
            auth, token = await asyncio.wait_for(
                connection._oauth_preflight(coordinator), timeout=2
            )
            assert token == "access-1"
            assert auth is not None
            # The transport auth adapter is the second call site: every
            # handshake request asks the coordinator for a token while the
            # connect dispatch still holds the lock.
            import httpx2

            flow = auth.async_auth_flow(httpx2.Request("POST", config.endpoint))
            request = await asyncio.wait_for(anext(flow), timeout=2)
            assert request.headers["Authorization"] == f"Bearer {token}"
        assert not any(instrumented.observations), (
            "the OAuth preflight called service.refresh() while the "
            "configuration lock was held (H13 recursion)"
        )
    finally:
        await coordinator.shutdown()


async def test_authorized_oauth_connect_completes_preflight_under_config_lock(
    monkeypatch, tmp_path
):
    """H13 (end-to-end): the reviewed connect dispatch runs the real preflight
    while holding the configuration lock; it must complete, not stall."""
    from unittest.mock import AsyncMock

    from machina.connectors.execution import connect_authorized, prepare_connect
    from machina.connectors.manager import ConnectorManager
    from machina.core.execution_context import ExecutionContext
    from machina.permissions import store as perm_store
    from machina.permissions.broker import clear_execution_context

    monkeypatch.setattr(perm_store, "_db", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "machina.core.execution_context.resolve_execution_context",
        AsyncMock(return_value=ExecutionContext("ask", "host", tmp_path)),
    )

    class PreflightConnection(MCPConnection):
        """Runs the production preflight at the point the manager connects."""

        async def connect(self, secrets=None):  # type: ignore[override]
            _auth, token = await self._oauth_preflight(self._oauth_coordinator)
            self.register_oauth_token(token)
            self.state = ConnectionState.CONNECTED

        async def disconnect(self):
            self.state = ConnectionState.DISCONNECTED

        async def list_tools(self):
            return [], False

    store = ConnectorStore(tmp_path / "mcp.json")
    creds = OAuthCredentialStore(tmp_path / "oauth", key_provider=_key_provider)
    coordinator = OAuthCoordinator(credential_store=creds)
    service = ConnectorService(store=store, oauth_coordinator=coordinator)
    service.manager = ConnectorManager(
        service.registry, client_factory=PreflightConnection, oauth_coordinator=coordinator
    )
    try:
        await service.configure(_config_fields())
        config = service.registry.get("web")
        await _seed_credential(creds, coordinator, config)
        instrumented = _InstrumentedRefresh(service)
        service.refresh = instrumented  # type: ignore[assignment]

        preview = await prepare_connect(service, "web", "session", "harness")
        connected = await asyncio.wait_for(
            connect_authorized(service, "web", "session", "harness", preview["digest"]),
            timeout=5,
        )
        assert connected["state"] == "connected"
        assert instrumented.observations, "connect must refresh before dispatch"
        assert not any(instrumented.observations), (
            "connect dispatch re-entered service.refresh while holding the configuration lock (H13)"
        )
    finally:
        clear_execution_context()
        await service.manager.shutdown_all()
        await coordinator.shutdown()


# ---------------------------------------------------------------------------
# H15 — quiet stdio stderr drains must not occupy the shared executor
# ---------------------------------------------------------------------------


async def test_quiet_stderr_drains_never_occupy_the_shared_executor():
    """H15: many quiet connectors plus one unrelated to_thread task.

    The default executor is capped at two workers, so pre-fix two quiet
    stderr drains occupy every worker and the unrelated task never starts.
    """
    loop = asyncio.get_running_loop()
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="h15")
    loop.set_default_executor(pool)
    drains = [_PipeStderr() for _ in range(4)]
    for drain in drains:
        drain.start()
    try:
        drains[0].file.write("boot-log\n")
        drains[0].file.flush()
        await _wait_until(lambda: "boot-log" in drains[0].read_text(), timeout=2)
        assert await asyncio.wait_for(asyncio.to_thread(lambda: 6 * 7), timeout=2) == 42
    finally:
        for drain in drains:
            await drain.close()
        pool.shutdown(wait=False)


# ---------------------------------------------------------------------------
# H16 — cancellation must fence an in-flight credential publication
# ---------------------------------------------------------------------------


class _FakeCallback:
    def __init__(self):
        self.redirect_uri = "http://127.0.0.1:9/oauth/callback"

    def arm(self, **_kwargs):
        return None

    async def wait(self, *, deadline):
        return SimpleNamespace(ok=True, code="code-1")

    async def aclose(self):
        return None


class _FakeCallbackFactory:
    @classmethod
    async def reserve(cls, **_kwargs):
        return _FakeCallback()


class _FakeNetwork:
    async def discover(self, config, *, issuer=None, deadline=None):
        return DiscoveredOAuth(
            resource="https://mcp.example.com/mcp",
            issuers=("https://issuer.example",),
            selected_issuer="https://issuer.example",
            authorization_endpoint="https://issuer.example/authorize",
            token_endpoint="https://issuer.example/token",
            registration_endpoint="https://issuer.example/register",
            revocation_endpoint="",
            scopes=("read", "write"),
            require_iss=False,
            supports_s256=True,
            fingerprint="fingerprint-1",
            supports_client_metadata_document=False,
        )

    async def register(self, snapshot, *, deadline):
        return RegisteredOAuthClient(
            client_id="client-1",
            token_endpoint_auth_method="none",
            redirect_uri=snapshot.redirect_uri,
        )

    async def exchange_code(self, snapshot, client, code, verifier, *, deadline):
        return SimpleNamespace(
            access_token="access-1",
            refresh_token="refresh-1",
            scope="read write",
            expires_in=3600,
        )

    async def aclose(self):
        return None


async def test_cancel_fences_in_flight_credential_publication(monkeypatch, tmp_path):
    """H16: a cancellation reported while ``save``'s worker is mid-commit must
    not leave a published credential behind."""
    from machina.permissions import broker

    config = ConnectorConfig(**_config_fields()).validate()
    creds = OAuthCredentialStore(tmp_path / "oauth", key_provider=_key_provider)
    coordinator = OAuthCoordinator(
        config_resolver=lambda _cid: config,
        credential_store=creds,
        network_factory=lambda **_kwargs: _FakeNetwork(),
        callback_factory=_FakeCallbackFactory,
    )

    async def allow_guard(req):
        return req

    monkeypatch.setattr(broker.permission_broker, "guard", allow_guard, raising=True)
    entered = threading.Event()
    release = threading.Event()
    written = threading.Event()
    real_write = creds._write_index

    def slow_write(index):
        entered.set()
        assert release.wait(5)
        real_write(index)
        written.set()

    monkeypatch.setattr(creds, "_write_index", slow_write)

    owner = OAuthOwner(session_id="sess-h16", agent_id="agent-h16")
    preview = await coordinator.preview("web", owner)
    started = await coordinator.start(
        "web", owner, preview_id=preview["previewId"], digest=preview["digest"]
    )
    operation_id = started["operation"]["operationId"]
    await _wait_until(entered.is_set, timeout=5)

    cancel_task = asyncio.create_task(coordinator.cancel(owner, operation_id=operation_id))
    # Let cancel() observe the operation and start fencing; the commit worker
    # holds the index lock, so the fence must wait for it.
    await asyncio.sleep(0.1)
    release.set()
    assert await asyncio.wait_for(cancel_task, timeout=5) is True
    await _wait_until(written.is_set, timeout=5)

    binding = coordinator._binding_for(
        config, issuer="https://issuer.example", client_id="client-1"
    )
    assert await creds.load(binding) is None, (
        "a cancelled authorization published its credential after cancel() reported (H16)"
    )
    view = await coordinator.status("web", owner, operation_id=operation_id)
    assert view["operation"]["state"] == "cancelled"


# ---------------------------------------------------------------------------
# H17 — index publication failure must not delete a referenced envelope
# ---------------------------------------------------------------------------


async def test_index_dir_fsync_failure_keeps_referenced_envelope(monkeypatch, tmp_path):
    """H17: fail the index directory fsync after the rename; the visible index
    must keep a loadable credential (pre-fix the envelope was deleted)."""
    config = ConnectorConfig(**_config_fields()).validate()
    creds = OAuthCredentialStore(tmp_path / "oauth", key_provider=_key_provider)
    coordinator = OAuthCoordinator(credential_store=creds)
    binding, record = await _seed_credential(creds, coordinator, config)

    real_fsync = os.fsync
    dir_events = {"count": 0}

    def failing_index_fsync(fd):
        try:
            is_dir = stat.S_ISDIR(os.fstat(fd).st_mode)
        except OSError:
            is_dir = False
        if is_dir:
            dir_events["count"] += 1
            if dir_events["count"] == 2:  # 1st: grants/ envelope, 2nd: index dir
                raise OSError(errno.EIO, "injected index directory fsync failure")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", failing_index_fsync)

    rotated = replace(record, generation=2, grant_id="grant-2", access_token="access-2")
    saved = await creds.save(rotated, expected_generation=1)
    assert saved.grant_id == "grant-2"
    index = creds._read_index()
    entry = index["grants"][binding.credential_key]
    envelope = creds.root / "grants" / entry["envelope"]
    assert envelope.exists(), "the visible index references a deleted envelope (H17)"
    loaded = await creds.load(binding)
    assert loaded is not None and loaded.access_token == "access-2"
