"""FM3-RF6-F2: preview listener reaping, cap, and reserve-failure mapping."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from machina.connectors.models import ConnectorConfig
from machina.connectors.oauth import OAuthCoordinator, OAuthError
from machina.connectors.oauth_models import (
    OAUTH_MAX_PREVIEWS,
    OAUTH_PREVIEW_TTL_SECONDS,
    DiscoveredOAuth,
    OAuthOwner,
)
from machina.connectors.oauth_store import OAuthCredentialStore


def _config() -> ConnectorConfig:
    return ConnectorConfig(
        id="web",
        transport="streamable_http",
        endpoint="https://mcp.example.com/mcp",
        local_access=True,
        oauth={"client_mode": "auto"},
    )


def _discovered() -> DiscoveredOAuth:
    return DiscoveredOAuth(
        resource="https://mcp.example.com/mcp",
        issuers=("https://auth.example.com",),
        selected_issuer="https://auth.example.com",
        authorization_endpoint="https://auth.example.com/authorize",
        token_endpoint="https://auth.example.com/token",
        registration_endpoint="https://auth.example.com/register",
        revocation_endpoint="",
        scopes=("read",),
        require_iss=False,
        supports_s256=True,
        fingerprint="f" * 64,
        supports_client_metadata_document=False,
    )


class _Network:
    async def discover(self, config, *, issuer, deadline):
        return _discovered()

    async def aclose(self):
        pass


class _Callback:
    def __init__(self):
        self.closed = False

    @property
    def redirect_uri(self):
        return "http://127.0.0.1:4567/oauth/callback/test"

    async def aclose(self):
        self.closed = True


def _coordinator(tmp_path, clock):
    created: list[_Callback] = []

    class _Factory:
        fail = False

        @classmethod
        async def reserve(cls, *, port, deadline, path=None):
            if cls.fail:
                raise OSError("callback port unavailable: 4567")
            callback = _Callback()
            created.append(callback)
            return callback

    coordinator = OAuthCoordinator(
        config_resolver=lambda connector_id: _config() if connector_id == "web" else None,
        credential_store=OAuthCredentialStore(tmp_path / "oauth", key_provider=lambda: b"k" * 32),
        network_factory=lambda *, local_access: _Network(),
        callback_factory=_Factory,
        now=lambda: clock["now"],
    )
    return coordinator, _Factory, created


def _clock() -> dict:
    return {"now": datetime(2026, 1, 1, 12, 0, tzinfo=UTC)}


def test_preview_reaps_expired_previews_and_closes_listeners(tmp_path):
    async def run():
        clock = _clock()
        coordinator, factory, created = _coordinator(tmp_path, clock)
        first = await coordinator.preview("web", OAuthOwner("s1", "a1"))
        first_callback = created[-1]
        clock["now"] += timedelta(seconds=OAUTH_PREVIEW_TTL_SECONDS + 1)
        # A different owner: the same-owner replacement path cannot hide the leak.
        await coordinator.preview("web", OAuthOwner("s2", "a2"))
        assert first_callback.closed is True
        assert first["previewId"] not in coordinator._previews
        assert len(coordinator._previews) == 1
        await coordinator.shutdown()

    asyncio.run(run())


def test_preview_count_is_capped(tmp_path):
    async def run():
        clock = _clock()
        coordinator, factory, created = _coordinator(tmp_path, clock)
        for index in range(OAUTH_MAX_PREVIEWS + 3):
            await coordinator.preview("web", OAuthOwner(f"s{index}", f"a{index}"))
        assert len(coordinator._previews) == OAUTH_MAX_PREVIEWS
        assert sum(1 for callback in created if callback.closed) == 3
        await coordinator.shutdown()

    asyncio.run(run())


def test_callback_reserve_failure_is_oauth_error(tmp_path):
    async def run():
        clock = _clock()
        coordinator, factory, created = _coordinator(tmp_path, clock)
        factory.fail = True
        with pytest.raises(OAuthError) as exc:
            await coordinator.preview("web", OAuthOwner("s1", "a1"))
        assert exc.value.reason == "oauth_callback_invalid"
        assert not coordinator._previews
        await coordinator.shutdown()

    asyncio.run(run())


def test_start_sweeps_all_expired_previews(tmp_path):
    async def run():
        clock = _clock()
        coordinator, factory, created = _coordinator(tmp_path, clock)
        first = await coordinator.preview("web", OAuthOwner("s1", "a1"))
        await coordinator.preview("web", OAuthOwner("s2", "a2"))
        callbacks = list(created)
        clock["now"] += timedelta(seconds=OAUTH_PREVIEW_TTL_SECONDS + 1)
        with pytest.raises(OAuthError) as exc:
            await coordinator.start(
                "web",
                OAuthOwner("s1", "a1"),
                preview_id=first["previewId"],
                digest=first["digest"],
            )
        assert exc.value.reason == "oauth_expired"
        assert all(callback.closed for callback in callbacks)
        assert coordinator._previews == {}
        await coordinator.shutdown()

    asyncio.run(run())
