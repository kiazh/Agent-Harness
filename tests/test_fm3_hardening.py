"""FM3 hardening: secrets allowlist, grant-cache revocation bound, httpx timeouts."""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest


class _FakeGW:
    def require_db(self) -> None:
        return None


# ─── RF4-W6: secrets.set allowlist ─────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key",
    ["PATH", "LD_PRELOAD", "MACHINA_GATEWAY_TOKEN", "MACHINA_ENV_FILE", "PYTHONPATH"],
)
async def test_secrets_set_rejects_control_plane_keys(tmp_path, monkeypatch, key):
    from machina.gateway.errors import RpcError
    from machina.gateway.features.secrets import secrets_set

    monkeypatch.setenv("MACHINA_ENV_FILE", str(tmp_path / ".env"))
    monkeypatch.delenv(key, raising=False)
    with pytest.raises(RpcError, match="managed"):
        await secrets_set(_FakeGW(), {"key": key, "value": "x"})  # type: ignore[arg-type]
    assert key not in os.environ
    assert not (tmp_path / ".env").exists()


@pytest.mark.asyncio
async def test_secrets_set_allows_managed_keys(tmp_path, monkeypatch):
    from machina.gateway.features.secrets import secrets_set

    monkeypatch.setenv("MACHINA_ENV_FILE", str(tmp_path / ".env"))
    monkeypatch.delenv("MACHINA_MODEL", raising=False)
    result = await secrets_set(  # type: ignore[arg-type]
        _FakeGW(), {"key": "MACHINA_MODEL", "value": "openrouter/free", "persist": False}
    )
    assert result["set"] is True
    assert os.environ["MACHINA_MODEL"] == "openrouter/free"


@pytest.mark.asyncio
async def test_secrets_clear_rejects_control_plane_keys(tmp_path, monkeypatch):
    from machina.gateway.errors import RpcError
    from machina.gateway.features.secrets import secrets_clear

    monkeypatch.setenv("MACHINA_ENV_FILE", str(tmp_path / ".env"))
    monkeypatch.setenv("PATH", "/usr/bin")
    with pytest.raises(RpcError, match="managed"):
        await secrets_clear(_FakeGW(), {"key": "PATH"})  # type: ignore[arg-type]
    assert os.environ["PATH"] == "/usr/bin"


# ─── F12: grant-cache revocation is bounded to one turn ────────────────────


@pytest.mark.asyncio
async def test_grant_cache_revocation_bounded_to_current_turn(monkeypatch):
    from machina.permissions import broker, store

    sid = str(uuid.uuid4())
    gid = str(uuid.uuid4())
    durable = [
        {
            "id": gid,
            "session_id": sid,
            "agent_id": "h",
            "mode": "full",
            "capability": "session",
            "scope_path": None,
            "scope_type": "file",
            "grant_kind": "session",
            "digest": "full-mode-session",
            "expires_at": None,
            "revoked": False,
        }
    ]
    reads: list[str] = []

    class FakeDb:
        async def fetch(self, query, *args):
            reads.append(query)
            return [dict(grant) for grant in durable]

    async def fake_db():
        return FakeDb()

    monkeypatch.setattr(store, "_db", fake_db)
    store.clear_grant_cache()
    broker.set_approval_handler(None, principal="tui", turn_id="fm3-turn-1")
    try:
        assert len(await store.list_grants(sid)) == 1
        assert len(reads) == 1
        # Another worker revokes durably; this turn keeps the cached view
        # (the documented one-turn bound: the cache is turn-scoped).
        durable.clear()
        assert len(await store.list_grants(sid)) == 1
        assert len(reads) == 1
        # The next turn re-reads durable authority: revocation takes effect.
        broker.set_approval_handler(None, principal="tui", turn_id="fm3-turn-2")
        assert await store.list_grants(sid) == []
        assert len(reads) == 2
    finally:
        broker.set_approval_handler(None)
        store.clear_grant_cache()


@pytest.mark.asyncio
async def test_cached_grants_with_revoked_flag_are_never_returned(monkeypatch):
    from machina.permissions import broker, store

    sid = str(uuid.uuid4())

    class FailDb:
        async def fetch(self, query, *args):
            raise AssertionError("cache hit must not reach durable authority")

    async def fake_db():
        return FailDb()

    monkeypatch.setattr(store, "_db", fake_db)
    store.clear_grant_cache()
    broker.set_approval_handler(None, principal="tui", turn_id="fm3-turn-revoked")
    store._grant_cache[("fm3-turn-revoked", sid)] = [
        {"id": uuid.uuid4().hex, "session_id": sid, "revoked": True}
    ]
    try:
        assert await store.list_grants(sid) == []
    finally:
        broker.set_approval_handler(None)
        store.clear_grant_cache()


# ─── RF6-F3: httpx timeouts are bounded OAuth errors ───────────────────────


def test_httpx_timeout_maps_to_bounded_oauth_error():
    async def run():
        import httpx

        from machina.connectors.oauth_network import OAuthNetworkClient, OAuthNetworkError

        client = OAuthNetworkClient(local_access=False)

        class _BoomClient:
            def stream(self, *args, **kwargs):
                raise httpx.TimeoutException("simulated timeout")

        client._client = _BoomClient()
        deadline = asyncio.get_running_loop().time() + 5
        with pytest.raises(OAuthNetworkError, match="timed out"):
            await client._request_bounded(
                "GET",
                "https://auth.example.com/.well-known/oauth-authorization-server",
                max_bytes=1024,
                deadline=deadline,
                headers={},
            )

    asyncio.run(run())
