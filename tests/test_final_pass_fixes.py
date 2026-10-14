"""Final adversarial pass fixes (M1, M2-adjacent pins, N1–N6).

- M1: run_stream failure paths schedule memory consolidation like run().
- N2: a failed triage hook clears any stale label on a reused agent.
- N3: the dead gateway `_turn_locks` attribute is gone.
- N5: packaging excludes runtime telemetry state.
- N6: learn_directory gets a 120s tool timeout.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def _agent(monkeypatch):
    import machina.core.agent as mod
    from machina.core.agent import ReActAgent

    agent = ReActAgent(provider=SimpleNamespace(model="fake", close=AsyncMock()))
    session = SimpleNamespace(
        context_budget=10_000_000,
        context_window=10_000_000,
        goal="",
        agent_id="harness",
        state={},
    )
    mock_sm = AsyncMock()
    mock_sm.get = AsyncMock(return_value=session)
    mock_sm.update_activity = AsyncMock()
    monkeypatch.setattr(mod, "session_manager", mock_sm)
    monkeypatch.setattr(mod, "context_manager", AsyncMock())
    monkeypatch.setattr(
        ReActAgent,
        "_prepare_context",
        AsyncMock(return_value=(session, [{"role": "user", "content": "hi"}])),
    )
    monkeypatch.setattr(ReActAgent, "_flush_pending_chunks", AsyncMock())
    calls: list = []
    monkeypatch.setattr(
        ReActAgent, "_schedule_memory_consolidation", lambda self, sid: calls.append(sid)
    )
    return agent, calls


# ─── M1: run_stream failed-path catch-up ────────────────────────────────────


@pytest.mark.asyncio
async def test_stream_llm_error_schedules_consolidation(monkeypatch, caplog):
    import logging

    agent, calls = _agent(monkeypatch)

    async def boom(self, **_kwargs):
        raise RuntimeError("provider down")
        yield  # pragma: no cover — async generator shape

    monkeypatch.setattr(type(agent), "_stream_llm_with_retry", boom)
    sid = uuid.uuid4()
    with caplog.at_level(logging.ERROR, logger="machina.core.agent"):
        events = [e async for e in agent.run_stream(sid, "hi", verbose=False)]
    assert any(e.type == "done" and e.response.outcome == "failed" for e in events)
    assert calls == [sid], "run_stream LLM-error path must schedule consolidation (M1)"
    # W7: the top-level provider-failure boundary logs the sanitized traceback.
    failures = [r for r in caplog.records if r.getMessage() == "LLM stream ultimately failed"]
    assert failures and "RuntimeError" in failures[0].exc_text


@pytest.mark.asyncio
async def test_run_llm_error_logs_traceback(monkeypatch, caplog):
    """W7: run()'s top-level provider-failure boundary logs the traceback."""
    import logging

    agent, calls = _agent(monkeypatch)

    async def boom(self, **_kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(type(agent), "_call_llm_with_retry", boom)
    sid = uuid.uuid4()
    with caplog.at_level(logging.ERROR, logger="machina.core.agent"):
        response = await agent.run(sid, "hi", verbose=False)
    assert response.outcome == "failed"
    assert calls == [sid]
    failures = [r for r in caplog.records if r.getMessage() == "LLM call ultimately failed"]
    assert failures and "RuntimeError" in failures[0].exc_text


@pytest.mark.asyncio
async def test_stream_context_exceeded_schedules_consolidation(monkeypatch):
    agent, calls = _agent(monkeypatch)
    import machina.core.agent as mod

    monkeypatch.setattr(mod, "_request_context_tokens", lambda *a, **k: 10**9)
    sid = uuid.uuid4()
    events = [e async for e in agent.run_stream(sid, "hi", verbose=False)]
    assert any(
        e.type == "done" and "exceeds the model context window" in e.response.content
        for e in events
    )
    assert calls == [sid], "run_stream context-exceeded path must schedule consolidation (M1)"


# ─── N2: stale triage cleared on hook failure ───────────────────────────────


def test_failed_triage_hook_clears_stale_label(monkeypatch):
    from machina.controller import wiring

    agent = SimpleNamespace(_last_triage="debug")

    def boom(_msg):
        raise RuntimeError("triage unavailable")

    monkeypatch.setattr("machina.controller.triage.resolve_task", boom)
    wiring.record_triage(agent, "hi")
    assert agent._last_triage is None, "stale label must not survive a failed hook (N2)"


# ─── N3 / N6: gateway + tool timeout ────────────────────────────────────────


def test_gateway_has_no_dead_turn_locks():
    from machina.gateway.server import Gateway

    gw = Gateway(lambda _frame: None)
    assert not hasattr(gw, "_turn_locks")


def test_learn_directory_has_an_explicit_timeout():
    from machina.core.agent import _TOOL_TIMEOUTS

    assert _TOOL_TIMEOUTS["learn_directory"] == 120.0


# ─── N5: runtime telemetry hygiene ──────────────────────────────────────────


def test_docker_context_and_docs_handle_telemetry_state():
    """N5: hatch force-include can't exclude telemetry from wheels (verified),
    so the enforced contract is: docker contexts prune it and pyproject
    documents the limitation honestly."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    dockerignore = (root / ".dockerignore").read_text(encoding="utf-8")
    assert "skills/telemetry.json" in dockerignore
    assert "skills/telemetry.json.lock" in dockerignore
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert "force-include bypasses" in pyproject, "limitation must be documented, not dead config"
