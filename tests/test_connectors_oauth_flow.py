"""Task 5: coordinator code grant through human review (real local fixture)."""

import asyncio
import json
import time
import uuid

import httpx
import pytest

from ah.connectors.models import ConnectorConfig
from ah.connectors.oauth import OAuthCoordinator, OAuthOwner
from ah.connectors.oauth_store import OAuthCredentialStore
from ah.connectors.registry import ConnectorRegistry
from ah.connectors.service import ConnectorService
from tests.support.mcp_oauth_fixture import OAuthFixture


def _owner():
    return OAuthOwner(
        session_id=f"sess-{uuid.uuid4().hex[:8]}", agent_id=f"agent-{uuid.uuid4().hex[:8]}"
    )


def _service(fixture, *, oauth_settings=None, tmp_path=None):
    registry = ConnectorRegistry()
    registry.add(
        ConnectorConfig(
            id="remote",
            transport="streamable_http",
            endpoint=fixture.endpoint,
            local_access=True,
            oauth=oauth_settings or {"client_mode": "auto"},
        )
    )
    creds = OAuthCredentialStore(tmp_path / "oauth", key_provider=lambda: b"k" * 32)
    coordinator = OAuthCoordinator(credential_store=creds)
    service = ConnectorService(registry=registry, oauth_coordinator=coordinator)
    coordinator._service = service
    return service, coordinator


async def _wait_state(coordinator, connector_id, owner, operation_id, want, timeout=30):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        status = await coordinator.status(connector_id, owner, operation_id=operation_id)
        state = status["operation"]["state"]
        if state == want or state in ("failed", "cancelled", "expired"):
            return status
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError(f"operation stuck at {state}")
        await asyncio.sleep(0.1)


async def _drive_browser(coordinator, owner, operation_id):
    """Simulate the external browser: authorize then follow the redirect."""
    url = await coordinator.authorization_url(operation_id, owner)
    async with httpx.AsyncClient(follow_redirects=False, timeout=10) as client:
        response = await client.get(url)
        assert response.status_code == 302, response.text
        callback_url = response.headers["location"]
        assert callback_url.startswith("http://127.0.0.1:")
        # The loopback callback receives the provider redirect.
        delivered = await client.get(callback_url)
        assert delivered.status_code == 200


async def _full_login(fixture, coordinator, owner, connector_id="remote"):
    preview = await coordinator.preview(connector_id, owner)
    assert "previewId" in preview
    started = await coordinator.start(
        connector_id, owner, preview_id=preview["previewId"], digest=preview["digest"]
    )
    operation_id = started["operation"]["operationId"]
    await asyncio.wait_for(
        _wait_state(coordinator, connector_id, owner, operation_id, "awaiting_browser"), timeout=30
    )
    await _drive_browser(coordinator, owner, operation_id)
    final = await asyncio.wait_for(
        _wait_state(coordinator, connector_id, owner, operation_id, "complete"), timeout=30
    )
    return preview, started, final


def test_dcr_pkce_flow_persists_encrypted_grant(tmp_path):
    async def _run():
        fixture = OAuthFixture()
        await fixture.start()
        try:
            service, coordinator = _service(fixture, tmp_path=tmp_path)
            owner = _owner()
            preview, started, final = await _full_login(fixture, coordinator, owner)
            assert final["operation"]["state"] == "complete"
            # Registration happened only after reviewed start.
            assert fixture.counters["register"] == 1
            assert fixture.counters["exchange"] == 1
            status = await coordinator.status("remote", owner)
            assert status["auth"]["state"] == "authorized"
            assert status["auth"]["grantedScopes"] == ["read", "write"]
            # Encrypted at rest: no token bytes on disk or in public views.
            blob = json.dumps(status)
            assert "access-" not in blob and "refresh-" not in blob
            for path in (tmp_path / "oauth").rglob("*"):
                if path.is_file():
                    raw = path.read_bytes()
                    assert b"access-" not in raw and b"refresh-" not in raw
            # Start returns promptly; work finishes via poll.
            other = _owner()
            preview2 = await coordinator.preview("remote", other)
            begun = time.monotonic()
            await coordinator.start(
                "remote", other, preview_id=preview2["previewId"], digest=preview2["digest"]
            )
            assert time.monotonic() - begun < 5
        finally:
            await coordinator.shutdown()
            await fixture.stop()

    asyncio.run(_run())


