"""Task 6: bounded refresh, rotation, cross-process concurrency, crash safety."""

import asyncio
import multiprocessing

import pytest

from machina.connectors.models import ConnectorConfig
from machina.connectors.oauth import OAuthCoordinator, OAuthError
from machina.connectors.oauth_models import OAuthBinding
from machina.connectors.oauth_store import OAuthCredentialStore
from machina.connectors.registry import ConnectorRegistry
from machina.connectors.service import ConnectorService
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

        from machina.connectors.oauth import OAuthCoordinator as _Coord
        from machina.connectors.oauth_models import OAuthBinding as _Binding
        from machina.connectors.oauth_store import OAuthCredentialStore as _Store

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
        config = ConnectorConfig(
            id="remote",
            transport="streamable_http",
            endpoint=endpoint,
            revision=record.config_revision,
            local_access=True,
            oauth={"client_mode": "auto", "issuer": issuer},
        )
        coordinator._config_resolver = lambda _: config
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


async def _seed_refresh(
    tmp_path, *, expires_at="2020-01-01T00:00:00+00:00", scopes=("read", "write")
):
    from tests.test_connectors_oauth_store import _binding, _record

    binding = _binding()
    store = OAuthCredentialStore(tmp_path / "oauth", key_provider=lambda: b"k" * 32)
    config = ConnectorConfig(
        id="web",
        transport="streamable_http",
        endpoint=binding.resource,
        revision=1,
        local_access=False,
        oauth={"client_mode": "auto"},
    )
    record = _record(
        binding, expires_at_utc=expires_at, approved_scopes=scopes, granted_scopes=scopes
    )
    await store.save(record, expected_generation=0)
    coordinator = OAuthCoordinator(config_resolver=lambda _: config, credential_store=store)
    await coordinator.refresh_auth_cache("web")
    return coordinator, config, binding, coordinator.auth_identity("web")


