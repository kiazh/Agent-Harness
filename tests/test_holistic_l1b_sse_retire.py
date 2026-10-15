"""L1b residual (H01 follow-up, holistic review 2026-10-15): the SSE retire
join must be bounded.

Pre-fix, ``_stream_session``'s ``retire_execution`` joined the cancelled SSE
step task through the unbounded ``await_joined`` before calling
``close_turn()``. ``TurnEngine.close()`` itself is bounded (L1/H01), but a
cancellation-resistant *step* wedged the transport first: ``close_turn()``
never ran, so the owned provider stayed open and the session claim was never
released (its renewal loop kept renewing it).

These pins drive the real REST streaming route (``create_app`` endpoint, real
``TurnEngine``) against a step that absorbs every cancellation:

- the wedged step must not wedge ``retire_execution``: within the transport's
  ``RETIRE_JOIN_TIMEOUT_SECONDS`` bound ``close_turn()`` retires the turn and
  the step is quarantined (still strongly referenced) instead of awaited;
- a step that does settle is still joined before retirement (no early release
  for resistant-but-settling work), and
- a well-behaved cancellable step keeps the existing direct close path.
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock

from machina.api.routes.models import PromptRequest
from machina.core.models import AgentResponse, Session, StreamEvent


class _RecordingProvider:
    """Provider whose ``close`` is the observable end of ``close_turn()``."""

    def __init__(self) -> None:
        self.closed = asyncio.Event()

    async def close(self) -> None:
        self.closed.set()


class _WedgeAgent:
    """Stream that absorbs every cancellation until ``release`` is set."""

    _owns_provider = True

    def __init__(self) -> None:
        self.provider = _RecordingProvider()
        self.release = asyncio.Event()
        self.entered = asyncio.Event()
        self.stopped = asyncio.Event()
        self.step_task: asyncio.Task | None = None

    async def run_stream(self, *_args, **_kwargs):
        self.step_task = asyncio.current_task()
        self.entered.set()
        while not self.release.is_set():
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                continue
        self.stopped.set()
        yield StreamEvent(type="done", response=AgentResponse(content="late result"))


class _WellBehavedAgent:
    """Stream that blocks on a cancellable wait: the route's cancel settles it."""

    _owns_provider = True

    def __init__(self) -> None:
        self.provider = _RecordingProvider()
        self.entered = asyncio.Event()

    async def run_stream(self, *_args, **_kwargs):
        self.entered.set()
        await asyncio.Event().wait()  # pragma: no cover - cancelled by the route
        yield StreamEvent(type="done", response=AgentResponse(content="never"))


async def _drive_route(monkeypatch, session: Session, agent, *, retire_timeout: float):
    """Wire the real streaming endpoint with the given agent; return a namespace.

    Only the provider boundary (agent factory), session lookup, and the
    transport's retire bound are faked/overridden; the real route, real
    ``TurnEngine`` and the DB-less claim registry run unmodified.
    """
    from types import SimpleNamespace

    import machina.core.agent_factory as factory
    from machina import services
    from machina.api import app as api
    from machina.api.routes import streaming as streaming_module
    from machina.core import turns
    from machina.core.config import config
    from machina.core.session import session_manager

    turns._local_reset_for_tests()
    retired = asyncio.Event()
    monkeypatch.setattr(config, "turn_timeout", 0.01)
    # Pre-fix the constant does not exist (unbounded join): raising=False keeps
    # the RED run honest — the override is inert until the fix reads it.
    monkeypatch.setattr(
        streaming_module, "RETIRE_JOIN_TIMEOUT_SECONDS", retire_timeout, raising=False
    )
    monkeypatch.setattr(session_manager, "get_fresh", AsyncMock(return_value=session))
    monkeypatch.setattr(factory, "build_agent_for_session", AsyncMock(return_value=agent))
    # Fire-and-forget post-turn compaction is unrelated background work.
    monkeypatch.setattr(services, "maybe_auto_compact", AsyncMock())
    real_end = turns.end_turn

    async def release_owner(sid, token):
        result = await real_end(sid, token)
        retired.set()
        return result

    monkeypatch.setattr(turns, "end_turn", release_owner)
    endpoint = next(
        route.endpoint
        for route in api.create_app().routes
        if route.path == "/sessions/{session_id}/prompt"
    )
    response = await endpoint(str(session.id), PromptRequest(text="work"))
    return SimpleNamespace(response=response, retired=retired, real_end=real_end, turns=turns)


async def _consume(response) -> tuple[list[str], bool]:
    async def drive():
        return [frame async for frame in response.body_iterator]

    task = asyncio.create_task(drive())
    done, _ = await asyncio.wait({task}, timeout=1.5)
    assert task in done, "the SSE cleanup path itself must not wedge"
    frames = await task
    return frames, any("turn.cleanup_pending" in frame for frame in frames)


