"""F07 evidence: HTTP streaming must carry growth/RSI ContextVars across steps.

Finding F07 (``docs/verification/CODEBASE_REVIEW_FOLLOWUP_2026-10-15.md``):
``streaming._stream_session`` pulls every agent-generator step in a fresh
asyncio Task (``_owned_step``).  ``agent._prepare_context`` sets the growth
episode / RSI ContextVars inside the FIRST step's task, so later steps (and the
``done`` step that runs ``_complete_growth``) read the *default* — growth
episodes never complete.

The fix is the same class as R4-010: the *driver* owns the ContextVar lifetime
so every step task observes the same state.  Here the whole turn's step tasks
share one execution Context, so a value written while preparing survives into
the step that reads it at completion.

These tests drive the real route (``create_app`` + ASGITransport) against the
real test database.  Only the agent is faked, at the builder boundary, so the
route's per-step ``_owned_step`` driver is exercised unchanged.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from machina.api import app as api
from machina.core.models import AgentResponse, StreamEvent

pytestmark = [pytest.mark.integration, pytest.mark.db]

# The three ContextVars F07 loses between steps.  They are the agent's private
# growth/RSI state (machina/core/agent.py); the route must not need to know
# their values, only preserve the context that carries them.
_EPISODE = uuid.uuid4()
_RSI_CHOICE = {"tool_weights": {"probe_tool": 0.75}, "reason": "f07"}
_RSI_SKILL = "probe-skill"


class _GrowthProbeAgent:
    """Fake agent that mimics ``_prepare_context`` → tool step → ``done``.

    The real agent (``instrument_stream``) reads the growth episode at ``done``
    to call ``_complete_growth``.  This fake records what a later step observes
    so the test can assert the state outlived the step that created it.
    """

    _owns_provider = False  # engine.close() must not try to close a provider.

    def __init__(self) -> None:
        self.observed_episode: object = "UNSET"
        self.observed_rsi_choice: object = "UNSET"
        self.observed_rsi_skills: object = "UNSET"

    async def run_stream(self, session_id, text, verbose=False, **kwargs):
        from machina.core.agent import _growth_episode, _rsi_choice, _rsi_skills

        # Step 1 — mimic ``_prepare_context``: the growth episode and RSI
        # choice are decided and set while preparing the turn.
        _growth_episode.set(_EPISODE)
        _rsi_choice.set(dict(_RSI_CHOICE))
        _rsi_skills.set([])
        yield StreamEvent(type="text", content="preparing ")

        # Step 2 — mimic a tool step recording RSI skill usage
        # (agent.py reads ``_rsi_skills`` with a None guard).
        if _rsi_skills.get() is not None:
            _rsi_skills.get().append(_RSI_SKILL)
        yield StreamEvent(type="token_usage", tokens_used=4)

        # Step 3 — ``done``: this is where ``_complete_growth`` runs for real,
        # reading the episode/RSI state set in step 1.
        self.observed_episode = _growth_episode.get()
        self.observed_rsi_choice = _rsi_choice.get()
        self.observed_rsi_skills = list(_rsi_skills.get() or [])
        yield StreamEvent(
            type="done",
            response=AgentResponse(
                content="preparing done.",
                tokens_used=3,
                iterations=1,
            ),
        )

    async def close(self) -> None:  # pragma: no cover - defensive parity
        return None


@pytest.fixture
async def stream_env(db_pool, monkeypatch):
    """Real app + real test DB; the agent builder is faked at its boundary."""
    monkeypatch.setattr("machina.db.connection.db", db_pool)
    monkeypatch.setattr(api, "db", db_pool)
    monkeypatch.setattr("machina.core.session.db", db_pool)
    monkeypatch.setattr("machina.core.context.db", db_pool)
    monkeypatch.setattr("machina.core.scheduler.db", db_pool)
    monkeypatch.setattr("machina.core.usage.db", db_pool)
    monkeypatch.setenv("MACHINA_API_KEY", "f07-token")
    import machina.services as services

    # Post-turn compaction is unrelated background work; never let it run here.
    monkeypatch.setattr(services, "maybe_auto_compact", AsyncMock())
    app = api.create_app()
    created: list[str] = []

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://f07",
        headers={"Authorization": "Bearer f07-token"},
    ) as client:
        yield SimpleNamespace(
            client=client, app=app, created=created, monkeypatch=monkeypatch, pool=db_pool
        )
    from machina.core.session import session_manager

    for sid in created:
        await session_manager.delete(uuid.UUID(sid))


async def _create_session(env: SimpleNamespace) -> str:
    response = await env.client.post("/api/v1/sessions", json={"title": "F07 growth scope"})
    assert response.status_code == 200, response.text
    sid = response.json()["session"]["id"]
    env.created.append(sid)
    return sid


async def test_growth_state_survives_across_stream_steps(stream_env):
    """The done step must see state the first step set (F07)."""
    agent = _GrowthProbeAgent()

    # The route imports the builder at call time, so patching the module
    # attribute is enough; nothing else about the route is replaced.
    async def _build_probe(session):
        return agent

    stream_env.monkeypatch.setattr(
        "machina.core.agent_factory.build_agent_for_session", _build_probe
    )
    sid = await _create_session(stream_env)

    response = await stream_env.client.post(
        f"/api/v1/sessions/{sid}/prompt",
        json={"text": "hello", "protocolVersion": 1},
    )
    assert response.status_code == 200, response.text
    frames = [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    assert frames[-1]["type"] == "done"

    # RED before the fix: every step ran in its own Context copy, so the
    # done step read the ContextVar *defaults* ("UNSET"/None), not step 1's
    # values — the real ``_complete_growth`` would then skip the episode.
    assert agent.observed_episode == _EPISODE, (
        "growth episode did not survive from preparation into the done step"
    )
    assert agent.observed_rsi_choice == _RSI_CHOICE, (
        "RSI choice did not survive from preparation into the done step"
    )
    assert agent.observed_rsi_skills == [_RSI_SKILL], (
        "RSI skills did not survive from preparation into the done step"
    )
