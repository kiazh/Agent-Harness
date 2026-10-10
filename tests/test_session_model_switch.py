"""Switching the model re-pins the live session, not just future defaults.

Regression: sessions pin model/provider at creation and the agent factory
prefers the pinned values, so switching the default alone left the current
chat failing on the old provider. /models, /provider and /default-model now
route through session.setModel for the live session.
"""

import uuid

import pytest

from ah.core.session import SessionManager
from ah.gateway.errors import RpcError
from ah.gateway.features import sessions as session_feature


class ModelGateway:
    def require_db(self):
        pass

    def __init__(self, sessions):
        self.sessions = sessions

    async def get_session(self, params):
        return await self.sessions.get_fresh(uuid.UUID(params["sessionId"]))


@pytest.fixture
async def live_sessions(db_pool, monkeypatch):
    monkeypatch.setattr("ah.core.session.db", db_pool)
    monkeypatch.setattr("ah.db.connection.db", db_pool)
    sessions = SessionManager()
    created = []

    async def create():
        session = await sessions.create(title="model switch")
        created.append(session.id)
        return session

    yield sessions, ModelGateway(sessions), create
    for sid in created:
        await sessions.delete(sid)


async def test_set_model_rejects_garbage_without_touching_db():
    sessions = SessionManager()
    with pytest.raises(ValueError):
        await sessions.set_model(uuid.uuid4())
    with pytest.raises(ValueError):
        await sessions.set_model(uuid.uuid4(), model="   ")
    with pytest.raises(ValueError):
        await sessions.set_model(uuid.uuid4(), model="x" * 201)


async def test_switch_re_pins_live_session(live_sessions):
    sessions, gateway, create = live_sessions
    session = await create()
    result = await session_feature.session_set_model(
        gateway,
        {"sessionId": str(session.id), "model": "deepseek-chat", "provider": "deepseek"},
    )
    assert result["sessionId"] == str(session.id)
    assert result["model"] == "deepseek-chat"
    assert result["provider"] == "deepseek"
    fresh = await sessions.get_fresh(session.id)
    assert fresh.model == "deepseek-chat"
    assert fresh.provider == "deepseek"


async def test_switch_rejects_unknown_provider_and_empty_request(live_sessions):
    _, gateway, create = live_sessions
    session = await create()
    with pytest.raises(RpcError):
        await session_feature.session_set_model(
            gateway, {"sessionId": str(session.id), "provider": "bogus"}
        )
    with pytest.raises(RpcError):
        await session_feature.session_set_model(gateway, {"sessionId": str(session.id)})
    # Failed switches leave the pinned values untouched.
    fresh = await SessionManager().get_fresh(session.id)
    assert (fresh.model, fresh.provider) == (session.model, session.provider)