def test_preview_freeze_and_ownership(tmp_path):
    async def _run():
        fixture = OAuthFixture()
        await fixture.start()
        try:
            service, coordinator = _service(fixture, tmp_path=tmp_path)
            owner = _owner()
            preview = await coordinator.preview("remote", owner)
            assert fixture.counters["register"] == 0  # no registration yet
            # Wrong digest, wrong owner, reuse all refuse.
            with pytest.raises(Exception):
                await coordinator.start(
                    "remote", owner, preview_id=preview["previewId"], digest="0" * 64
                )
            with pytest.raises(Exception):
                await coordinator.start(
                    "remote", _owner(), preview_id=preview["previewId"], digest=preview["digest"]
                )
            started = await coordinator.start(
                "remote", owner, preview_id=preview["previewId"], digest=preview["digest"]
            )
            operation_id = started["operation"]["operationId"]
            with pytest.raises(Exception):
                await coordinator.start(
                    "remote", owner, preview_id=preview["previewId"], digest=preview["digest"]
                )
            # Foreign session cannot read the URL or poll the operation.
            with pytest.raises(Exception):
                await coordinator.authorization_url(operation_id, _owner())
            with pytest.raises(Exception):
                await coordinator.status("remote", _owner(), operation_id=operation_id)
            await coordinator.cancel(owner, operation_id=operation_id)
        finally:
            await coordinator.shutdown()
            await fixture.stop()

    asyncio.run(_run())


def test_preregistered_and_basic_and_metadata_document_modes(tmp_path):
    async def _run():
        fixture = OAuthFixture()
        fixture.preregister("public-client")
        fixture.preregister("conf-client", secret="conf-secret-value")
        await fixture.start()
        try:
            # Public pre-registered client: no DCR traffic.
            service, coordinator = _service(
                fixture,
                tmp_path=tmp_path,
                oauth_settings={"client_mode": "preregistered", "client_id": "public-client"},
            )
            owner = _owner()
            await _full_login(fixture, coordinator, owner)
            assert fixture.counters["register"] == 0
            await coordinator.shutdown()
            # Explicit Basic client resolves the named secret only at exchange.
            import os

            os.environ["OAUTH_FIXTURE_CLIENT_SECRET"] = "conf-secret-value"
            service2, coordinator2 = _service(
                fixture,
                tmp_path=tmp_path,
                oauth_settings={
                    "client_mode": "preregistered",
                    "client_id": "conf-client",
                    "client_secret_ref": "OAUTH_FIXTURE_CLIENT_SECRET",
                    "token_endpoint_auth_method": "client_secret_basic",
                },
            )
            await _full_login(fixture, coordinator2, owner)
            await coordinator2.shutdown()
            del os.environ["OAUTH_FIXTURE_CLIENT_SECRET"]
        finally:
            await fixture.stop()

    asyncio.run(_run())


def test_unsupported_registration_and_scope_widening_fail(tmp_path):
    async def _run():
        fixture = OAuthFixture()
        fixture.switches["incompatible_registration"] = True
        await fixture.start()
        try:
            service, coordinator = _service(fixture, tmp_path=tmp_path)
            owner = _owner()
            preview = await coordinator.preview("remote", owner)
            started = await coordinator.start(
                "remote", owner, preview_id=preview["previewId"], digest=preview["digest"]
            )
            operation_id = started["operation"]["operationId"]
            final = await _wait_state(
                coordinator, "remote", owner, operation_id, "complete", timeout=30
            )
            assert final["operation"]["state"] == "failed"
            await coordinator.shutdown()
        finally:
            await fixture.stop()
        fixture2 = OAuthFixture()
        fixture2.switches["scope_widening"] = True
        await fixture2.start()
        try:
            service2, coordinator2 = _service(fixture2, tmp_path=tmp_path)
            owner2 = _owner()
            preview2 = await coordinator2.preview("remote", owner2)
            started2 = await coordinator2.start(
                "remote", owner2, preview_id=preview2["previewId"], digest=preview2["digest"]
            )
            operation_id2 = started2["operation"]["operationId"]
            await _wait_state(
                coordinator2, "remote", owner2, operation_id2, "awaiting_browser", timeout=30
            )
            await _drive_browser(coordinator2, owner2, operation_id2)
            final2 = await _wait_state(
                coordinator2, "remote", owner2, operation_id2, "complete", timeout=30
            )
            assert final2["operation"]["state"] == "failed"
            await coordinator2.shutdown()
        finally:
            await fixture2.stop()

    asyncio.run(_run())


def test_lost_exchange_response_never_reuses_code(tmp_path):
    async def _run():
        fixture = OAuthFixture()
        fixture.switches["lost_exchange_response"] = True
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
            # Exchange hangs (response lost); cancel and start a fresh login.
            await asyncio.sleep(1)
            await coordinator.cancel(owner, operation_id=operation_id)
            first_count = fixture.counters["exchange"]
            assert first_count == 1
            # A fresh operation cannot replay the consumed code.
            preview2 = await coordinator.preview("remote", owner)
            assert preview2["previewId"] != preview["previewId"]
            await coordinator.shutdown()
        finally:
            await fixture.stop()

    asyncio.run(_run())
