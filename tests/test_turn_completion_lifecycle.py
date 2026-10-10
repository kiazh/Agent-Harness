"""A resistant turn remains owned until its actual execution has retired."""

import asyncio
import uuid
from unittest.mock import AsyncMock

import pytest

from ah.core.models import AgentResponse, Session, StreamEvent
from ah.gateway import server


@pytest.mark.parametrize("stop_kind", ["timeout", "cancel"])
async def test_gateway_quarantine_preserves_owner_and_terminal_order(monkeypatch, stop_kind):
    from ah import services
    from ah.core import turns
    from ah.core.config import config
    from ah.core.session import session_manager

    turns._local_reset_for_tests()
    session = Session(id=uuid.uuid4())
    started, release = asyncio.Event(), asyncio.Event()
    frames = []
    monkeypatch.setattr(config, "turn_timeout", 0.01 if stop_kind == "timeout" else 300)
    monkeypatch.setattr(server, "CANCEL_JOIN_TIMEOUT_SECONDS", 0.05, raising=False)
    monkeypatch.setattr(session_manager, "get_fresh", AsyncMock(return_value=session))
    monkeypatch.setattr(services, "maybe_auto_compact", AsyncMock())

    class Agent:
        async def run_stream(self, *args, **kwargs):
            started.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue
            yield StreamEvent(type="done", response=AgentResponse(content="late result"))

    gateway = server.Gateway(
        lambda frame: frames.append(frame["params"]),
        agent_factory=lambda *args: Agent(),
        owns_db=False,
    )
    sid, turn_id = str(session.id), "resistant-turn"
    owner = await turns.try_begin_turn(session.id)
    gateway._turn_tokens[sid] = owner
    task = asyncio.create_task(gateway._run_turn_with_timeout(session.id, turn_id, "work"))
    gateway._turns[sid] = task
    inner = None
    try:
        await asyncio.wait_for(started.wait(), 0.5)
        if stop_kind == "cancel":
            task.cancel()
        done, _ = await asyncio.wait({task}, timeout=0.8)
        assert task in done, "timeout/cancel must return within the cleanup observation bound"
        await asyncio.gather(task, return_exceptions=True)
        assert any(frame["type"] == "turn.cleanup_pending" for frame in frames)
        assert not any(frame["type"] == "message.complete" for frame in frames)
        inner = gateway._turns.get(sid)
        assert inner is not None and not inner.done(), "runaway lost its strong owner reference"
        assert await turns.try_begin_turn(session.id) is None
        release.set()
        await asyncio.wait_for(asyncio.shield(inner), 0.8)
        terminals = [frame for frame in frames if frame["type"] == "message.complete"]
        assert len(terminals) == 1 and terminals[0]["cancelled"] is True
        replacement = await turns.try_begin_turn(session.id)
        assert replacement
        await turns.end_turn(session.id, replacement)
    finally:
        release.set()
        task.cancel()
        if inner is None:
            inner = gateway._turns.get(sid)
        await asyncio.gather(
            task,
            *([inner] if inner is not None and inner is not task else []),
            return_exceptions=True,
        )
        await turns.end_turn(session.id, owner)


async def test_rest_timeout_quarantines_until_actual_execution_stops(monkeypatch):
    from ah.api import app as api
    import ah.core.agent_factory as factory
    from ah.core import turns
    from ah.core.config import config
    from ah.core.session import session_manager

    turns._local_reset_for_tests()
    session = Session(id=uuid.uuid4())
    release, retired = asyncio.Event(), asyncio.Event()
    closed = []
    monkeypatch.setattr(config, "turn_timeout", 0.01)
    monkeypatch.setattr(session_manager, "get_fresh", AsyncMock(return_value=session))

    class Provider:
        async def close(self):
            closed.append(True)

    class Agent:
        _owns_provider = True
        provider = Provider()

        async def run_stream(self, *args, **kwargs):
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue
            yield StreamEvent(type="done", response=AgentResponse(content="late result"))

    monkeypatch.setattr(factory, "build_agent_for_session", AsyncMock(return_value=Agent()))
    real_end = turns.end_turn

    async def release_owner(sid, token):
        result = await real_end(sid, token)
        retired.set()
        return result

    monkeypatch.setattr(turns, "end_turn", release_owner)
    endpoint = next(route.endpoint for route in api.create_app().routes
        if route.path == "/sessions/{session_id}/prompt")
    response = await endpoint(str(session.id), api.PromptRequest(text="work"))

    async def consume():
        return [frame async for frame in response.body_iterator]

    task = asyncio.create_task(consume())
    try:
        done, _ = await asyncio.wait({task}, timeout=0.8)
        assert task in done
        frames = await task
        assert any('"type": "turn.cleanup_pending"' in frame for frame in frames)
        assert not any("[DONE]" in frame or '"type": "done"' in frame for frame in frames)
        assert await turns.try_begin_turn(session.id) is None
        assert closed == [] and not retired.is_set()
        release.set()
        await asyncio.wait_for(retired.wait(), 0.8)
        assert closed == [True]
        replacement = await turns.try_begin_turn(session.id)
        assert replacement
        await real_end(session.id, replacement)
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
