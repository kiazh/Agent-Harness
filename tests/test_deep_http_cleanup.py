"""SSE completion stays bounded when optional learning resists cancellation."""

import asyncio
import uuid
from unittest.mock import AsyncMock

from httpx import ASGITransport, AsyncClient

from ah.api.app import create_app
from ah.core.models import AgentResponse, Session, StreamEvent


async def test_sse_releases_turn_before_done_with_resistant_learning(monkeypatch):
    import ah.core.agent_factory as factory
    from ah import services
    from ah.core import turns
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
    released = asyncio.Event()

    class Agent:
        _learning_tasks = {worker}

        async def run_stream(self, *_args, **_kwargs):
            yield StreamEvent(type="done", response=AgentResponse(content="finished"))

    async def end_turn(session_id, token):
        assert session_id == session.id
        assert token == "sse-owner"
        released.set()

    monkeypatch.setenv("AGENT_HARNESS_API_KEY", "cleanup-test-key")
    monkeypatch.setattr(session_manager, "get_fresh", AsyncMock(return_value=session))
    monkeypatch.setattr(factory, "build_agent_for_session", AsyncMock(return_value=Agent()))
    monkeypatch.setattr(services, "maybe_auto_compact", AsyncMock())
    monkeypatch.setattr(turns, "try_begin_turn", AsyncMock(return_value="sse-owner"))
    monkeypatch.setattr(turns, "end_turn", end_turn)
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        request = asyncio.create_task(
            client.post(
                f"/sessions/{session.id}/prompt",
                json={"text": "hello"},
                headers={"Authorization": "Bearer cleanup-test-key"},
            )
        )
        try:
            done, _ = await asyncio.wait({request}, timeout=2.5)
            assert request in done, "optional learning retained the SSE response past its deadline"
            response = await request
            assert response.status_code == 200
            assert released.is_set()
            assert '"content": "finished"' in response.text
            assert "data: [DONE]" in response.text
        finally:
            release.set()
            await asyncio.gather(worker, request, return_exceptions=True)
