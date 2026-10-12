"""Task 1: strict OAuth settings and backward-compatible connector config."""

import pytest

from machina.connectors.models import ConfigError, ConnectorConfig
from machina.connectors.oauth_models import OAuthSettings


def _http(**fields):
    base = {
        "id": "web",
        "transport": "streamable_http",
        "endpoint": "https://mcp.example.com/mcp",
    }
    base.update(fields)
    return ConnectorConfig(**base)


def test_oauth_settings_rejects_raw_secrets_and_unknown_fields():
    for bad in (
        {"access_token": "abc"},
        {"client_secret": "abc"},
        {"refresh_token": "abc"},
        {"unknown_key": 1},
        {"code_verifier": "x"},
    ):
        with pytest.raises(ConfigError):
            _http(oauth=bad).validate()
    # OAuth on stdio and oauth+credential_profile both refuse before disk.
    with pytest.raises(ConfigError):
        ConnectorConfig(id="s", command="/bin/true", oauth={"client_mode": "auto"}).validate()
    with pytest.raises(ConfigError):
        _http(credential_profile="prof", oauth={"client_mode": "auto"}).validate()


@pytest.mark.parametrize(
    "oauth",
    [
        {"callback_port": True},
        {"callback_port": -1},
        {"callback_port": 80},
        {"client_mode": "magic"},
        {"client_id": "x" * 2049},
        {"client_mode": "preregistered"},
        {
            "client_mode": "preregistered",
            "client_id": "id",
            "token_endpoint_auth_method": "client_secret_basic",
        },
        {"client_mode": "auto", "client_secret_ref": "TOKEN"},
        {"client_mode": "metadata_document"},
        {"client_metadata_url": "https://auth.example.com"},
        {"client_metadata_url": "http://auth.example.com/doc"},
        {"scope_allowlist": ["ok"] * 65},
        {"scope_allowlist": ["not a scope!"]},
        {"issuer": "http://auth.example.com/tenant"},
        {"token_endpoint_auth_method": "s256"},
    ],
)
def test_invalid_oauth_combinations_rejected(oauth):
    with pytest.raises(ConfigError):
        _http(oauth=oauth).validate()


def test_valid_none_dcr_preregistration_metadata_document_cases():
    _http().validate()  # legacy: no oauth key at all
    assert _http(oauth=None).validate().oauth is None
    _http(oauth={"client_mode": "auto"}).validate()
    _http(oauth={"client_mode": "preregistered", "client_id": "public-id"}).validate()
    _http(
        oauth={
            "client_mode": "preregistered",
            "client_id": "confidential",
            "client_secret_ref": "MCP_CLIENT_SECRET",
            "token_endpoint_auth_method": "client_secret_basic",
        }
    ).validate()
    _http(
        oauth={
            "client_mode": "metadata_document",
            "client_id": "https://client.example.com/metadata",
            "client_metadata_url": "https://client.example.com/metadata",
        }
    ).validate()


def test_legacy_config_roundtrip_and_revision_identity():
    from dataclasses import asdict

    legacy = _http()
    assert legacy.oauth is None
    changed = _http(oauth={"client_mode": "auto"})
    assert asdict(legacy) != asdict(changed)
    view = changed.public_view()
    assert view["oauth"] == {
        "client_mode": "auto",
        "client_id": "",
        "client_metadata_url": "",
        "client_secret_ref": "",
        "token_endpoint_auth_method": "none",
        "issuer": "",
        "scope_allowlist": [],
        "callback_port": 0,
        "callback_path": "/oauth/callback",
    }
    sentinel = "supersecret-token-value"
    oauth_view = _http(oauth={"client_mode": "preregistered", "client_id": "id"}).public_view()
    assert sentinel not in str(oauth_view)
    assert OAuthSettings.from_dict({"client_mode": "auto"}) is not None


def test_partial_oauth_update_merges_and_bumps_revision(tmp_path):
    import asyncio

    from machina.connectors.service import ConnectorService
    from machina.connectors.store import ConnectorStore

    async def _run():
        store = ConnectorStore(tmp_path / "mcp.json")
        service = ConnectorService(store=store)
        await service.configure(
            {"id": "web", "transport": "streamable_http", "endpoint": "https://mcp.example.com/mcp"}
        )
        first = service.registry.get("web")
        assert first is not None and first.oauth is None
        updated = await service.configure({"id": "web", "oauth": {"client_mode": "auto"}})
        assert updated["revision"] == first.revision + 1
        assert updated["oauth"]["client_mode"] == "auto"
        assert updated["endpoint"] == "https://mcp.example.com/mcp"
        # Full configs without transport still work; unknown partials refuse.
        import pytest as _pytest

        with _pytest.raises(ConfigError):
            await service.configure({"id": "web", "trust": "trusted_read"})
        with _pytest.raises(ConfigError):
            await service.configure({"id": "missing", "oauth": {"client_mode": "auto"}})

    asyncio.run(_run())


def test_preregistered_callback_path_roundtrips():
    settings = OAuthSettings.from_dict(
        {
            "client_mode": "preregistered",
            "client_id": "registered",
            "callback_path": "/provider/callback",
            "callback_port": 12345,
        }
    )
    assert settings.callback_path == "/provider/callback"
    assert settings.to_dict()["callback_path"] == "/provider/callback"
    assert (
        OAuthSettings.from_dict(
            {"client_mode": "preregistered", "client_id": "registered"}
        ).callback_path
        == "/oauth/callback"
    )


@pytest.mark.parametrize(
    "path",
    [
        "",
        "relative",
        "//evil/callback",
        "/a?x=1",
        "/a#frag",
        "/a\r\n",
        "/a%2fb",
        "/a/../b",
        "/a\\b",
        3,
    ],
)
def test_unsafe_callback_path_rejected(path):
    with pytest.raises(ValueError):
        OAuthSettings.from_dict(
            {"client_mode": "preregistered", "client_id": "registered", "callback_path": path}
        )
