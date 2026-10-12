"""Task 7: mutation and runtime cancellation races."""

import asyncio

import pytest

from ah.connectors.models import ConnectorConfig
from ah.connectors.oauth import OAuthCoordinator
from ah.connectors.oauth_models import OAuthBinding
from ah.connectors.oauth_store import OAuthCredentialStore
from ah.connectors.registry import ConnectorRegistry
from ah.connectors.service import ConnectorService
from tests.support.mcp_oauth_fixture import OAuthFixture
from tests.test_connectors_oauth_flow import _drive_browser, _full_login, _owner, _wait_state


def _service(fixture, *, tmp_path):
    registry = ConnectorRegistry()
    registry.add(
        ConnectorConfig(
            id="remote",
            transport="streamable_http",
            endpoint=fixture.endpoint,
            local_access=True,
            oauth={"client_mode": "auto"},
        )
    )
    creds = OAuthCredentialStore(tmp_path / "oauth", key_provider=lambda: b"k" * 32)
    coordinator = OAuthCoordinator(credential_store=creds)
    service = ConnectorService(registry=registry, oauth_coordinator=coordinator)
    coordinator._service = service
    return service, coordinator


def test_late_login_after_forget_cannot_restore_credentials(tmp_path):
    async def _run():
        fixture = OAuthFixture()
        fixture.suspend_exchange.clear()  # hold the token response
        await fixture.start()
        try:
            service, coordinator = _service(fixture, tmp_path=tmp_path)
            owner = _owner()
            preview = await coordinator.preview("remote", owner)
            started = await coordinator.start(
                "remote", owner, preview_id=preview["previewId"], digest=preview["digest"]
            )
            operation_id = started["operation"]["operationId"]
            await _wait_state(
                coordinator, "remote", owner, operation_id, "awaiting_browser", timeout=30
            )
            await _drive_browser(coordinator, owner, operation_id)
            # Exchange is now suspended inside the fixture; forget meanwhile.
            await asyncio.sleep(0.5)
            status_before = await coordinator.status("remote", owner)
            generation = status_before["auth"].get("generation", 0)
            forgotten = await coordinator.forget(
                "remote", owner, expected_generation=generation, revoke=False
            )
            assert forgotten["auth"]["state"] in ("signed_out", "storage_unavailable")
            # Release the suspended exchange: late writer must fail closed.
            fixture.suspend_exchange.set()
            await asyncio.sleep(1.5)
            status_after = await coordinator.status("remote", owner)
            assert status_after["auth"]["state"] != "authorized", status_after
            await coordinator.shutdown()
        finally:
            fixture.suspend_exchange.set()
            await fixture.stop()

    asyncio.run(_run())


def test_refresh_racing_logout_cannot_persist(tmp_path):
    async def _run():
        fixture = OAuthFixture()
        await fixture.start()
        try:
            service, coordinator = _service(fixture, tmp_path=tmp_path)
            owner = _owner()
            await _full_login(fixture, coordinator, owner)
            identity = coordinator.auth_identity("remote")
            binding = OAuthBinding(
                connector_id="remote",
                credential_key=identity["credentialKey"],
                resource=identity["resource"],
                issuer=identity["issuer"],
                client_id=identity["clientId"],
            )
            record = await coordinator._creds.load(binding)
            record.expires_at_utc = "2020-01-01T00:00:00+00:00"
            await coordinator._creds.save(record, expected_generation=record.generation)
            # Suspend the refresh POST at the fixture by blocking responses:
            # forget first, then attempt refresh with the stale grant.
            await coordinator.forget(
                "remote", owner, expected_generation=identity["generation"], revoke=False
            )
            loop = asyncio.get_running_loop()
            with pytest.raises(Exception):
                await coordinator.ensure_token(
                    binding, expected_grant=identity, deadline=loop.time() + 10
                )
            status = await coordinator.status("remote", owner)
            assert status["auth"]["state"] != "authorized"
            await coordinator.shutdown()
        finally:
            await fixture.stop()

    asyncio.run(_run())


def test_disconnect_preserves_forget_removes_and_shutdown_cleans(tmp_path):
    async def _run():
        fixture = OAuthFixture()
        await fixture.start()
        try:
            service, coordinator = _service(fixture, tmp_path=tmp_path)
            owner = _owner()
            await _full_login(fixture, coordinator, owner)
            # Disconnect preserves authorized credentials.
            await service.disconnect("remote")
            status = await coordinator.status("remote", owner)
            assert status["auth"]["state"] == "authorized"
            # Session-switch style cancel of a new operation cleans up.
            preview = await coordinator.preview("remote", owner)
            started = await coordinator.start(
                "remote", owner, preview_id=preview["previewId"], digest=preview["digest"]
            )
            operation_id = started["operation"]["operationId"]
            assert await coordinator.cancel(owner, operation_id=operation_id) is True
            cancelled = await coordinator.status("remote", owner, operation_id=operation_id)
            assert cancelled["operation"]["state"] == "cancelled"
            # Forget removes eligibility.
            generation = status["auth"]["generation"]
            forgotten = await coordinator.forget(
                "remote", owner, expected_generation=generation, revoke=True
            )
            assert forgotten["auth"]["state"] != "authorized"
            assert forgotten["remoteRevocation"] in (
                "succeeded",
                "unsupported",
                "failed",
                "not_requested",
            )
            # Shutdown closes listeners/clients with no leaks.
            await coordinator.shutdown()
            assert not coordinator._previews
        finally:
            await fixture.stop()

    asyncio.run(_run())


