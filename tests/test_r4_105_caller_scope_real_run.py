"""R4-105(a): true caller-scope binding through a REAL agent run.

Replaces the AH-018 vacuum (`tests/test_handoff_regressions.py:471-519`
seeded the ContextVars by hand and never invoked the probe: ``seen == {}``).
This suite drives the real ``ReActAgent.run()`` loop, the real permission
broker, the real context/session/usage stores on ``machina_test``, and the
real ``machina.tools.agents.active_agent_scope()`` call — including its DB
ownership check against the ``sessions`` row. The LLM is faked at the
provider boundary only; nothing on the fixed seam is patched.

Proof shape (RED-on-unfixed, see the R4-105 evidence doc): with the two
``current_session_id.set`` / ``current_agent_id.set`` lines removed from
``agent.py::_run_one``, the probes observe no scope and the assertions fail;
with the binding in place every concurrent tool task sees its own
(agent_id, session_id) and no other run's scope.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest
import pytest_asyncio

from machina.core.models import LLMResponse

pytestmark = pytest.mark.db


@pytest_asyncio.fixture
async def scope_run_db():
    """Wire the shared ``db`` singleton to a PRIVATE test schema.

    Deliberately independent of the ``db_pool`` fixture: that fixture runs the
    full public ``schema.sql`` (ALTER TABLE ...) per test, which deadlocks
    against other workspace processes' concurrent data writes on the shared
    test database. A private schema's DDL never locks public relations.
    """
    dsn = os.environ.get("MACHINA_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("MACHINA_TEST_DATABASE_URL not set")
    from machina.db.connection import Database, db
    from tests.support.schema import isolated_schema

    administrator = Database(dsn=dsn)
    await administrator.connect()
    try:
        async with isolated_schema(administrator) as (_schema, database):
            original_pool, original_dsn = db._pool, db.dsn
            db._pool = database._pool
            db.dsn = database.dsn
            try:
                yield database
            finally:
                db._pool = original_pool
                db.dsn = original_dsn
    finally:
        await administrator.close()


@pytest.fixture(autouse=True)
def scope_cleanup():
    """Reset broker execution state this suite can touch."""
    from machina.permissions.broker import clear_execution_context, set_approval_handler

    set_approval_handler(None)
    try:
        yield
    finally:
        set_approval_handler(None)
        clear_execution_context()


@pytest.fixture
def probe_tools():
    """Register real probe tools on the production registry; remove after."""
    from machina.tools import agents as agents_mod
    from machina.tools.base import registry

    names: list[str] = []

    def make(name: str, observations: list[dict]) -> None:
        @registry.register(name=name, description="R4-105 caller-scope probe")
        async def _probe() -> str:
            agent_id, session_id = await agents_mod.active_agent_scope()
            observations.append({"tool": name, "agent_id": agent_id, "session_id": session_id})
            return f"scope {agent_id} {session_id}"

        names.append(name)

    yield make
    for name in names:
        registry._tools.pop(name, None)
    registry._definitions_cache = None
    registry._result_cache.clear()


class _TwoStepProvider:
    """First call returns the scripted tool calls, then a final message."""

    model = "r4-105-probe"

    def __init__(self, tool_calls: list[dict]):
        self._tool_calls = tool_calls
        self.calls: list[dict] = []

    async def complete(self, **kwargs) -> LLMResponse:
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            return LLMResponse(
                content="",
                model=self.model,
                usage={"total_tokens": 1},
                tool_calls=self._tool_calls,
            )
        return LLMResponse(content="final", model=self.model, usage={"total_tokens": 1})


def _tool_call(name: str, call_id: str) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps({})},
    }


def _quiet_agent_config(monkeypatch):
    """Turn off retrieval/side-effect subsystems this proof does not exercise."""
    from machina.core.config import config

    for field in (
        "memory_enabled",
        "personality_growth_enabled",
        "rag_enabled",
        "memory_consolidation_enabled",
    ):
        monkeypatch.setattr(config, field, False)


@pytest.mark.asyncio
async def test_scope_probe_sees_real_caller_scope_in_agent_run(
    scope_run_db, monkeypatch, probe_tools
):
    """A per-tool probe calls active_agent_scope() for real during run()."""
    from machina.core.agent import ReActAgent
    from machina.core.exceptions import ToolError
    from machina.core.session import session_manager
    from machina.tools import agents as agents_mod

    _quiet_agent_config(monkeypatch)
    session = await session_manager.create(title="r4-105-scope", agent_id="harness")
    observations: list[dict] = []
    probe_tools("r4_105_scope_probe_a", observations)
    probe_tools("r4_105_scope_probe_b", observations)

    provider = _TwoStepProvider(
        [
            _tool_call("r4_105_scope_probe_a", "call-a"),
            _tool_call("r4_105_scope_probe_b", "call-b"),
        ]
    )
    agent = ReActAgent(
        provider=provider,
        agent_id="harness",
        allowed_tools=["r4_105_scope_probe_a", "r4_105_scope_probe_b"],
    )
    response = await agent.run(session.id, "probe scope", verbose=False)

    assert response.content == "final"
    assert len(provider.calls) == 2, "the loop must continue after tool results"
    assert sorted(o["tool"] for o in observations) == [
        "r4_105_scope_probe_a",
        "r4_105_scope_probe_b",
    ]
    for observed in observations:
        # active_agent_scope() itself re-checked sessions.agent_id in the real
        # database; these values can only be bound by the per-tool caller
        # scope in agent.py::_run_one.
        assert observed["agent_id"] == "harness"
        assert observed["session_id"] == session.id
    # Scope never leaks past the tool task.
    assert agents_mod.current_session_id.get() is None
    assert agents_mod.current_agent_id.get() is None
    # Negative control in the same process: with no binding, the same
    # production call the probes made raises (so the positive proof above is
    # not vacuous).
    with pytest.raises(ToolError, match="active agent session"):
        await agents_mod.active_agent_scope()


@pytest.mark.asyncio
async def test_concurrent_runs_keep_speaker_scope_isolated_per_tool(
    scope_run_db, monkeypatch, probe_tools
):
    """Two concurrent runs: every tool task sees ONLY its own run's scope."""
    from machina.core.agent import ReActAgent
    from machina.core.session import session_manager

    _quiet_agent_config(monkeypatch)
    session_harness = await session_manager.create(title="r4-105-iso-h", agent_id="harness")
    session_researcher = await session_manager.create(title="r4-105-iso-r", agent_id="researcher")
    observations: list[dict] = []
    probe_tools("r4_105_scope_probe_c", observations)
    probe_tools("r4_105_scope_probe_d", observations)

    agent_harness = ReActAgent(
        provider=_TwoStepProvider([_tool_call("r4_105_scope_probe_c", "call-c")]),
        agent_id="harness",
        allowed_tools=["r4_105_scope_probe_c"],
    )
    agent_researcher = ReActAgent(
        provider=_TwoStepProvider([_tool_call("r4_105_scope_probe_d", "call-d")]),
        agent_id="researcher",
        allowed_tools=["r4_105_scope_probe_d"],
    )
    responses = await asyncio.gather(
        agent_harness.run(session_harness.id, "probe h", verbose=False),
        agent_researcher.run(session_researcher.id, "probe r", verbose=False),
    )
    assert [r.content for r in responses] == ["final", "final"]

    by_tool = {o["tool"]: o for o in observations}
    assert set(by_tool) == {"r4_105_scope_probe_c", "r4_105_scope_probe_d"}
    assert (
        by_tool["r4_105_scope_probe_c"]["agent_id"],
        by_tool["r4_105_scope_probe_c"]["session_id"],
    ) == (
        "harness",
        session_harness.id,
    )
    assert (
        by_tool["r4_105_scope_probe_d"]["agent_id"],
        by_tool["r4_105_scope_probe_d"]["session_id"],
    ) == (
        "researcher",
        session_researcher.id,
    )
