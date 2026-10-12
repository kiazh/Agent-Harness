"""Task 6: bounded refresh, rotation, cross-process concurrency, crash safety."""

import asyncio
import multiprocessing

import pytest

from ah.connectors.models import ConnectorConfig
from ah.connectors.oauth import OAuthCoordinator, OAuthError
from ah.connectors.oauth_models import OAuthBinding
from ah.connectors.oauth_store import OAuthCredentialStore
from ah.connectors.registry import ConnectorRegistry
from ah.connectors.service import ConnectorService
from tests.support.mcp_oauth_fixture import OAuthFixture
from tests.test_connectors_oauth_flow import _full_login, _owner


def _service(fixture, *, tmp_path, root=None):
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
    creds = OAuthCredentialStore((root or tmp_path / "oauth"), key_provider=lambda: b"k" * 32)
    coordinator = OAuthCoordinator(credential_store=creds)
    service = ConnectorService(registry=registry, oauth_coordinator=coordinator)
    coordinator._service = service
    return service, coordinator


def test_proactive_refresh_and_valid_token_needs_no_post(tmp_path):
    async def _run():
        fixture = OAuthFixture()
        await fixture.start()
        try:
            service, coordinator = _service(fixture, tmp_path=tmp_path)
            owner = _owner()
            await _full_login(fixture, coordinator, owner)
            await coordinator.refresh_auth_cache("remote")
            identity = coordinator.auth_identity("remote")
            assert identity is not None
            binding = OAuthBinding(
                connector_id="remote",
                credential_key=identity["credentialKey"],
                resource=identity["resource"],
                issuer=identity["issuer"],
                client_id=identity["clientId"],
            )
            loop = asyncio.get_running_loop()
            # Valid token: no refresh POST.
            token = await coordinator.ensure_token(
                binding, expected_grant=identity, deadline=loop.time() + 10
            )
            assert token.startswith("access-")
            assert fixture.counters["refresh"] == 0
            # Force expiry: proactive refresh rotates exactly once.
            record = await coordinator._creds.load(binding)
            assert record is not None
            record.expires_at_utc = "2020-01-01T00:00:00+00:00"
            await coordinator._creds.save(record, expected_generation=record.generation)
            token2 = await coordinator.ensure_token(
                binding, expected_grant=identity, deadline=loop.time() + 10
            )
            assert token2.startswith("access-") and token2 != token
            assert fixture.counters["refresh"] == 1
            # Rotation preserved grant identity and generation.
            await coordinator.refresh_auth_cache("remote")
            identity2 = coordinator.auth_identity("remote")
            assert identity2 is not None
            assert identity2["grantId"] == identity["grantId"]
            assert identity2["generation"] == identity["generation"]
            # N concurrent same-loop refreshes collapse to one exchange.
            record3 = await coordinator._creds.load(binding)
            record3.expires_at_utc = "2020-01-01T00:00:00+00:00"
            await coordinator._creds.save(record3, expected_generation=record3.generation)
            before = fixture.counters["refresh"]
            tokens = await asyncio.gather(
                *[
                    coordinator.ensure_token(
                        binding,
                        expected_grant=identity2,
                        deadline=asyncio.get_running_loop().time() + 20,
                    )
                    for _ in range(6)
                ]
            )
            assert fixture.counters["refresh"] == before + 1
            assert len(set(tokens)) == 1
            await coordinator.shutdown()
        finally:
            await fixture.stop()

    asyncio.run(_run())


def _worker(root: str, endpoint: str, issuer: str, queue):
    async def _run():
        from pathlib import Path as _Path

        from ah.connectors.oauth import OAuthCoordinator as _Coord
        from ah.connectors.oauth_models import OAuthBinding as _Binding
        from ah.connectors.oauth_store import OAuthCredentialStore as _Store

        creds = _Store(_Path(root), key_provider=lambda: b"k" * 32)
        coordinator = _Coord(credential_store=creds)
        # Find the binding from the shared index.
        index = creds._read_index()
        key = next(iter(index["grants"]))
        record = await creds.load(
            _Binding(
                connector_id="remote", credential_key=key, resource="", issuer="", client_id=""
            )
        )
        binding = _Binding(
            connector_id="remote",
            credential_key=key,
            resource=record.resource,
            issuer=record.issuer,
            client_id=record.client_id,
        )
        identity = {"grantId": record.grant_id, "generation": record.generation}
        try:
            token = await coordinator.ensure_token(
                binding, expected_grant=identity, deadline=asyncio.get_running_loop().time() + 20
            )
            queue.put(("ok", token))
        except Exception as exc:  # noqa: BLE001
            queue.put(("error", f"{type(exc).__name__}: {exc}"))

    asyncio.run(_run())


def test_cross_process_refresh_performs_one_exchange(tmp_path):
    async def _run():
        fixture = OAuthFixture()
        await fixture.start()
        try:
            root = tmp_path / "oauth"
            service, coordinator = _service(fixture, tmp_path=tmp_path, root=root)
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
            base_before = fixture.counters["refresh"]
            await coordinator.shutdown()
            results = await asyncio.to_thread(
                _run_workers, str(root), fixture.endpoint, record.issuer
            )
            tokens = [token for status, token in results if status == "ok"]
            assert results, results
            # Exactly one exchange across all processes.
            assert fixture.counters["refresh"] == base_before + 1, (fixture.counters, results)
            assert len(set(tokens)) == 1, results
        finally:
            await coordinator.shutdown()
            await fixture.stop()

    def _run_workers(root, endpoint, issuer):
        ctx = multiprocessing.get_context("spawn")
        queue = ctx.Queue()
        workers = [
            ctx.Process(target=_worker, args=(root, endpoint, issuer, queue)) for _ in range(4)
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=90)
            assert worker.exitcode == 0, worker.exitcode
        return [queue.get(timeout=10) for _ in workers]

    asyncio.run(_run())


def test_crash_after_in_flight_forces_reauthorization(tmp_path):
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
            # Disposable worker journals in_flight then dies before commit.
            record2 = await coordinator._creds.load(binding)
            record2.refresh_state = "in_flight"
            await coordinator._creds.save(record2, expected_generation=record2.generation)
            # Restarted worker must not re-send the rotating token.
            before = fixture.counters["refresh"]
            loop = asyncio.get_running_loop()
            with pytest.raises(OAuthError):
                await coordinator.ensure_token(
                    binding, expected_grant=identity, deadline=loop.time() + 10
                )
            assert fixture.counters["refresh"] == before
            await coordinator.shutdown()
        finally:
            await fixture.stop()

    asyncio.run(_run())


def test_invalid_grant_clears_eligibility_without_resource_dispatch(tmp_path):
    async def _run():
        fixture = OAuthFixture()
        fixture.switches["invalid_grant_refresh"] = True
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
            loop = asyncio.get_running_loop()
            with pytest.raises(OAuthError) as excinfo:
                await coordinator.ensure_token(
                    binding, expected_grant=identity, deadline=loop.time() + 10
                )
            assert excinfo.value.reason in (
                "oauth_reauthorization_required",
                "oauth_refresh_failed",
            )
            assert fixture.counters["tool_calls"] == 0
            await coordinator.shutdown()
        finally:
            await fixture.stop()

    asyncio.run(_run())
