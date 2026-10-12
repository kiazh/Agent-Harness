"""Task 5: coordinator code grant through human review (real local fixture)."""

import asyncio
import json
import time
import uuid

import httpx
import pytest

from machina.connectors.models import ConnectorConfig
from machina.connectors.oauth import OAuthCoordinator, OAuthOwner
from machina.connectors.oauth_store import OAuthCredentialStore
from machina.connectors.registry import ConnectorRegistry
from machina.connectors.service import ConnectorService
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


def test_preregistered_and_basic_and_metadata_document_modes(tmp_path, monkeypatch):
    async def _run():
        import socket

        # Reserve an available port only to choose the registered exact URI;
        # the coordinator owns the actual listener throughout each login.
        with socket.socket() as candidate:
            candidate.bind(("127.0.0.1", 0))
            callback_port = candidate.getsockname()[1]
        callback_uri = f"http://127.0.0.1:{callback_port}/provider/callback"
        fixture = OAuthFixture()
        fixture.preregister("public-client", redirect_uri=callback_uri)
        fixture.preregister("conf: +é", redirect_uri=callback_uri, secret="conf: +&é")
        await fixture.start()
        coordinator = coordinator2 = None
        try:
            # Public pre-registered client: no DCR traffic.
            service, coordinator = _service(
                fixture,
                tmp_path=tmp_path,
                oauth_settings={
                    "client_mode": "preregistered",
                    "client_id": "public-client",
                    "callback_port": callback_port,
                    "callback_path": "/provider/callback",
                },
            )
            owner = _owner()
            _, _, final = await _full_login(fixture, coordinator, owner)
            assert final["operation"]["state"] == "complete"
            assert fixture.counters["register"] == 0
            await coordinator.shutdown()
            # Explicit Basic client resolves the named secret only at exchange.
            monkeypatch.setenv("OAUTH_FIXTURE_CLIENT_SECRET", "conf: +&é")
            service2, coordinator2 = _service(
                fixture,
                tmp_path=tmp_path,
                oauth_settings={
                    "client_mode": "preregistered",
                    "client_id": "conf: +é",
                    "callback_port": callback_port,
                    "callback_path": "/provider/callback",
                    "client_secret_ref": "OAUTH_FIXTURE_CLIENT_SECRET",
                    "token_endpoint_auth_method": "client_secret_basic",
                },
            )
            _, _, final = await _full_login(fixture, coordinator2, owner)
            assert final["operation"]["state"] == "complete"
            assert fixture.counters["exchange"] == 2
            await coordinator2.shutdown()
        finally:
            for active in (coordinator, coordinator2):
                if active is not None:
                    await active.shutdown()
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


def test_advertised_response_issuer_is_required_before_exchange(tmp_path):
    async def run():
        from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

        fixture = OAuthFixture()
        await fixture.start()
        coordinator = None
        try:
            service, coordinator = _service(fixture, tmp_path=tmp_path)
            owner = _owner()
            preview = await coordinator.preview("remote", owner)
            started = await coordinator.start(
                "remote", owner, preview_id=preview["previewId"], digest=preview["digest"]
            )
            operation_id = started["operation"]["operationId"]
            await _wait_state(coordinator, "remote", owner, operation_id, "awaiting_browser")
            url = await coordinator.authorization_url(operation_id, owner)
            async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as client:
                response = await client.get(url)
                callback = urlsplit(response.headers["location"])
                query = urlencode(
                    [(key, value) for key, value in parse_qsl(callback.query) if key != "iss"]
                )
                missing_issuer = urlunsplit(callback._replace(query=query))
                rejected = await client.get(missing_issuer)
                assert "Authorization request invalid" in rejected.text
            await asyncio.sleep(0.05)
            assert fixture.counters["exchange"] == 0
            status = await coordinator.status("remote", owner, operation_id=operation_id)
            assert status["operation"]["state"] == "awaiting_browser"
            assert (await coordinator.status("remote", owner))["auth"]["state"] == "signed_out"
            async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as client:
                await client.get(response.headers["location"])
            final = await _wait_state(coordinator, "remote", owner, operation_id, "complete")
            assert final["operation"]["state"] == "complete"
            assert fixture.counters["exchange"] == 1
        finally:
            if coordinator is not None:
                await coordinator.shutdown()
            await fixture.stop()

    asyncio.run(run())


def test_metadata_document_requires_server_advertisement(tmp_path):
    async def run():
        from machina.connectors.oauth import OAuthError

        fixture = OAuthFixture()
        await fixture.start()
        coordinator = None
        try:
            service, coordinator = _service(
                fixture,
                tmp_path=tmp_path,
                oauth_settings={
                    "client_mode": "metadata_document",
                    "client_metadata_url": "https://client.example.com/client.json",
                },
            )
            with pytest.raises(OAuthError) as exc:
                await coordinator.preview("remote", _owner())
            assert exc.value.reason == "oauth_client_configuration_required"
            assert fixture.counters["register"] == 0
            assert not coordinator._previews
        finally:
            if coordinator is not None:
                await coordinator.shutdown()
            await fixture.stop()

    asyncio.run(run())