def test_refresh_journal_failure_never_sends_rotating_token(tmp_path, monkeypatch):
    async def run():
        coordinator, config, binding, identity = await _seed_refresh(tmp_path)

        async def fail_save(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(coordinator._creds, "save", fail_save)

        def forbidden_network(**kwargs):
            pytest.fail("refresh token dispatched without durable journal")

        coordinator._network_factory = forbidden_network
        with pytest.raises(OAuthError) as exc:
            await coordinator.ensure_token(
                binding, expected_grant=identity, deadline=asyncio.get_running_loop().time() + 2
            )
        assert exc.value.reason == "oauth_storage_unavailable"

    asyncio.run(run())


@pytest.mark.parametrize("change", ["revision", "resource", "issuer", "client"])
def test_valid_token_rejects_current_configuration_change(tmp_path, change):
    async def run():
        coordinator, config, binding, identity = await _seed_refresh(tmp_path, expires_at="")
        if change == "revision":
            config.revision = 2
        elif change == "resource":
            config.endpoint = "https://other.example.com/mcp"
        elif change == "issuer":
            config.oauth = {"client_mode": "auto", "issuer": "https://other.example.com"}
        else:
            config.oauth = {"client_mode": "preregistered", "client_id": "other-client"}
        with pytest.raises(OAuthError) as exc:
            await coordinator.ensure_token(
                binding, expected_grant=identity, deadline=asyncio.get_running_loop().time() + 2
            )
        assert exc.value.reason == "oauth_config_changed"
        await coordinator.refresh_auth_cache("web")
        assert coordinator.auth_identity("web") is None

    asyncio.run(run())


def test_refresh_scope_change_invalidates_prior_review(tmp_path):
    async def run():
        from types import SimpleNamespace

        coordinator, config, binding, identity = await _seed_refresh(tmp_path)

        class Network:
            async def refresh_token(self, record, *, deadline):
                return SimpleNamespace(
                    access_token="new-access",
                    refresh_token="new-refresh",
                    scope="read",
                    expires_in=3600,
                )

            async def aclose(self):
                pass

        coordinator._network_factory = lambda **kwargs: Network()
        with pytest.raises(OAuthError) as exc:
            await coordinator.ensure_token(
                binding, expected_grant=identity, deadline=asyncio.get_running_loop().time() + 2
            )
        assert exc.value.reason == "oauth_config_changed"
        await coordinator.refresh_auth_cache("web")
        new_identity = coordinator.auth_identity("web")
        assert new_identity["grantId"] != identity["grantId"]
        assert new_identity["generation"] == identity["generation"] + 1
        assert new_identity["grantedScopes"] == ["read"]
        with pytest.raises(OAuthError):
            await coordinator.ensure_token(
                binding, expected_grant=identity, deadline=asyncio.get_running_loop().time() + 2
            )

    asyncio.run(run())


def test_refresh_uses_configured_local_access_policy(tmp_path):
    async def run():
        from types import SimpleNamespace

        coordinator, config, binding, identity = await _seed_refresh(tmp_path)

        class Network:
            async def refresh_token(self, record, *, deadline):
                return SimpleNamespace(
                    access_token="new-access",
                    refresh_token="new-refresh",
                    scope="read write",
                    expires_in=3600,
                )

            async def aclose(self):
                pass

        def network_factory(*, local_access):
            assert local_access is False, "refresh must honor saved local access permission"
            return Network()

        coordinator._network_factory = network_factory
        assert (
            await coordinator.ensure_token(
                binding, expected_grant=identity, deadline=asyncio.get_running_loop().time() + 2
            )
            == "new-access"
        )

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["invalid_grant", "lost_response"])
def test_refresh_failure_clears_cached_authority_and_prevents_replay(tmp_path, failure):
    async def run():
        from machina.connectors.oauth_network import OAuthNetworkError

        coordinator, config, binding, identity = await _seed_refresh(tmp_path)

        class Network:
            async def refresh_token(self, record, *, deadline):
                raise OAuthNetworkError(failure, reason="oauth_refresh_failed")

            async def aclose(self):
                pass

        coordinator._network_factory = lambda **kwargs: Network()
        with pytest.raises(OAuthError):
            await coordinator.ensure_token(
                binding, expected_grant=identity, deadline=asyncio.get_running_loop().time() + 2
            )
        assert coordinator.auth_identity("web") is None
        await coordinator.refresh_auth_cache("web")
        assert coordinator.auth_identity("web") is None
        assert (await coordinator._public_auth(config))["state"] == "reauthorization_required"
        coordinator._network_factory = lambda **kwargs: pytest.fail(
            "ambiguous rotating refresh replayed"
        )
        with pytest.raises(OAuthError):
            await coordinator.ensure_token(
                binding, expected_grant=identity, deadline=asyncio.get_running_loop().time() + 2
            )

    asyncio.run(run())


def test_refresh_cache_accepts_known_config_without_resolving(tmp_path):
    async def run():
        coordinator, config, binding, identity = await _seed_refresh(tmp_path)
        coordinator._config_resolver = lambda _: pytest.fail(
            "known config must not resolve recursively"
        )
        await coordinator.refresh_auth_cache("web", config=config)
        assert coordinator.auth_identity("web") == identity

    asyncio.run(run())


def test_overlapping_refresh_waits_for_existing_journal_owner(tmp_path):
    async def run():
        from types import SimpleNamespace

        coordinator, config, binding, identity = await _seed_refresh(tmp_path)
        entered = asyncio.Event()
        release = asyncio.Event()

        class Network:
            async def refresh_token(self, record, *, deadline):
                entered.set()
                await release.wait()
                return SimpleNamespace(
                    access_token="new-access",
                    refresh_token="new-refresh",
                    scope="read write",
                    expires_in=3600,
                )

            async def aclose(self):
                pass

        coordinator._network_factory = lambda **kwargs: Network()

        async def get_token():
            return await coordinator.ensure_token(
                binding, expected_grant=identity, deadline=asyncio.get_running_loop().time() + 2
            )

        first = asyncio.create_task(get_token())
        second = None
        try:
            await entered.wait()
            second = asyncio.create_task(get_token())
            await asyncio.sleep(0.05)
            assert not second.done(), (
                "a live refresh must be awaited before interpreting its journal as a crash"
            )
            release.set()
            assert await asyncio.gather(first, second) == ["new-access", "new-access"]
        finally:
            release.set()
            await asyncio.gather(
                first, *([second] if second is not None else []), return_exceptions=True
            )

    asyncio.run(run())
