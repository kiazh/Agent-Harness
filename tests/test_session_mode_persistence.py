"""Durable mode state is shared across workers without sharing live grants."""

import asyncio

import pytest

from ah.core import session_mode
from ah.core.config import config
from ah.core.session import SessionManager
from ah.gateway.features import mode as mode_feature
from ah.permissions import store


class ModeGateway:
    def __init__(self, sessions):
        self.sessions = sessions

    def require_db(self):
        pass

    async def get_session(self, params):
        import uuid

        return await self.sessions.get_fresh(uuid.UUID(params["sessionId"]))


@pytest.fixture
async def durable_modes(db_pool, monkeypatch):
    monkeypatch.setattr("ah.core.session.db", db_pool)
    monkeypatch.setattr("ah.db.connection.db", db_pool)
    monkeypatch.setattr(config, "execution_mode", "ask")
    sessions = SessionManager()
    created = []

    async def create():
        session = await sessions.create(title="mode persistence")
        created.append(session.id)
        return session

    yield db_pool, sessions, ModeGateway(sessions), create
    for sid in created:
        await sessions.delete(sid)
        session_mode.clear_session_mode(str(sid))


async def test_mode_survives_registry_restart_and_other_worker(durable_modes):
    database, sessions, gateway, create = durable_modes
    session = await create()
    await mode_feature.mode_set(gateway, {"sessionId": str(session.id), "mode": "workspace"})
    session_mode.reset_for_tests()
    result = await mode_feature.mode_get(gateway, {"sessionId": str(session.id)})
    assert result["mode"] == "workspace"
    assert (await SessionManager().get_fresh(session.id)).execution_mode == "workspace"
    assert config.get("execution_mode") == "ask"


async def test_global_default_affects_only_new_sessions(durable_modes):
    database, sessions, gateway, create = durable_modes
    old = await create()
    await mode_feature.mode_set(gateway, {"scope": "global", "mode": "full"})
    new = await create()
    assert (await sessions.get_fresh(old.id)).execution_mode == "ask"
    assert (await sessions.get_fresh(new.id)).execution_mode == "full"
    assert await store.list_grants(str(old.id)) == []
    # Routing mode never substitutes for a live grant.
    assert await store.list_grants(str(new.id)) == []


async def test_full_grant_and_mode_update_rollback_together(durable_modes, monkeypatch):
    database, sessions, gateway, create = durable_modes
    session = await create()

    async def failed_grant(*args, **kwargs):
        raise RuntimeError("grant persistence failure")

    monkeypatch.setattr(store, "save_grant", failed_grant)
    with pytest.raises(RuntimeError, match="grant persistence failure"):
        await mode_feature.mode_set(gateway, {"sessionId": str(session.id), "mode": "full"})
    assert (await sessions.get_fresh(session.id)).execution_mode == "ask"
    assert await store.list_grants(str(session.id)) == []


async def test_concurrent_mode_changes_keep_grants_consistent(durable_modes):
    database, sessions, gateway, create = durable_modes
    session = await create()
    await asyncio.gather(
        mode_feature.mode_set(gateway, {"sessionId": str(session.id), "mode": "full"}),
        mode_feature.mode_set(gateway, {"sessionId": str(session.id), "mode": "workspace"}),
    )
    stored = (await sessions.get_fresh(session.id)).execution_mode
    full_grants = [g for g in await store.list_grants(str(session.id)) if g["mode"] == "full"]
    assert bool(full_grants) == (stored == "full")


async def test_revoke_persists_ask_even_when_global_default_is_full(durable_modes):
    database, sessions, gateway, create = durable_modes
    session = await create()
    await mode_feature.mode_set(gateway, {"sessionId": str(session.id), "mode": "full"})
    config.set("execution_mode", "full")
    await mode_feature.mode_revoke(gateway, {"sessionId": str(session.id)})
    session_mode.reset_for_tests()
    assert (await mode_feature.mode_get(gateway, {"sessionId": str(session.id)}))["mode"] == "ask"
    assert await store.list_grants(str(session.id)) == []


async def test_durable_mode_read_failure_never_uses_cached_full(durable_modes, monkeypatch):
    database, sessions, gateway, create = durable_modes
    session = await create()
    session_mode.set_session_mode(str(session.id), "full")

    async def unavailable(*args):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(database, "fetchrow", unavailable)
    with pytest.raises(RuntimeError, match="database unavailable"):
        await mode_feature.mode_get(gateway, {"sessionId": str(session.id)})
