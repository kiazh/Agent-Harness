"""Adversarial E2E proof for R4-010: per-step SSE tasks vs. the stream turn-scope.

``streaming._stream_session`` pulls every agent-generator step in a fresh
asyncio Task (``_owned_step``), while ``instrument_stream``
(``machina/core/agent.py``) enters ``session_mode.turn_scope()`` when the
generator is first resumed and resets the ContextVar at generator exit.  The
reset therefore runs in a different task (and Context) than the one that
created the token, so completing a stream raises ``ValueError: ... was created
in a different Context``; the route swallows it into a spurious ``error``
frame after the last delta (R4-010).

These tests drive the real route (``create_app`` + ASGITransport), the real
``TurnEngine`` and the real ``instrument_stream`` wrapper against the real
test database.  Only the provider factory is faked, at the provider boundary.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from machina.api import app as api
from machina.api.routes.models import PromptRequest
from machina.core.models import LLMResponse, StreamEvent
from machina.core.provider import LLMProvider
from machina.core.turns import end_turn, try_begin_turn

pytestmark = [pytest.mark.integration, pytest.mark.db]

# Three deltas: a multi-event stream that exercises several generator steps.
REPLY_CHUNKS = ("Streaming ", "proof ", "in three parts.")
REPLY = "".join(REPLY_CHUNKS)


class _ScriptedProvider(LLMProvider):
    """Provider faked at the factory boundary: scripted deltas, no network."""

    model = "scripted-e2e"

    def __init__(self, chunks: tuple[str, ...] = REPLY_CHUNKS) -> None:
        self._chunks = chunks
        self.closed_event = asyncio.Event()

    async def stream_complete(self, messages, **kwargs):
        for chunk in self._chunks:
            yield StreamEvent(type="text", content=chunk)
        yield StreamEvent(
            type="done",
            response=LLMResponse(
                content="".join(self._chunks),
                model=self.model,
                usage={"prompt_tokens": 7, "completion_tokens": 5, "total_tokens": 12},
            ),
        )

    async def close(self) -> None:
        self.closed_event.set()


class _BlockingProvider(_ScriptedProvider):
    """Yields one delta, then blocks forever (bounded) until cancelled."""

    def __init__(self) -> None:
        super().__init__(chunks=("partial ",))
        self.blocked = asyncio.Event()
        self.release = asyncio.Event()  # never set: the test cancels instead

    async def stream_complete(self, messages, **kwargs):
        yield StreamEvent(type="text", content=self._chunks[0])
        self.blocked.set()
        # The test cancels the stream while we wait here; the bound is only a
        # hang guard so a broken cancellation can never wedge the suite.
        await asyncio.wait_for(self.release.wait(), 30)
        raise AssertionError("blocking provider was never cancelled")


@pytest.fixture
async def sse_env(db_pool, monkeypatch):
    """Real app + real test DB; each test fakes only the provider factory."""
    monkeypatch.setattr("machina.db.connection.db", db_pool)
    monkeypatch.setattr(api, "db", db_pool)
    monkeypatch.setattr("machina.core.session.db", db_pool)
    monkeypatch.setattr("machina.core.context.db", db_pool)
    monkeypatch.setattr("machina.core.scheduler.db", db_pool)
    monkeypatch.setattr("machina.core.usage.db", db_pool)
    monkeypatch.setenv("MACHINA_API_KEY", "r4-sse-token")
    import machina.services as services

    # Post-turn compaction is unrelated background work; never let it spend
    # the scripted provider (same isolation the production smoke fixture uses).
    monkeypatch.setattr(services, "maybe_auto_compact", AsyncMock())
    app = api.create_app()
    created: list[str] = []

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://r4",
        headers={"Authorization": "Bearer r4-sse-token"},
    ) as client:
        yield SimpleNamespace(client=client, app=app, created=created, monkeypatch=monkeypatch)
    from machina.core.session import session_manager

    for sid in created:
        await session_manager.delete(uuid.UUID(sid))


def _use_provider(env: SimpleNamespace, provider: LLMProvider) -> None:
    """Fake the provider at its factory boundary; nothing else is replaced."""
    env.monkeypatch.setattr("machina.core.provider.get_provider", lambda **kwargs: provider)


async def _create_session(env: SimpleNamespace) -> str:
    response = await env.client.post("/api/v1/sessions", json={"title": "R4-010 SSE harness"})
    assert response.status_code == 200, response.text
    sid = response.json()["session"]["id"]
    env.created.append(sid)
    return sid


async def _prompt_frames(env: SimpleNamespace, sid: str) -> tuple[str, list[dict]]:
    response = await env.client.post(
        f"/api/v1/sessions/{sid}/prompt", json={"text": "hello", "protocolVersion": 1}
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    frames = [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    return response.text, frames


async def test_stream_completes_with_normal_frames_and_preserves_deltas(sse_env):
    sid = await _create_session(sse_env)
    _use_provider(sse_env, _ScriptedProvider())

    body, frames = await _prompt_frames(sse_env, sid)
    types = [frame["type"] for frame in frames]

    assert types[0] == "turn.started"
    assert types[-1] == "done"
    assert body.endswith("data: [DONE]\n\n")
    deltas = [frame["content"] for frame in frames if frame["type"] == "text"]
    assert len(deltas) >= 2  # multi-event stream, not a single terminal frame
    assert "".join(deltas) == REPLY
    assert frames[-1]["content"] == REPLY


async def test_client_cancel_mid_stream_closes_provider_and_releases_claim(sse_env):
    sid = await _create_session(sse_env)
    provider = _BlockingProvider()
    _use_provider(sse_env, provider)

    prompt = next(
        route.endpoint
        for route in sse_env.app.routes
        if route.path == "/sessions/{session_id}/prompt"
    )
    # The route endpoint is the only place a mid-stream cancel is expressible:
    # httpx's ASGITransport buffers the whole SSE body before returning.
    response = await prompt(sid, PromptRequest(text="hello"))
    stream = response.body_iterator

    started = await anext(stream)
    assert '"type": "turn.started"' in started
    delta = await anext(stream)
    assert '"type": "text"' in delta

    # The provider starts blocking only when the next step resumes the stream.
    pending = asyncio.create_task(anext(stream))
    await provider.blocked.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending

    # Cleanup must close the owned provider and release the turn claim.
    await asyncio.wait_for(provider.closed_event.wait(), 5)
    token = await try_begin_turn(uuid.UUID(sid))
    assert token, "turn claim was not released after cancellation"
    assert await end_turn(uuid.UUID(sid), token)


async def test_multi_event_stream_has_no_error_frame(sse_env, caplog):
    caplog.set_level(logging.ERROR, logger="machina.api.routes.streaming")
    sid = await _create_session(sse_env)
    _use_provider(sse_env, _ScriptedProvider())

    body, frames = await _prompt_frames(sse_env, sid)
    types = [frame["type"] for frame in frames]

    assert types[0] == "turn.started"
    assert len([t for t in types if t == "text"]) >= 2
    assert types[-1] == "done"  # the stream itself completes normally
    assert "error" not in types, "spurious error frame after the last delta (R4-010)"
    assert body.endswith("data: [DONE]\n\n")
    # Completing the stream raises no cross-context ValueError: today the route
    # logs it (with traceback) before converting it into the error frame.
    cross_context = [
        record
        for record in caplog.records
        if record.name == "machina.api.routes.streaming"
        and record.exc_info
        and isinstance(record.exc_info[1], ValueError)
        and "was created in a different Context" in str(record.exc_info[1])
    ]
    assert not cross_context, "stream completion raised a cross-context ValueError (R4-010)"


def _otel_detach_errors(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("opentelemetry") and record.levelno >= logging.ERROR
    ]


async def test_stream_span_never_detaches_across_contexts(sse_env, caplog):
    """R4-064 follow-up: the OTel stream span must be owned by the driving
    transport, not by the agent async generator that per-step tasks resume and
    finalize in different contexts (opentelemetry logs and swallows the
    cross-context detach, so only the error records reveal it)."""
    caplog.set_level(logging.WARNING, logger="opentelemetry")

    sid = await _create_session(sse_env)
    _use_provider(sse_env, _ScriptedProvider())
    await _prompt_frames(sse_env, sid)
    assert not _otel_detach_errors(caplog), _otel_detach_errors(caplog)

    # Cancel path: the response generator is finalized by the task whose step
    # was cancelled — a foreign context for any span entered by an earlier step.
    sid = await _create_session(sse_env)
    provider = _BlockingProvider()
    _use_provider(sse_env, provider)
    prompt = next(
        route.endpoint
        for route in sse_env.app.routes
        if route.path == "/sessions/{session_id}/prompt"
    )
    response = await prompt(sid, PromptRequest(text="hello"))
    stream = response.body_iterator
    await anext(stream)
    await anext(stream)
    pending = asyncio.create_task(anext(stream))
    await provider.blocked.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    await asyncio.wait_for(provider.closed_event.wait(), 5)
    assert not _otel_detach_errors(caplog), _otel_detach_errors(caplog)
