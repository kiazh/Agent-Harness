"""Task 2: encrypted grant store, key source, generation/tombstone rules."""

import asyncio
import base64
import json
import os
import stat

import pytest

from ah.connectors.oauth_models import OAuthBinding
from ah.connectors.oauth_store import (
    OAuthCredentialRecord,
    OAuthCredentialStore,
    credential_key_for,
)
from ah.security import oauth_keys
from ah.security.oauth_keys import OAuthKeyUnavailable

SECRET = "test-access-token-secret-value"
REFRESH = "test-refresh-token-secret-value"


def _key_provider():
    return b"k" * 32


def _binding(**overrides):
    key = credential_key_for(
        connector_id="web",
        resource="https://mcp.example.com/mcp",
        issuer="https://auth.example.com",
        client_id="client-1",
    )
    base = {
        "connector_id": "web",
        "credential_key": key,
        "resource": "https://mcp.example.com/mcp",
        "issuer": "https://auth.example.com",
        "client_id": "client-1",
    }
    base.update(overrides)
    return OAuthBinding(**base)


def _record(binding, **overrides):
    base = {
        "version": 1,
        "credential_key": binding.credential_key,
        "connector_id": "web",
        "config_revision": 1,
        "resource": binding.resource,
        "issuer": binding.issuer,
        "client_id": "client-1",
        "token_endpoint": "https://auth.example.com/token",
        "authorization_endpoint": "https://auth.example.com/authorize",
        "grant_id": "grant-1",
        "generation": 1,
        "approved_scopes": ("read",),
        "granted_scopes": ("read",),
        "access_token": SECRET,
        "refresh_token": REFRESH,
        "acquired_at_utc": "2026-01-01T00:00:00+00:00",
        "expires_at_utc": "",
        "refresh_state": "idle",
    }
    base.update(overrides)
    return OAuthCredentialRecord(**base)


def test_encrypted_grant_survives_restart_and_contains_no_secret_bytes(tmp_path):
    async def _run():
        store = OAuthCredentialStore(tmp_path / "oauth", key_provider=_key_provider)
        binding = _binding()
        await store.save(_record(binding), expected_generation=0)
        identity = await store.public_identity(binding)
        assert identity is not None and identity["grantId"] == "grant-1"
        # Reopen (restart): recover tokens.
        reopened = OAuthCredentialStore(tmp_path / "oauth", key_provider=_key_provider)
        loaded = await reopened.load(binding)
        assert loaded is not None and loaded.access_token == SECRET
        assert loaded.refresh_token == REFRESH
        return identity

    identity = asyncio.run(_run())
    assert SECRET not in json.dumps(identity) and REFRESH not in json.dumps(identity)
    for path in (tmp_path / "oauth").rglob("*"):
        if path.is_file():
            assert SECRET.encode() not in path.read_bytes()
            assert REFRESH.encode() not in path.read_bytes()
    assert "grant-1" in json.dumps(identity)