def test_reauthorization_after_provider_edit_selects_current_grant(tmp_path):
    async def run():
        from machina.connectors.oauth_store import credential_key_for

        old_provider = OAuthFixture()
        new_provider = OAuthFixture()
        await old_provider.start()
        await new_provider.start()
        coordinator = None
        try:
            old_client = "old-client"
            old_key = credential_key_for(
                connector_id="remote",
                resource=old_provider.endpoint,
                issuer=old_provider.issuer,
                client_id=old_client,
            )
            for attempt in range(1000):
                new_client = f"new-client-{attempt}"
                new_key = credential_key_for(
                    connector_id="remote",
                    resource=new_provider.endpoint,
                    issuer=new_provider.issuer,
                    client_id=new_client,
                )
                if old_key < new_key:
                    break
            else:
                pytest.fail("could not arrange stale grant first in canonical index")
            service, coordinator = _service(
                old_provider,
                tmp_path=tmp_path,
                oauth_settings={"client_mode": "preregistered", "client_id": old_client},
            )
            owner = _owner()

            async def login(provider, client_id):
                preview = await coordinator.preview("remote", owner)
                provider.preregister(client_id, redirect_uri=preview["review"]["redirectUri"])
                started = await coordinator.start(
                    "remote", owner, preview_id=preview["previewId"], digest=preview["digest"]
                )
                operation_id = started["operation"]["operationId"]
                await _wait_state(coordinator, "remote", owner, operation_id, "awaiting_browser")
                await _drive_browser(coordinator, owner, operation_id)
                final = await _wait_state(coordinator, "remote", owner, operation_id, "complete")
                assert final["operation"]["state"] == "complete"

            await login(old_provider, old_client)
            service.registry.update(
                "remote",
                endpoint=new_provider.endpoint,
                oauth={"client_mode": "preregistered", "client_id": new_client},
            )
            await login(new_provider, new_client)
            await coordinator.refresh_auth_cache("remote")
            identity = coordinator.auth_identity("remote")
            assert identity is not None, (
                "an earlier stale grant must not hide newly authorized credentials"
            )
            assert identity["credentialKey"] == new_key
            assert identity["resource"] == new_provider.endpoint
            status = await coordinator.status("remote", owner)
            assert status["auth"]["state"] == "authorized"
            assert status["auth"]["issuer"] == new_provider.issuer
        finally:
            if coordinator is not None:
                await coordinator.shutdown()
            await old_provider.stop()
            await new_provider.stop()

    asyncio.run(run())


def test_initial_exchange_preserves_explicit_empty_granted_scope(tmp_path):
    async def run():
        from dataclasses import replace

        from machina.connectors.oauth_models import OAuthBinding
        from machina.connectors.oauth_network import OAuthNetworkClient

        class ReadOnlyNetwork(OAuthNetworkClient):
            async def discover(self, *args, **kwargs):
                discovered = await super().discover(*args, **kwargs)
                return replace(discovered, scopes=("read",))

        fixture = OAuthFixture()
        await fixture.start()
        coordinator = None
        try:
            service, coordinator = _service(fixture, tmp_path=tmp_path)
            coordinator._network_factory = lambda *, local_access: ReadOnlyNetwork(
                local_access=local_access
            )
            owner = _owner()
            preview = await coordinator.preview("remote", owner)
            assert preview["review"]["requestedScopes"] == ["read"]
            started = await coordinator.start(
                "remote", owner, preview_id=preview["previewId"], digest=preview["digest"]
            )
            operation_id = started["operation"]["operationId"]
            await _wait_state(coordinator, "remote", owner, operation_id, "awaiting_browser")
            url = await coordinator.authorization_url(operation_id, owner)
            async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as client:
                response = await client.get(url)
                assert response.status_code == 302
                assert len(fixture._codes) == 1
                code = next(iter(fixture._codes.values()))
                assert code["scope"] == "read"
                # The provider reduces its actual token grant to zero scopes.
                # Its real token endpoint emits the explicit scope="" field.
                code["scope"] = ""
                await client.get(response.headers["location"])
            final = await _wait_state(coordinator, "remote", owner, operation_id, "complete")
            assert final["operation"]["state"] == "complete"
            assert final["auth"]["grantedScopes"] == []
            await coordinator.refresh_auth_cache("remote")
            identity = coordinator.auth_identity("remote")
            assert identity["grantedScopes"] == []
            binding = OAuthBinding(
                connector_id="remote",
                credential_key=identity["credentialKey"],
                resource=identity["resource"],
                issuer=identity["issuer"],
                client_id=identity["clientId"],
            )
            record = await coordinator._creds.load(binding)
            assert record.approved_scopes == ("read",)
            assert record.granted_scopes == ()
        finally:
            if coordinator is not None:
                await coordinator.shutdown()
            await fixture.stop()

    asyncio.run(run())
