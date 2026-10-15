"""R5-C (finding 7): scheduled jobs must honor the agent selected in the job.

Finding: ``JobRunner._build_agent`` passes the owned session unchanged to
``build_agent_for_session``, which selects ``session.agent_id``.
``job.agent_name`` is used only when the session cannot be loaded. A job
configured for a restricted specialist on a harness-owned session therefore
executes the harness definition/tools instead, while job metadata reports
the specialist.

Fix direction (per FIX_PLAN R5-C): honor ``job.agent_name`` within the
session's authority restrictions (prefer honor-with-restrictions over
reject-at-creation).

Seams:
- unit tests use the REAL ``build_agent_for_session`` with only the
  network/provider edge stubbed (``get_provider``, RAG pipeline) plus the
  session-read seam (``session_manager.get_fresh``) and the agent-definition
  lookup returning the real builtins. No DB.
- the E2E test runs a real scheduler tick (claim -> turn -> build -> run ->
  finish) against a self-managed ``isolated_schema`` private schema, with
  only the LLM call itself stubbed (``ReActAgent.run``).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from machina.core.agent_def import BUILTIN_AGENTS
from machina.core.models import AgentResponse, Session

pytestmark = pytest.mark.integration

RESEARCHER_TOOLS = sorted(BUILTIN_AGENTS["researcher"].tools)


def _session(agent_id: str, authority: dict | None = None) -> Session:
    now = datetime.now(UTC)
    return Session(
        id=uuid.uuid4(),
        agent_id=agent_id,
        created_at=now,
        last_activity=now,
        delegation_authority=dict(authority or {}),
    )


def _stub_factory_edge(monkeypatch) -> SimpleNamespace:
    """Stub only the network/provider edge of agent construction."""
    from machina.core import agent_factory

    stub = SimpleNamespace(model="stub-model")
    monkeypatch.setattr("machina.core.provider.get_provider", lambda **kwargs: stub)
    monkeypatch.setattr(agent_factory, "_shared_rag_pipeline", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "machina.core.agent_def.agent_registry.get",
        AsyncMock(side_effect=lambda name: BUILTIN_AGENTS.get(name)),
    )
    return stub


def _stub_session_read(monkeypatch, session: Session) -> None:
    monkeypatch.setattr(
        "machina.core.session.session_manager.get_fresh",
        AsyncMock(return_value=session),
    )


async def _build(monkeypatch, agent_name: str, session: Session):
    from machina.core.scheduler import JobRunner

    _stub_factory_edge(monkeypatch)
    _stub_session_read(monkeypatch, session)
    return await JobRunner()._build_agent(agent_name, session_id=session.id)


async def test_job_specialist_identity_honored_on_owned_session(monkeypatch):
    """RED (finding 7): a researcher job on a harness session must not run harness."""
    agent = await _build(monkeypatch, "researcher", _session("harness"))
    assert agent.agent_id == "researcher"
    assert sorted(agent.allowed_tools or []) == RESEARCHER_TOOLS


async def test_job_identity_keeps_session_authority_caps(monkeypatch):
    """Honor-with-restrictions: session tool caps still intersect the job identity."""
    agent = await _build(
        monkeypatch,
        "researcher",
        _session("harness", {"tools": ["read_file", "write_file"]}),
    )
    assert agent.agent_id == "researcher"
    # write_file is outside the researcher definition; read_file is in both.
    assert sorted(agent.allowed_tools or []) == ["read_file"]


async def test_matching_identity_unchanged(monkeypatch):
    """No behavior change when the job already names the session's agent."""
    agent = await _build(monkeypatch, "harness", _session("harness"))
    assert agent.agent_id == "harness"
    assert agent.allowed_tools is None


# ─── E2E: real scheduler tick + real session row ─────────────────────────────


@pytest.mark.db
async def test_e2e_scheduler_tick_runs_job_agent_identity(db_pool, monkeypatch):
    """Real claim/turn/build/run/finish: the tick executes the JOB's specialist."""
    from tests.support.schema import isolated_schema

    async with isolated_schema(db_pool) as (_schema, database):
        monkeypatch.setattr("machina.db.connection.db", database)
        monkeypatch.setattr("machina.core.session.db", database)
        monkeypatch.setattr("machina.core.scheduler.db", database)
        monkeypatch.setattr("machina.core.agent_def.db", database)

        _stub_factory_edge(monkeypatch)

        seen: dict = {}

        async def fake_run(self, session_id, prompt, verbose=False):
            seen["agent_id"] = self.agent_id
            seen["allowed_tools"] = list(self.allowed_tools or [])
            seen["session_id"] = session_id
            return AgentResponse(content="done", tool_calls=[], tokens_used=5, iterations=1)

        monkeypatch.setattr("machina.core.agent.ReActAgent.run", fake_run)

        from machina.core.scheduler import JobRunner, job_store
        from machina.core.session import SessionManager

        session = await SessionManager().create(
            title="r5-c identity",
            agent_id="harness",
            delegation_authority={"tools": ["read_file", "write_file"]},
        )
        job = await job_store.create(
            name="r5-c researcher job",
            kind="interval",
            session_id=session.id,
            prompt="survey the repo",
            interval_seconds=10,
            agent_name="researcher",
        )
        await database.execute(
            "UPDATE jobs SET next_run_at = now() - interval '1 second' WHERE id = $1",
            job.id,
        )

        assert await JobRunner().run_due_once() is True

        assert seen["agent_id"] == "researcher", seen
        assert seen["allowed_tools"] == ["read_file"], seen
        assert seen["session_id"] == session.id

        finished = await job_store.get(job.id)
        assert finished is not None and finished.run_count == 1
        assert finished.status == "idle" and finished.last_error is None