def test_shutdown_awaits_operation_cleanup_without_holding_state_lock(tmp_path):
    async def run():
        coordinator = OAuthCoordinator(
            credential_store=OAuthCredentialStore(
                tmp_path / "oauth", key_provider=lambda: b"k" * 32
            )
        )
        started = asyncio.Event()
        finished = asyncio.Event()

        class Callback:
            async def aclose(self):
                await asyncio.sleep(0)

        async def operation():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                async with coordinator._lock:
                    await asyncio.sleep(0)
                    finished.set()

        task = asyncio.create_task(operation())
        coordinator._operations["op"] = {"task": task, "callback": Callback()}
        await started.wait()
        await coordinator.shutdown(deadline=0.5)
        assert task.done()
        assert finished.is_set()

    asyncio.run(run())


@pytest.mark.parametrize("action", ["url", "open", "cancel", "preview_cancel"])
@pytest.mark.parametrize("change", ["owner", "revision"])
def test_existing_handles_revalidate_actual_connector_authority(tmp_path, action, change):
    async def run():
        from types import SimpleNamespace

        from ah.connectors.oauth import OAuthError

        owner = _owner()
        config = ConnectorConfig(
            id="web",
            transport="streamable_http",
            endpoint="https://mcp.example.com/mcp",
            revision=1,
            owner=owner.agent_id,
            oauth={"client_mode": "auto"},
        )
        coordinator = OAuthCoordinator(
            config_resolver=lambda connector_id: (
                config
                if connector_id == "web"
                else pytest.fail("actual handle connector must be resolved")
            ),
            credential_store=OAuthCredentialStore(
                tmp_path / "oauth", key_provider=lambda: b"k" * 32
            ),
            browser_opener=lambda _: pytest.fail("unauthorized browser opened"),
        )

        class Callback:
            closed = False

            async def aclose(self):
                self.closed = True

        callback = Callback()
        handle = {
            "connector_id": "web",
            "owner": owner,
            "snapshot": SimpleNamespace(config_revision=1),
            "state": "awaiting_browser",
            "authorization_url": "https://auth.example.com/authorize",
            "task": None,
            "callback": callback,
        }
        coordinator._operations["op"] = handle
        coordinator._previews["preview"] = dict(handle)
        if change == "owner":
            config.owner = "other-agent"
        else:
            config.revision = 2
        with pytest.raises(OAuthError) as exc:
            if action == "url":
                await coordinator.authorization_url("op", owner)
            elif action == "open":
                await coordinator.open_browser("op", owner)
            elif action == "cancel":
                await coordinator.cancel(owner, operation_id="op")
            else:
                await coordinator.cancel(owner, preview_id="preview")
        assert exc.value.reason == (
            "oauth_operation_not_found" if change == "owner" else "oauth_config_changed"
        )
        assert handle["state"] == "awaiting_browser"
        assert not callback.closed
        await coordinator.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize("action", ["url", "open", "cancel", "preview_cancel"])
def test_handle_cannot_masquerade_as_supplied_connector(tmp_path, action):
    async def run():
        from types import SimpleNamespace

        from ah.connectors.oauth import OAuthError

        owner = _owner()
        config = ConnectorConfig(
            id="web",
            transport="streamable_http",
            endpoint="https://mcp.example.com/mcp",
            revision=1,
            oauth={"client_mode": "auto"},
        )
        coordinator = OAuthCoordinator(
            config_resolver=lambda _: config,
            credential_store=OAuthCredentialStore(
                tmp_path / "oauth", key_provider=lambda: b"k" * 32
            ),
            browser_opener=lambda _: pytest.fail("wrong connector browser opened"),
        )
        handle = {
            "connector_id": "web",
            "owner": owner,
            "snapshot": SimpleNamespace(config_revision=1),
            "state": "awaiting_browser",
            "authorization_url": "https://auth.example.com/authorize",
            "task": None,
        }
        coordinator._operations["op"] = handle
        coordinator._previews["preview"] = dict(handle)
        with pytest.raises(OAuthError) as exc:
            if action == "url":
                await coordinator.authorization_url("op", owner, connector_id="different")
            elif action == "open":
                await coordinator.open_browser("op", owner, connector_id="different")
            elif action == "cancel":
                await coordinator.cancel(owner, operation_id="op", connector_id="different")
            else:
                await coordinator.cancel(owner, preview_id="preview", connector_id="different")
        assert exc.value.reason == "oauth_operation_not_found"
        assert handle["state"] == "awaiting_browser"

    asyncio.run(run())


