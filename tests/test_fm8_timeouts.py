"""FM8 #6: retrieval lookups carry explicit bounds; a hung backend never stalls a turn.

RED-first: before the fix, a ``search_archive_text`` / ``rag_pipeline.search``
implementation that never returns hangs the whole turn (the test's outer
``asyncio.wait_for`` fires). After the fix, the 5s / 15s bounds cancel the
hung await and the turn completes with a truthful RAG mode.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from machina.core.models import LLMResponse, Session


def _mock_session() -> Session:
    return Session(id=uuid.uuid4(), title="Test", context_budget=8000)


class _WaitForRecorder:
    """Wraps asyncio.wait_for, recording requested timeouts and shrinking the
    production 5s/15s bounds so the test is fast and deterministic."""

    def __init__(self) -> None:
        self.seen: list[float] = []
        self._real = asyncio.wait_for

    def install(self):
        async def fast(coro, timeout):
            self.seen.append(timeout)
            return await self._real(coro, timeout=0.05 if timeout and timeout >= 5 else timeout)

        return fast


async def _hang(*args, **kwargs):
    await asyncio.Event().wait()  # pragma: no cover - always cancelled


async def _run_turn(monkeypatch, *, rag_pipeline=None, archive_search=None):
    from machina.core.agent import ReActAgent

    recorder = _WaitForRecorder()
    monkeypatch.setattr("machina.core.agent.asyncio.wait_for", recorder.install())

    provider = AsyncMock()
    provider.complete = AsyncMock(
        return_value=LLMResponse(content="still answered", model="t", usage={"total_tokens": 5})
    )
    session = _mock_session()
    with (
        patch("machina.core.agent.session_manager") as mock_sm,
        patch("machina.core.agent.context_manager") as mock_cm,
    ):
        mock_sm.get = AsyncMock(return_value=session)
        mock_sm.update_activity = AsyncMock()
        mock_sm.update_state_fields = AsyncMock(return_value={})
        mock_cm.add_chunk = AsyncMock()
        mock_cm.add_chunks_batch = AsyncMock()
        mock_cm.get_latest_images = AsyncMock(return_value=[])
        mock_cm.get_recent_context = AsyncMock(return_value=[])
        mock_cm.search_archive_text = archive_search or AsyncMock(return_value=[])
        agent = ReActAgent(provider=provider, rag_pipeline=rag_pipeline)
        response = await asyncio.wait_for(agent.run(session.id, "go", verbose=False), timeout=3)
    return agent, response, recorder


async def test_archive_search_hang_is_bounded_at_5s(monkeypatch):
    agent, response, recorder = await _run_turn(
        monkeypatch, archive_search=AsyncMock(side_effect=_hang)
    )
    assert response.content == "still answered"
    assert response.outcome == "completed"
    assert 5 in recorder.seen, f"archive lookup must carry a 5s bound, saw {recorder.seen}"
    assert agent is not None


async def test_rag_search_hang_is_bounded_at_15s(monkeypatch):
    rag = SimpleNamespace(search=AsyncMock(side_effect=_hang))
    agent, response, recorder = await _run_turn(monkeypatch, rag_pipeline=rag)
    assert response.content == "still answered"
    assert rag.search.await_count == 1
    assert 15 in recorder.seen, f"RAG lookup must carry a 15s bound, saw {recorder.seen}"
    assert agent._last_rag_mode == "unavailable"
