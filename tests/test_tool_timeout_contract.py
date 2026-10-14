"""R4-019 E2E contract: terminal's requested timeout reaches the agent loop.

Schema + handler + loop must agree: ``terminal`` advertises ``timeout``
in [1, 300] seconds and validates exactly that range, so the agent loop has
to thread the requested value into its per-call ``asyncio.wait_for`` budget
(bounded by the documented 300 s cap), and a loop-level timeout must reach
the model as a typed ``Error: tool 'terminal' timed out after Ns``. Before
R4-019 the loop hard-cancelled at its static 60 s and the model received the
bare ``"Error: "`` of an argument-less ``TimeoutError``.

Harness (H-SSE / H-DB): the real REST/SSE route, real TurnEngine, real agent
loop, real permission broker/store and a real ``terminal`` subprocess against
the real ``machina_test`` database. Only the provider factory is faked, at
the provider/network boundary; its scripted turns call the REAL terminal tool.

Time compression: the boundary-value cases record the exact budget the loop
hands to ``asyncio.wait_for`` and shorten only the ``ToolRegistry.execute``
wait (the seam under assertion); every other wait stays real. The
requested-boundary case runs uncompressed.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from machina.api import app as api
from machina.core.agent import _TOOL_TIMEOUTS
from machina.core.models import LLMResponse, StreamEvent
from machina.core.provider import LLMProvider

pytestmark = [pytest.mark.integration, pytest.mark.db]


class _TerminalCallingProvider(LLMProvider):
    """Scripted provider: first turn calls the REAL terminal tool, then answers."""

    model = "scripted-r4-019"

    def __init__(
        self, command: str, timeout: int | None = None, final: str = "turn complete"
    ) -> None:
        self.command = command
        self.timeout = timeout
        self.final = final
        self.calls: list[list[dict]] = []

    async def stream_complete(self, messages, **kwargs):
        self.calls.append(messages)
        usage = {"prompt_tokens": 7, "completion_tokens": 5, "total_tokens": 12}
        if len(self.calls) == 1:
            arguments: dict = {"command": self.command}
            if self.timeout is not None:
                arguments["timeout"] = self.timeout
            response = LLMResponse(
                content="",
                model=self.model,
                usage=usage,
                tool_calls=[
                    {
                        "id": "terminal-1",
                        "function": {"name": "terminal", "arguments": json.dumps(arguments)},
                    }
                ],
            )
        else:
            response = LLMResponse(content=self.final, model=self.model, usage=usage)
        yield StreamEvent(type="done", response=response)

    async def close(self) -> None:
        return None


class _ToolBudgetRecorder:
    """Records/shortens only the loop's per-tool ``wait_for`` budget.

    ``asyncio.wait_for`` is process-global, so the wrapper must pass every
    non-tool wait through untouched (the route's turn deadline, engine waits,
    ...). Only coroutines belonging to ``ToolRegistry.execute`` are recorded
    and, when at or above *compress_at*, shortened to *compress_to* so a
    request of e.g. 300 s does not cost 300 s of test wall-clock.
    """

    def __init__(self, compress_at: float, compress_to: float) -> None:
        self.seen: list[float] = []
        self._real = asyncio.wait_for
        self._compress_at = compress_at
        self._compress_to = compress_to

    def install(self):
        async def recorded(coro, timeout):
            code = getattr(coro, "cr_code", None)
            if code is not None and code.co_qualname == "ToolRegistry.execute":
                self.seen.append(timeout)
                if timeout is not None and timeout >= self._compress_at:
                    timeout = self._compress_to
            return await self._real(coro, timeout=timeout)

        return recorded


@pytest.fixture
async def timeout_env(db_pool, tmp_path, monkeypatch):
    """Real app + real test DB; each test fakes only the provider factory."""
    monkeypatch.setattr("machina.db.connection.db", db_pool)
    monkeypatch.setattr(api, "db", db_pool)
    monkeypatch.setattr("machina.core.session.db", db_pool)
    monkeypatch.setattr("machina.core.context.db", db_pool)
    monkeypatch.setattr("machina.core.scheduler.db", db_pool)
    monkeypatch.setattr("machina.core.usage.db", db_pool)
    monkeypatch.setenv("MACHINA_API_KEY", "r4-019-token")
    from machina.core.config import config

    monkeypatch.setattr(config, "workspace_root", str(tmp_path))
    for field in (
        "memory_enabled",
        "rag_enabled",
        "personality_growth_enabled",
        "memory_consolidation_enabled",
        "learning_review_enabled",
        "compression_enabled",
    ):
        monkeypatch.setattr(config, field, False)
    import machina.services as services

    # Post-turn compaction is unrelated background work; never let it spend
    # the scripted provider (same isolation the production smoke fixture uses).
    monkeypatch.setattr(services, "maybe_auto_compact", AsyncMock())
    app = api.create_app()
    created: list[str] = []
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://r4-019",
        headers={"Authorization": "Bearer r4-019-token"},
    ) as client:
        yield SimpleNamespace(client=client, app=app, created=created, monkeypatch=monkeypatch)
    from machina.core.session import session_manager

    for sid in created:
        await session_manager.delete(uuid.UUID(sid))


def _use_provider(env: SimpleNamespace, provider: LLMProvider) -> None:
    """Fake the provider at its factory boundary; nothing else is replaced."""
    env.monkeypatch.setattr("machina.core.provider.get_provider", lambda **kwargs: provider)


async def _full_mode_session(env: SimpleNamespace) -> str:
    """Create a real session and activate a durable full-mode grant (real DB)."""
    response = await env.client.post("/api/v1/sessions", json={"title": "R4-019 contract"})
    assert response.status_code == 200, response.text
    sid = response.json()["session"]["id"]
    env.created.append(sid)
    from machina.core.session import session_manager
    from machina.core.session_mode import activate_durable_mode

    session = await session_manager.get(uuid.UUID(sid))
    assert session is not None
    await activate_durable_mode(sid, session.agent_id, "full")
    return sid


async def _prompt_frames(env: SimpleNamespace, sid: str) -> tuple[str, list[dict]]:
    response = await env.client.post(
        f"/api/v1/sessions/{sid}/prompt", json={"text": "go", "protocolVersion": 1}
    )
    assert response.status_code == 200, response.text
    frames = [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    return response.text, frames


def _tool_results(frames: list[dict]) -> list[dict]:
    return [frame for frame in frames if frame["type"] == "tool_result"]


async def test_requested_timeout_cancels_at_boundary_and_reaches_model(timeout_env):
    """(a) request 2 s -> cancelled ~2 s (not the 60 s default) and (b) typed result."""
    sid = await _full_mode_session(timeout_env)
    provider = _TerminalCallingProvider(command="sleep 30", timeout=2)
    _use_provider(timeout_env, provider)

    start = time.monotonic()
    body, frames = await _prompt_frames(timeout_env, sid)
    elapsed = time.monotonic() - start

    results = _tool_results(frames)
    assert len(results) == 1, results
    assert results[0]["tool"] == "terminal"
    assert results[0]["result"] == "Error: tool 'terminal' timed out after 2s"
    # (a) honoured at the requested bound, nowhere near the legacy 60 s cap.
    assert 1.5 <= elapsed < 15.0, f"requested 2 s bound not honoured: {elapsed:.1f}s"
    # (b) model-visible: the second provider call carries the typed result.
    assert len(provider.calls) == 2
    assert "tool 'terminal' timed out after 2s" in json.dumps(provider.calls[1])
    # The turn itself still completes normally with the follow-up answer.
    assert frames[-1]["type"] == "done"
    assert frames[-1]["content"] == provider.final
    assert "error" not in [frame["type"] for frame in frames]
    assert body.endswith("data: [DONE]\n\n")


async def test_default_path_is_unchanged(timeout_env):
    """(c) no requested timeout -> static 60 s budget and healthy execution."""
    assert _TOOL_TIMEOUTS["terminal"] == 60.0
    sid = await _full_mode_session(timeout_env)
    provider = _TerminalCallingProvider(command="echo r4-019-default")
    _use_provider(timeout_env, provider)

    _body, frames = await _prompt_frames(timeout_env, sid)
    results = _tool_results(frames)
    assert len(results) == 1
    assert "r4-019-default" in results[0]["result"]
    assert "timed out" not in results[0]["result"]
    assert frames[-1]["type"] == "done"


@pytest.mark.parametrize("requested", [59, 60, 90, 300])
async def test_boundary_values_map_to_the_requested_budget(timeout_env, monkeypatch, requested):
    """(d) 59/60/90/300: the loop budget equals the request (cap 300)."""
    recorder = _ToolBudgetRecorder(compress_at=1.0, compress_to=0.5)
    monkeypatch.setattr("machina.core.agent.asyncio.wait_for", recorder.install())
    sid = await _full_mode_session(timeout_env)
    provider = _TerminalCallingProvider(command="sleep 30", timeout=requested)
    _use_provider(timeout_env, provider)

    _body, frames = await _prompt_frames(timeout_env, sid)
    # The loop handed the REAL wait_for exactly the requested budget.
    assert recorder.seen == [float(requested)], recorder.seen
    results = _tool_results(frames)
    assert len(results) == 1
    assert results[0]["result"] == f"Error: tool 'terminal' timed out after {requested}s"


@pytest.mark.parametrize("requested", [0, -3, 1000])
async def test_out_of_schema_requests_keep_the_handlers_typed_error(
    timeout_env, monkeypatch, requested
):
    """Requested values outside 1..300 fail the handler, never a silent cap/kill."""
    recorder = _ToolBudgetRecorder(compress_at=1.0, compress_to=0.5)
    monkeypatch.setattr("machina.core.agent.asyncio.wait_for", recorder.install())
    sid = await _full_mode_session(timeout_env)
    provider = _TerminalCallingProvider(command="sleep 30", timeout=requested)
    _use_provider(timeout_env, provider)

    _body, frames = await _prompt_frames(timeout_env, sid)
    results = _tool_results(frames)
    assert len(results) == 1
    assert results[0]["result"] == (
        f"Error: timeout must be an integer between 1 and 300, got {requested}"
    )
    # The budget clamped to [1, 300] so the handler (not wait_for) reported it.
    assert recorder.seen == [float(min(max(requested, 1), 300))], recorder.seen
