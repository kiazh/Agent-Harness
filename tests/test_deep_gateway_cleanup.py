"""Gateway factory injection and optional work preserve the turn contract."""

import asyncio
import uuid
from unittest.mock import AsyncMock

from ah.core.models import AgentResponse, Session, StreamEvent
from ah.gateway.server import Gateway


async def test_gateway_honors_explicit_factory_with_stored_session_settings(monkeypatch):
    import ah.core.agent_factory as factory
    from ah import services
    from ah.core.session import session_manager

    session = Session(id=uuid.uuid4(), model="saved-model", provider="saved-provider")
    frames, calls = [], []

    class Agent:
        async def run_stream(self, *_args, **_kwargs):
            yield StreamEvent(type="done", response=AgentResponse(content="injected answer"))

    def injected(model, provider):
        calls.append((model, provider))
        return Agent()

    monkeypatch.setattr(session_manager, "get_fresh", AsyncMock(return_value=session))
    monkeypatch.setattr(
        factory,
        "build_agent_for_session",
        AsyncMock(side_effect=RuntimeError("unexpected default")),
    )
    monkeypatch.setattr(services, "maybe_auto_compact", AsyncMock())
    gateway = Gateway(frames.append, agent_factory=injected, owns_db=False)
    await gateway._run_turn(session.id, "injected-turn", "hello")
    completions = [f["params"] for f in frames if f["params"]["type"] == "message.complete"]
    assert calls == [("saved-model", "saved-provider")]
    assert len(completions) == 1
    assert completions[0]["text"] == "injected answer"


async def test_gateway_completion_does_not_wait_for_resistant_learning(monkeypatch):
    import ah.core.agent_factory as factory
    from ah import services
    from ah.core.session import session_manager

    started, release = asyncio.Event(), asyncio.Event()

    async def learning():
        started.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue

    worker = asyncio.create_task(learning())
    await started.wait()
    session = Session(id=uuid.uuid4())
    frames = []

    class Agent:
        _learning_tasks = {worker}

        async def run_stream(self, *_args, **_kwargs):
            yield StreamEvent(type="done", response=AgentResponse(content="finished"))

    monkeypatch.setattr(session_manager, "get_fresh", AsyncMock(return_value=session))
    monkeypatch.setattr(factory, "build_agent_for_session", AsyncMock(return_value=Agent()))
    monkeypatch.setattr(services, "maybe_auto_compact", AsyncMock())
    gateway = Gateway(frames.append, owns_db=False)
    turn = asyncio.create_task(gateway._run_turn(session.id, "learning-turn", "hello"))
    try:
        done, _ = await asyncio.wait({turn}, timeout=2.5)
        assert turn in done, "optional learning retained the turn past its cleanup deadline"
        await turn
        assert any(
            f["params"]["type"] == "message.complete" and f["params"]["text"] == "finished"
            for f in frames
        )
    finally:
        release.set()
        await asyncio.gather(worker, turn, return_exceptions=True)


async def test_late_turn_cleanup_cannot_release_replacement_turn(monkeypatch):
    import ah.core.agent_factory as factory
    from ah import services
    from ah.core import turns
    from ah.core.session import session_manager

    started, release = asyncio.Event(), asyncio.Event()
    session = Session(id=uuid.uuid4())
    released = []

    class Agent:
        async def run_stream(self, *_args, **_kwargs):
            started.set()
            await release.wait()
            yield StreamEvent(type="done", response=AgentResponse(content="old answer"))

    async def end_turn(session_id, token):
        released.append((session_id, token))
        return True

    monkeypatch.setattr(session_manager, "get_fresh", AsyncMock(return_value=session))
    monkeypatch.setattr(factory, "build_agent_for_session", AsyncMock(return_value=Agent()))
    monkeypatch.setattr(services, "maybe_auto_compact", AsyncMock())
    monkeypatch.setattr(turns, "end_turn", end_turn)
    gateway = Gateway(lambda _frame: None, owns_db=False)
    sid = str(session.id)
    gateway._turn_tokens[sid] = "old-owner"
    turn = asyncio.create_task(gateway._run_turn(session.id, "old-turn", "hello"))
    await started.wait()
    gateway._turn_tokens[sid] = "replacement-owner"
    release.set()
    await turn
    assert released == [(session.id, "old-owner")]
    assert gateway._turn_tokens[sid] == "replacement-owner"