def test_tamper_wrong_key_wrong_aad_rejected(tmp_path):
    async def _run():
        store = OAuthCredentialStore(tmp_path / "oauth", key_provider=_key_provider)
        binding = _binding()
        await store.save(_record(binding), expected_generation=0)
        # Tamper with envelope bytes.
        index = json.loads((tmp_path / "oauth" / "index.json").read_text())
        envelope = index["grants"][binding.credential_key]["envelope"]
        envelope_path = tmp_path / "oauth" / "grants" / envelope
        raw = bytearray(envelope_path.read_bytes())
        raw[len(raw) // 2] ^= 1
        envelope_path.write_bytes(bytes(raw))
        with pytest.raises(ValueError):
            await store.load(binding)
        # Wrong key.
        wrong = OAuthCredentialStore(tmp_path / "oauth", key_provider=lambda: b"z" * 32)
        with pytest.raises(ValueError):
            await wrong.load(binding)
        # Wrong AAD (different credential key).
        other = _binding(connector_id="other")
        with pytest.raises(ValueError):
            await store.load(other)

    asyncio.run(_run())


def test_atomic_replacement_fault_and_file_security(tmp_path):
    async def _run():
        store = OAuthCredentialStore(tmp_path / "oauth", key_provider=_key_provider)
        binding = _binding()
        await store.save(_record(binding), expected_generation=0)
        # Simulate a crash between envelope write and index commit: orphan
        # envelope must not disturb the committed record.
        orphan = tmp_path / "oauth" / "grants" / f"{binding.credential_key}.orphan.enc"
        orphan.write_bytes(b"orphan")
        loaded = await store.load(binding)
        assert loaded is not None and loaded.access_token == SECRET
        # Symlink index refuses.
        index_path = tmp_path / "oauth" / "index.json"
        backup = index_path.read_bytes()
        index_path.unlink()
        index_path.symlink_to(tmp_path / "oauth" / "grants")
        with pytest.raises(ValueError):
            await store.load(binding)
        index_path.unlink()
        index_path.write_bytes(backup)
        # Hostile credential key rejected.
        with pytest.raises(ValueError):
            await store.load(_binding(credential_key="../../etc/passwd"))

    asyncio.run(_run())
    if os.name != "nt":
        mode = stat.S_IMODE((tmp_path / "oauth").stat().st_mode)
        assert mode == 0o700


def test_logout_tombstone_rejects_stale_writes_and_rollback(tmp_path):
    async def _run():
        store = OAuthCredentialStore(tmp_path / "oauth", key_provider=_key_provider)
        binding = _binding()
        await store.save(_record(binding), expected_generation=0)
        new_gen = await store.forget(binding, expected_generation=1)
        assert new_gen == 2
        assert await store.load(binding) is None
        # Late writer with stale generation cannot resurrect.
        with pytest.raises(ValueError):
            await store.save(_record(binding, generation=2), expected_generation=1)
        # Generation rollback fails closed.
        with pytest.raises(ValueError):
            await store.forget(binding, expected_generation=0)

    asyncio.run(_run())


def test_key_sources_and_no_plaintext_fallback(tmp_path, monkeypatch):
    # Missing backend and no env → storage_unavailable.
    monkeypatch.delenv("AGENT_HARNESS_OAUTH_MASTER_KEY", raising=False)
    monkeypatch.setenv("AGENT_HARNESS_SECRET_BACKEND", "")
    import sys

    monkeypatch.setitem(sys.modules, "keyring", None)
    with pytest.raises(OAuthKeyUnavailable):
        oauth_keys.get_master_key()
    # Malformed supplied key does not fall back.
    monkeypatch.setenv("AGENT_HARNESS_OAUTH_MASTER_KEY", "not-base64!!")
    with pytest.raises(OAuthKeyUnavailable):
        oauth_keys.get_master_key()
    monkeypatch.setenv("AGENT_HARNESS_OAUTH_MASTER_KEY", base64.b64encode(b"short").decode())
    with pytest.raises(OAuthKeyUnavailable):
        oauth_keys.get_master_key()
    # Valid supplied key resolves.
    monkeypatch.setenv("AGENT_HARNESS_OAUTH_MASTER_KEY", base64.b64encode(b"k" * 32).decode())
    assert oauth_keys.get_master_key() == b"k" * 32


def test_fake_keyring_backend_selection(tmp_path, monkeypatch):
    class _Backend:
        pass

    _Backend.__module__ = "keyring.backends.fail"

    class _Keyring:
        @staticmethod
        def get_keyring():
            return _Backend()

    import sys

    monkeypatch.setitem(sys.modules, "keyring", _Keyring)
    monkeypatch.delenv("AGENT_HARNESS_OAUTH_MASTER_KEY", raising=False)
    with pytest.raises(OAuthKeyUnavailable):
        oauth_keys.get_master_key()