async def _two_provider_grants(tmp_path):
    from tests.test_connectors_oauth_refresh import _seed_refresh
    from tests.test_connectors_oauth_store import _record

    coordinator, config, old_binding, identity = await _seed_refresh(tmp_path)
    old_record = await coordinator._creds.load(old_binding)
    config.endpoint = "https://new-mcp.example.com/mcp"
    config.revision = 2
    new_binding = coordinator._binding_for(
        config, issuer="https://new-auth.example.com", client_id="new-client"
    )
    new_record = _record(
        new_binding, config_revision=2, client_id="new-client", grant_id="new-grant"
    )
    await coordinator._creds.save(new_record, expected_generation=0)
    return coordinator, config, old_binding, old_record, new_binding, new_record


def test_forget_retires_all_provider_bindings_without_late_resurrection(tmp_path):
    async def run():
        (
            coordinator,
            config,
            old_binding,
            old_record,
            new_binding,
            new_record,
        ) = await _two_provider_grants(tmp_path)
        forgotten = await coordinator.forget("web", _owner(), expected_generation=1, revoke=False)
        assert forgotten["auth"]["state"] == "signed_out"
        for binding, record in [(old_binding, old_record), (new_binding, new_record)]:
            assert await coordinator._creds.load(binding) is None
            record.generation = 2
            with pytest.raises(ValueError):
                await coordinator._creds.save(record, expected_generation=1)

    asyncio.run(run())


@pytest.mark.parametrize("retire_old_only", [True, False])
def test_config_invalidation_tombstones_retired_provider_bindings(tmp_path, retire_old_only):
    async def run():
        (
            coordinator,
            config,
            old_binding,
            old_record,
            new_binding,
            new_record,
        ) = await _two_provider_grants(tmp_path)
        if retire_old_only:
            await coordinator.invalidate_connector("web", config_revision=1)
        else:
            await coordinator.invalidate_connector("web")
        assert await coordinator._creds.load(old_binding) is None
        old_record.generation = 2
        with pytest.raises(ValueError):
            await coordinator._creds.save(old_record, expected_generation=1)
        if retire_old_only:
            assert await coordinator._creds.load(new_binding) is not None
            await coordinator.refresh_auth_cache("web")
            assert coordinator.auth_identity("web")["grantId"] == "new-grant"
        else:
            assert await coordinator._creds.load(new_binding) is None
            new_record.generation = 2
            with pytest.raises(ValueError):
                await coordinator._creds.save(new_record, expected_generation=1)

    asyncio.run(run())


def test_forget_tombstones_pending_exchange_binding_before_first_commit(tmp_path):
    async def run():
        from types import SimpleNamespace

        from tests.test_connectors_oauth_store import _binding, _record

        config = ConnectorConfig(
            id="web",
            transport="streamable_http",
            endpoint="https://mcp.example.com/mcp",
            revision=1,
            oauth={"client_mode": "auto"},
        )
        coordinator = OAuthCoordinator(
            config_resolver=lambda _: config,
            credential_store=OAuthCredentialStore(
                tmp_path / "oauth", key_provider=lambda: b"k" * 32
            ),
        )
        binding = _binding()
        coordinator._operations["pending"] = {
            "connector_id": "web",
            "state": "exchanging",
            "task": None,
            "callback": None,
            "snapshot": SimpleNamespace(
                resource=binding.resource, issuer=binding.issuer, config_revision=1
            ),
            "client": SimpleNamespace(client_id=binding.client_id),
        }
        await coordinator.forget("web", _owner(), expected_generation=0, revoke=False)
        with pytest.raises(ValueError):
            await coordinator._creds.save(_record(binding), expected_generation=0)
        assert await coordinator._creds.load(binding) is None

    asyncio.run(run())


def test_config_invalidation_preserves_newer_login_on_reused_binding(tmp_path):
    async def run():
        from types import SimpleNamespace

        from tests.test_connectors_oauth_refresh import _seed_refresh

        coordinator, config, binding, identity = await _seed_refresh(tmp_path)
        record = await coordinator._creds.load(binding)
        record.config_revision = 2
        record.generation = 2
        record.grant_id = "newer-login"
        await coordinator._creds.save(record, expected_generation=1)
        coordinator._operations["old-flow"] = {
            "connector_id": "web",
            "state": "complete",
            "task": None,
            "snapshot": SimpleNamespace(
                resource=binding.resource, issuer=binding.issuer, config_revision=1
            ),
            "client": SimpleNamespace(client_id=binding.client_id),
        }
        await coordinator.invalidate_connector("web", config_revision=1)
        kept = await coordinator._creds.load(binding)
        assert kept is not None
        assert kept.grant_id == "newer-login"
        assert kept.generation == 2

    asyncio.run(run())
