"""Unknown database ownership must never become a successful completion."""

import asyncio
import json
import uuid
from unittest.mock import AsyncMock

import pytest

from ah.core import turns
from ah.core.models import AgentResponse, Session, StreamEvent
from ah.db.connection import db


@pytest.mark.parametrize("operation", [turns.end_turn, turns.end_mutation])
async def test_release_database_failure_is_distinct_from_stale_owner(monkeypatch, operation):
    monkeypatch.setattr(db, "_pool", object())
    monkeypatch.setattr(turns, "_db_release", AsyncMock(side_effect=ConnectionError("offline")))
    with pytest.raises(ConnectionError):
        await operation(uuid.uuid4(), "owner")


async def test_claim_read_failure_cannot_prove_claim_absent(monkeypatch):
    monkeypatch.setattr(db, "_pool", object())
    monkeypatch.setattr(db, "fetchrow", AsyncMock(side_effect=ConnectionError("offline")))
    with pytest.raises(ConnectionError):
        await turns._db_claim_state(uuid.uuid4())
    assert await turns.turn_active(uuid.uuid4()) is True


@pytest.mark.parametrize("transport", ["gateway", "http"])
async def test_failed_release_emits_cleanup_pending_without_terminal(monkeypatch, transport):
    from ah import services
    from ah.core.session import session_manager

    turns._local_reset_for_tests()
    session = Session(id=uuid.uuid4())
    monkeypatch.setattr(session_manager, "get_fresh", AsyncMock(return_value=session))
    compact = AsyncMock()
    monkeypatch.setattr(services, "maybe_auto_compact", compact)
    monkeypatch.setattr(turns, "end_turn", AsyncMock(side_effect=ConnectionError("offline")))

    class Agent:
        async def run_stream(self, *args, **kwargs):
            yield StreamEvent(type="done", response=AgentResponse(content="answer"))

    owner = await turns.try_begin_turn(session.id)
    assert owner
    frames = []
    try:
        if transport == "gateway":
            from ah.gateway.server import Gateway

            gateway = Gateway(
                lambda frame: frames.append(frame["params"]),
                agent_factory=lambda *args: Agent(),
                owns_db=False,
            )
            gateway._turn_tokens[str(session.id)] = owner
            await gateway._run_turn(session.id, "release-failure", "work")
        else:
            import ah.core.agent_factory as factory
            from ah.api import app as api

            monkeypatch.setattr(factory, "build_agent_for_session", AsyncMock(return_value=Agent()))
            monkeypatch.setattr(turns, "try_begin_turn", AsyncMock(return_value=owner))
            endpoint = next(
                route.endpoint
                for route in api.create_app().routes
                if route.path == "/sessions/{session_id}/prompt"
            )
            response = await endpoint(str(session.id), api.PromptRequest(text="work"))
            raw = [frame async for frame in response.body_iterator]
            assert not any("[DONE]" in frame for frame in raw)
            frames = [json.loads(frame[6:]) for frame in raw]
        assert any(frame["type"] == "turn.cleanup_pending" for frame in frames)
        assert not any(frame["type"] in {"done", "message.complete"} for frame in frames)
        await asyncio.sleep(0)
        compact.assert_not_awaited()
    finally:
        turns._local_reset_for_tests()