async def _settle_wedged_step(agent: _WedgeAgent) -> None:
    """Release a wedged execution so its (quarantined) task can unwind."""
    agent.release.set()
    if agent.step_task is not None:
        await asyncio.wait_for(asyncio.gather(agent.step_task, return_exceptions=True), timeout=2.0)


async def _drain_tracked(timeout: float = 2.0) -> None:
    """Wait (bounded) for the route's tracked retirement to actually finish.

    ``retire_execution`` is registered with ``runtime_services`` (the route's
    strong reference). The assertions observe its effects; without this drain
    the test could end while the retire task is still mid-``close_turn``, and
    the event-loop teardown would cancel it (a teardown-only artifact).
    """
    from machina.core.runtime import runtime_services

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        pending = [task for task in runtime_services._background if not task.done()]
        remaining = deadline - loop.time()
        if not pending or remaining <= 0:
            return
        await asyncio.wait(pending, timeout=remaining)


async def _await_retire_task(agent: _WedgeAgent, timeout: float = 2.0) -> asyncio.Task | None:
    """Await the tracked retirement task (the only other tracked task) and return it."""
    from machina.core.runtime import runtime_services

    retire = next(
        (task for task in runtime_services._background if task is not agent.step_task), None
    )
    if retire is not None:
        await asyncio.wait({retire}, timeout=timeout)
    return retire


async def test_wedged_step_retire_join_is_bounded(monkeypatch):
    """A step that never settles must not wedge retire_execution before close_turn."""
    from machina.core import background_tasks

    session = Session(id=uuid.uuid4())
    agent = _WedgeAgent()
    env = await _drive_route(monkeypatch, session, agent, retire_timeout=0.2)

    frames, cleanup_pending = await _consume(env.response)
    try:
        assert cleanup_pending, frames
        assert agent.entered.is_set(), "the stream step never started"

        retired_in_time = True
        try:
            await asyncio.wait_for(env.retired.wait(), 1.5)
        except TimeoutError:
            retired_in_time = False
        quarantined = agent.step_task in background_tasks._quarantined_joins
        claim = await env.turns.try_begin_turn(session.id)
        if claim is not None:
            await env.real_end(session.id, claim)

        assert retired_in_time, (
            "retire_execution did not reach close_turn within the bound: a wedged "
            "step must be quarantined, never awaited unbounded"
        )
        assert quarantined, "the wedged step was not quarantined (cancelled + kept referenced)"
        assert claim is not None, "the session claim stayed held while the step was wedged"
        assert agent.provider.closed.is_set(), "close_turn did not close the owned provider"
        assert not agent.stopped.is_set(), "the step must still be wedged when the turn retires"
        # H01 rule: with the step still executing, the stream generator must
        # not be aclosed (that would raise); retirement still ends cleanly.
        retire_task = await _await_retire_task(agent)
        assert retire_task is not None and retire_task.done(), "retirement did not finish"
        assert retire_task.exception() is None, repr(retire_task.exception())
    finally:
        # Always release the double so its quarantined task can unwind; on the
        # pre-fix hang this also lets the unbounded retire finish before the
        # event loop is finalized.
        await _settle_wedged_step(agent)
        await asyncio.wait_for(agent.provider.closed.wait(), 2.0)
        await _drain_tracked()


async def test_settling_step_is_still_joined_before_retirement(monkeypatch):
    """Control: a step that settles is joined before retirement (no early release)."""
    session = Session(id=uuid.uuid4())
    agent = _WedgeAgent()
    env = await _drive_route(monkeypatch, session, agent, retire_timeout=2.0)

    frames, cleanup_pending = await _consume(env.response)
    try:
        assert cleanup_pending, frames
        # The run is resistant but mid-flight: the turn stays owned and un-retired.
        assert agent.entered.is_set()
        assert not agent.stopped.is_set()
        assert not env.retired.is_set()
        assert not agent.provider.closed.is_set()
        assert await env.turns.try_begin_turn(session.id) is None

        agent.release.set()
        await asyncio.wait_for(env.retired.wait(), 1.5)
        assert agent.stopped.is_set()
        assert agent.provider.closed.is_set()
        claim = await env.turns.try_begin_turn(session.id)
        assert claim
        await env.real_end(session.id, claim)
    finally:
        await _settle_wedged_step(agent)
        await _drain_tracked()


async def test_well_behaved_step_takes_the_direct_close_path(monkeypatch):
    """Control: a cancellable step keeps the direct close path (no retire marker)."""
    session = Session(id=uuid.uuid4())
    agent = _WellBehavedAgent()
    env = await _drive_route(monkeypatch, session, agent, retire_timeout=0.2)

    frames, cleanup_pending = await _consume(env.response)
    assert not cleanup_pending, frames
    # The direct path completes close_turn() before the stream yields its tail.
    assert env.retired.is_set()
    assert agent.provider.closed.is_set()
    claim = await env.turns.try_begin_turn(session.id)
    assert claim
    await env.real_end(session.id, claim)
    await _drain_tracked()
