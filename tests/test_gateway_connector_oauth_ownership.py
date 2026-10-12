"""OAuth routes must authorize against the refreshed connector inventory."""

from dataclasses import asdict
from types import SimpleNamespace

import pytest

from ah.connectors.models import ConnectorConfig
from ah.connectors.oauth import OAuthCoordinator, OAuthOwner
from ah.connectors.oauth_store import OAuthCredentialStore
from ah.connectors.registry import ConnectorRegistry
from ah.connectors.service import ConnectorService
from ah.connectors.store import ConnectorStore
from ah.gateway.errors import NOT_FOUND, UNAUTHORIZED, RpcError
from ah.gateway.features import connector_oauth

ROUTES = (
    (connector_oauth.mcp_oauth_preview, {}),
    (connector_oauth.mcp_oauth_start, {"previewId": "preview", "digest": "digest"}),
    (connector_oauth.mcp_oauth_status, {}),
    (connector_oauth.mcp_oauth_url, {"operationId": "operation"}),
    (connector_oauth.mcp_oauth_open, {"operationId": "operation"}),
    (connector_oauth.mcp_oauth_cancel, {"operationId": "operation"}),
    (connector_oauth.mcp_oauth_forget, {"expectedGeneration": 0}),
)


class Coordinator:
    def __init__(self):
        self.effects = []

    async def refresh_auth_cache(self, _connector_id, **_kwargs):
        pass

    async def invalidate_connector(self, _connector_id):
        pass

    def __getattr__(self, name):
        async def operation(*args, **kwargs):
            self.effects.append(name)
            return {} if name not in ("open_browser", "cancel") else True

        return operation


def gateway(service):
    async def get_session(_params):
        return SimpleNamespace(id="session", agent_id="actor")

    return SimpleNamespace(
        get_session=get_session,
        runtime=SimpleNamespace(get_connector_service=lambda: service),
    )


@pytest.mark.parametrize("route,extra", ROUTES)
@pytest.mark.parametrize("current_owner", ["another-agent", None])
async def test_oauth_routes_refuse_foreign_or_removed_current_config(
    route, extra, current_owner, tmp_path
):
    stale = ConnectorConfig(
        id="remote",
        transport="streamable_http",
        endpoint="https://example.com/mcp",
        owner="actor",
        oauth={"client_mode": "auto"},
    )
    registry = ConnectorRegistry()
    registry.add(stale)
    store = ConnectorStore(tmp_path / "mcp.json")
    current = ConnectorConfig(**{**asdict(stale), "owner": current_owner})
    store._write(1, [current] if current_owner else [], {"remote": 0})
    coordinator = Coordinator()
    service = ConnectorService(registry=registry, store=store, oauth_coordinator=coordinator)

    with pytest.raises(RpcError) as denied:
        await route(gateway(service), {"id": "remote", "sessionId": "session", **extra})

    assert denied.value.code == (UNAUTHORIZED if current_owner else NOT_FOUND)
    assert coordinator.effects == []


@pytest.mark.parametrize("route,extra", ROUTES)
async def test_oauth_routes_allow_the_current_owner(route, extra, tmp_path):
    store = ConnectorStore(tmp_path / "mcp.json")
    config = ConnectorConfig(
        id="remote",
        transport="streamable_http",
        endpoint="https://example.com/mcp",
        owner="actor",
        oauth={"client_mode": "auto"},
    )
    store._write(1, [config], {"remote": 0})
    service = ConnectorService(store=store, oauth_coordinator=Coordinator())
    result = await route(gateway(service), {"id": "remote", "sessionId": "session", **extra})
    assert isinstance(result, dict)


@pytest.mark.parametrize(
    "route,extra",
    (
        (connector_oauth.mcp_oauth_url, {"operationId": "operation"}),
        (connector_oauth.mcp_oauth_open, {"operationId": "operation"}),
        (connector_oauth.mcp_oauth_cancel, {"operationId": "operation"}),
        (connector_oauth.mcp_oauth_cancel, {"previewId": "preview"}),
    ),
)
async def test_oauth_handle_cannot_be_used_under_a_different_connector_id(route, extra, tmp_path):
    registry = ConnectorRegistry()
    for connector_id in ("actual", "remote"):
        registry.add(
            ConnectorConfig(
                id=connector_id,
                transport="streamable_http",
                endpoint="https://example.com/mcp",
                owner="actor",
                oauth={"client_mode": "auto"},
            )
        )
    opened = []
    coordinator = OAuthCoordinator(
        credential_store=OAuthCredentialStore(tmp_path / "oauth", key_provider=lambda: b"k" * 32),
        browser_opener=lambda url: opened.append(url) or True,
    )
    service = ConnectorService(registry=registry, oauth_coordinator=coordinator)
    handle = {
        "connector_id": "actual",
        "owner": OAuthOwner(session_id="session", agent_id="actor"),
        "snapshot": SimpleNamespace(config_revision=0),
        "state": "awaiting_browser",
        "authorization_url": "https://auth.example.com/authorize?state=transient",
    }

    class Callback:
        closed = False

        async def aclose(self):
            self.closed = True

    callback = Callback()
    coordinator._operations["operation"] = handle
    coordinator._previews["preview"] = {**handle, "callback": callback}
    try:
        with pytest.raises(RpcError) as denied:
            await route(gateway(service), {"id": "remote", "sessionId": "session", **extra})
        assert denied.value.code == NOT_FOUND
        assert opened == []
        assert handle["state"] == "awaiting_browser"
        assert callback.closed is False
    finally:
        await coordinator.shutdown()
