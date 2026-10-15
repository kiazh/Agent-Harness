"""R4-085 hardening: reachable oauth guard, typed ports, off-loop index reads."""

import asyncio
import threading
import time

import pytest

from machina.connectors.models import ConfigError, ConnectorConfig
from machina.connectors.oauth import OAuthCoordinator, OAuthError, OAuthOwner
from machina.connectors.oauth_store import OAuthCredentialStore
from machina.connectors.store import ConnectorStore


def _http_config(**fields):
    base = {
        "id": "web",
        "transport": "streamable_http",
        "endpoint": "https://mcp.example.com/mcp",
    }
    base.update(fields)
    return ConnectorConfig(**base)


def _coordinator(config, tmp_path):
    return OAuthCoordinator(
        config_resolver=lambda _: config,
        credential_store=OAuthCredentialStore(tmp_path / "oauth", key_provider=lambda: b"k" * 32),
    )


def _empty_index():
    return {"version": 1, "grants": {}, "tombstones": {}}


# ── (a) the "oauth is not configured" guard is reachable ────────────────────


def test_preview_refuses_connector_without_oauth_settings(tmp_path):
    """No oauth settings -> typed refusal before any discovery traffic.

    The call sites used to coerce ``None`` to ``{}``, so ``from_dict`` never
    returned None and preview() attempted discovery against the MCP endpoint
    instead of refusing with ``oauth_not_configured``.
    """
    config = _http_config()  # oauth=None
    coordinator = _coordinator(config, tmp_path)

    def forbidden_network(**_kwargs):
        pytest.fail("oauth discovery must not run for a connector without oauth settings")

    coordinator._network_factory = forbidden_network

    async def run():
        with pytest.raises(OAuthError) as exc:
            await coordinator.preview("web", OAuthOwner(session_id="s", agent_id="a"))
        assert exc.value.reason == "oauth_not_configured"
        status = await coordinator.status("web", OAuthOwner(session_id="s", agent_id="a"))
        assert status["auth"] == {"state": "not_configured", "generation": 0}
        await coordinator.refresh_auth_cache("web")
        assert coordinator.auth_identity("web") is None

    asyncio.run(run())


def test_removed_oauth_settings_drop_cached_authority(tmp_path):
    """A grant must not surface once the connector has no oauth settings."""
    from tests.test_connectors_oauth_refresh import _seed_refresh

    async def run():
        coordinator, config, binding, identity = await _seed_refresh(tmp_path)
        assert coordinator.auth_identity("web") is not None
        config.oauth = None
        await coordinator.refresh_auth_cache("web", config=config)
        assert coordinator.auth_identity("web") is None
        assert (await coordinator._public_auth(config))["state"] == "not_configured"

    asyncio.run(run())


# ── (b) invalid ports rejected at validation time, preview stays typed ──────


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://mcp.example.com:99999/mcp",  # out of range
        "https://mcp.example.com:abc/mcp",  # non-numeric
        "https://mcp.example.com:-1/mcp",  # negative
        "http://[::1/mcp",  # malformed netloc (urlparse ValueError)
    ],
)
def test_invalid_endpoint_ports_rejected_at_save(endpoint, tmp_path):
    store = ConnectorStore(tmp_path / "mcp.json")
    fields = {
        "id": "web",
        "transport": "streamable_http",
        "endpoint": endpoint,
        "oauth": {"client_mode": "auto"},
    }
    with pytest.raises(ConfigError):
        store.validate_input(dict(fields))
    with pytest.raises(ConfigError):
        store.configure(dict(fields))
    assert not store.path.exists(), "rejected config must never reach disk"


def test_preview_with_unvalidated_endpoint_never_raises_raw_valueerror(tmp_path):
    """Defence in depth: an in-memory config that bypassed validate() still
    refuses with a typed OAuth error, not the deferred urlparse ValueError."""
    config = _http_config(
        endpoint="https://mcp.example.com:99999/mcp", oauth={"client_mode": "auto"}
    )
    coordinator = _coordinator(config, tmp_path)

    async def run():
        with pytest.raises(OAuthError) as exc:
            await coordinator.preview("web", OAuthOwner(session_id="s", agent_id="a"))
        assert exc.value.reason == "oauth_discovery_failed"

    asyncio.run(run())


# ── (e) index reads run off the event loop ──────────────────────────────────


def test_index_reads_run_off_the_event_loop(tmp_path):
    """A slow synchronous index read must not stall the loop (loop-lag proof)."""
    config = _http_config(oauth={"client_mode": "auto"})
    coordinator = _coordinator(config, tmp_path)
    threads: list[threading.Thread] = []

    def slow_read():
        threads.append(threading.current_thread())
        time.sleep(0.2)  # simulate blocking file I/O on the store
        return _empty_index()

    coordinator._creds._read_index = slow_read

    async def run():
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        task = asyncio.create_task(ticker())
        try:
            await coordinator._public_auth(config)  # index scan branch
            await coordinator._primary_binding(config)
        finally:
            task.cancel()
        return ticks

    ticks = asyncio.run(run())
    assert len(threads) == 2, "both call sites must read the index"
    assert all(thread is not threading.main_thread() for thread in threads)
    assert ticks >= 10, f"event loop stalled during index reads ({ticks} ticks)"
