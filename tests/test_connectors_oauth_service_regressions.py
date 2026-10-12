"""Exercise the durable service wiring used by the runtime, not a resolver fake."""

import asyncio

from machina.connectors.oauth import OAuthCoordinator
from machina.connectors.oauth_store import OAuthCredentialStore
from machina.connectors.service import ConnectorService
from machina.connectors.store import ConnectorStore


async def test_durable_inventory_with_oauth_coordinator_does_not_reenter_refresh(tmp_path):
    coordinator = OAuthCoordinator(
        credential_store=OAuthCredentialStore(tmp_path / "oauth", key_provider=lambda: b"k" * 32)
    )
    store = ConnectorStore(tmp_path / "mcp.json")
    service = ConnectorService(store=store, oauth_coordinator=coordinator)
    try:
        configured = await asyncio.wait_for(
            service.configure({"id": "local", "command": "python3"}), timeout=1
        )
        assert configured["id"] == "local"
        assert (await asyncio.wait_for(service.list(), timeout=1))[0]["id"] == "local"
        restarted = ConnectorService(
            store=store, oauth_coordinator=OAuthCoordinator(credential_store=coordinator._creds)
        )
        assert (await asyncio.wait_for(restarted.list(), timeout=1))[0]["id"] == "local"
        assert await asyncio.wait_for(service.remove("local"), timeout=1)
        assert await service.list() == []
    finally:
        await coordinator.shutdown()
