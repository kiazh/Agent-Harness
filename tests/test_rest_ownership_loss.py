"""REST claim loss cancels the active generator before another effect."""

import asyncio
import json
import uuid
from unittest.mock import AsyncMock

import pytest

from machina.api import app as api
from machina.core.models import Session


@pytest.mark.parametrize("renewal_error", [False, True])
async def test_rest_lost_claim_cancels_execution(monkeypatch, renewal_error):
    import machina.core.agent_factory as factory
    from machina import services
    from machina.core import turns
    from machina.core.session import session_manager

    started, stopped, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    effects = []
    session = Session(id=uuid.uuid4())

    class Agent:
        async def run_stream(self, *args, **kwargs):
            started.set()
            try:
                await release.wait()
                effects.append("effect after loss")
                yield api.StreamEvent(type="text", content="false success")
            finally:
                stopped.set()

    async def renew(*args):
        await started.wait()
        if renewal_error:
            raise RuntimeError("database unavailable")
        return False

    monkeypatch.setattr(api, "CLAIM_RENEW_INTERVAL_SECONDS", 0.01, raising=False)
    monkeypatch.setattr(session_manager, "get_fresh", AsyncMock(return_value=session))
    monkeypatch.setattr(factory, "build_agent_for_session", AsyncMock(return_value=Agent()))
    monkeypatch.setattr(services, "maybe_auto_compact", AsyncMock())
    monkeypatch.setattr(turns, "try_begin_turn", AsyncMock(return_value="retiring-owner"))
    end = AsyncMock(return_value=False)  # A replacement owns the durable claim.
    monkeypatch.setattr(turns, "end_turn", end)
    monkeypatch.setattr(turns, "renew_turn", renew)
    endpoint = next(
        route.endpoint
        for route in api.create_app().routes
        if route.path == "/sessions/{session_id}/prompt"
    )
    response = await endpoint(str(session.id), api.PromptRequest(text="work"))

    async def consume():
        return [frame async for frame in response.body_iterator]

    task = asyncio.create_task(consume())
    try:
        done, _ = await asyncio.wait({task}, timeout=0.5)
        assert task in done, "REST execution continued after failed claim renewal"
        frames = await task
        assert stopped.is_set() and effects == []
        outcomes = [json.loads(frame[6:]) for frame in frames if "[DONE]" not in frame]
        assert any(event["type"] == "turn.ownership_lost" for event in outcomes)
        assert not any(event.get("content") == "false success" for event in outcomes)
        end.assert_awaited_once_with(session.id, "retiring-owner")
    finally:
        task.cancel()
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("repeat_cancel", [False, True])
async def test_http_disconnect_closes_source_before_provider_and_claim(monkeypatch, repeat_cancel):
    import machina.core.agent_factory as factory
    from machina import services
    from machina.core import turns
    from machina.core.session import session_manager

    order = []
    closing_started, release_close = asyncio.Event(), asyncio.Event()
    session = Session(id=uuid.uuid4())

    class Agent:
        async def run_stream(self, *args, **kwargs):
            try:
                yield api.StreamEvent(
                    type="tool_result", tool_name="probe", tool_result="saved-effect"
                )
            finally:
                closing_started.set()
                if repeat_cancel:
                    await release_close.wait()
                order.append("records")

    async def close_provider(agent):
        order.append("provider")

    async def end(*args):
        order.append("claim")
        return True

    monkeypatch.setattr(session_manager, "get_fresh", AsyncMock(return_value=session))
    monkeypatch.setattr(factory, "build_agent_for_session", AsyncMock(return_value=Agent()))
    monkeypatch.setattr(factory, "close_agent_provider", close_provider)
    monkeypatch.setattr(services, "maybe_auto_compact", AsyncMock())
    monkeypatch.setattr(turns, "try_begin_turn", AsyncMock(return_value="owner"))
    monkeypatch.setattr(turns, "end_turn", end)
    monkeypatch.setattr(turns, "renew_turn", AsyncMock(return_value=True))
    endpoint = next(
        route.endpoint
        for route in api.create_app().routes
        if route.path == "/sessions/{session_id}/prompt"
    )
    response = await endpoint(str(session.id), api.PromptRequest(text="work"))
    async for frame in response.body_iterator:
        if "saved-effect" in frame:
            break
    closing = asyncio.create_task(response.body_iterator.aclose())
    await closing_started.wait()
    if repeat_cancel:
        closing.cancel()
        await asyncio.sleep(0)
        closing.cancel()
        release_close.set()
        with pytest.raises(asyncio.CancelledError):
            await closing
    else:
        await closing
    assert order == ["records", "provider", "claim"]
