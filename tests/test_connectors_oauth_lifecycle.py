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
