"""Phase 5 multi-agent: definitions, registry, orchestrator, tools, gateway methods."""

from __future__ import annotations

import os
import uuid

import pytest

from ah.core.agent import ReActAgent
from ah.core.agent_def import BUILTIN_AGENTS, AgentDef, agent_registry
from ah.core.models import AgentResponse
from ah.gateway.server import INVALID_PARAMS, NOT_FOUND
from tests.test_gateway import Harness

TEST_DSN = os.environ.get("AGENT_HARNESS_TEST_DATABASE_URL", "")
needs_db = pytest.mark.skipif(not TEST_DSN, reason="AGENT_HARNESS_TEST_DATABASE_URL not set")


@pytest.fixture
async def connected_db():
    """Connect the shared db for tests that call the registry/orchestrator directly
    (the gateway Harness connects via initialize, so its tests don't need this)."""
    from ah.db.connection import db

    await db.connect()
    yield
    await db.close()


# ─── definitions / tool allow-list (no DB) ────────────────────────────────────


def test_builtin_agents_have_prompts_and_tool_scopes():
    assert set(BUILTIN_AGENTS) == {"harness", "researcher", "coder"}
    assert "write_file" not in BUILTIN_AGENTS["researcher"].tools
    assert "write_file" in BUILTIN_AGENTS["coder"].tools
    assert BUILTIN_AGENTS["harness"].tools == []  # unrestricted


def test_agent_tool_allowlist_filters_definitions():
    import ah.tools  # noqa: F401 — ensure tools are registered

    unrestricted = ReActAgent.__new__(ReActAgent)
    unrestricted.allowed_tools = None
    restricted = ReActAgent.__new__(ReActAgent)
    restricted.allowed_tools = ["read_file", "web_search"]

    all_names = {d.name for d in unrestricted._tool_defs()}
    scoped = {d.name for d in restricted._tool_defs()}
    assert {"read_file", "web_search"} <= all_names
    assert scoped == {"read_file", "web_search"}


# ─── registry / orchestrator / tools (real DB) ────────────────────────────────


class FakeAgent:
    """Deterministic stand-in: echoes the task back as the response."""

    def __init__(self, definition: AgentDef) -> None:
        self.definition = definition

    async def run(self, session_id, user_message, verbose=True) -> AgentResponse:
        return AgentResponse(
            content=f"[{self.definition.name}] handled: {user_message[:60]}",
            tool_calls=[],
            tokens_used=11,
            iterations=1,
        )


@needs_db
@pytest.mark.usefixtures("connected_db")
class TestRegistry:
    async def test_save_list_get_delete(self):
        name = f"tester-{uuid.uuid4().hex[:8]}"
        saved = await agent_registry.save(
            AgentDef(name=name, description="a test agent", tools=["read_file"], max_iterations=4)
        )
        assert saved.source == "db" and saved.tools == ["read_file"]

        fetched = await agent_registry.get(name)
        assert fetched is not None and fetched.max_iterations == 4

        names = {a.name for a in await agent_registry.list()}
        assert name in names and {"harness", "researcher", "coder"} <= names

        assert await agent_registry.delete(name) is True
        assert await agent_registry.get(name) is None

    async def test_stored_agent_overrides_builtin(self):
        try:
            await agent_registry.save(AgentDef(name="researcher", description="overridden"))
            fetched = await agent_registry.get("researcher")
            assert fetched.description == "overridden" and fetched.source == "db"
        finally:
            await agent_registry.delete("researcher")
        # Falls back to the built-in after deletion.
        assert (await agent_registry.get("researcher")).source == "builtin"


@needs_db
@pytest.mark.usefixtures("connected_db")
class TestOrchestrator:
    @pytest.fixture
    def orch(self):
        from ah.core.orchestrator import Orchestrator

        return Orchestrator(agent_factory=FakeAgent)

    async def test_delegate_records_message_and_child_session(self, orch):
        parent = await _parent_session()
        result = await orch.delegate("researcher", "find the latest release", parent_session_id=parent)
        assert result.status == "complete"
        assert "handled: find the latest release" in result.response
        assert result.tokens == 11

        history = await orch.history(parent)
        assert len(history) == 1
        assert history[0]["toAgent"] == "researcher" and history[0]["status"] == "complete"
        assert history[0]["tokens"] == 11

    async def test_unknown_agent_raises(self, orch):
        from ah.core.orchestrator import AgentNotFoundError

        with pytest.raises(AgentNotFoundError):
            await orch.delegate("nobody", "task")

    async def test_sequential_passes_prior_results_forward(self, orch):
        captured: list[str] = []

        class Recorder(FakeAgent):
            async def run(self, session_id, user_message, verbose=True):
                captured.append(user_message)
                return await super().run(session_id, user_message, verbose)

        from ah.core.orchestrator import Orchestrator

        orch = Orchestrator(agent_factory=Recorder)
        results = await orch.run_sequential([("researcher", "gather facts"), ("coder", "write code")])
        assert len(results) == 2
        assert "## Results so far" in captured[1]  # second step sees the first's output
        assert "## Results so far" not in captured[0]

    async def test_parallel_runs_all(self, orch):
        results = await orch.run_parallel([("researcher", "a"), ("coder", "b"), ("harness", "c")])
        assert {r.agent for r in results} == {"researcher", "coder", "harness"}
        assert all(r.status == "complete" for r in results)


@needs_db
@pytest.mark.usefixtures("connected_db")
class TestDelegateTool:
    async def test_delegate_tool_uses_orchestrator(self, monkeypatch):
        import ah.tools  # noqa: F401
        from ah.core import orchestrator as orch_mod
        from ah.tools.base import registry

        monkeypatch.setattr(orch_mod.orchestrator, "_agent_factory", FakeAgent)
        out = await registry.execute("delegate", agent="researcher", task="summarize the readme")
        assert "[researcher responded]" in out
        assert "handled: summarize the readme" in out

    async def test_delegate_tool_unknown_agent(self):
        import ah.tools  # noqa: F401
        from ah.core.exceptions import ToolError
        from ah.tools.base import registry

        with pytest.raises(ToolError, match="no agent named"):
            await registry.execute("delegate", agent="ghost", task="x")

    async def test_list_agents_tool(self):
        import ah.tools  # noqa: F401
        from ah.tools.base import registry

        out = await registry.execute("list_agents")
        assert "researcher:" in out and "coder:" in out


@needs_db
class TestGatewayAgents:
    @pytest.fixture
    async def h(self, monkeypatch):
        from ah.core import orchestrator as orch_mod

        monkeypatch.setattr(orch_mod.orchestrator, "_agent_factory", FakeAgent)
        harness = Harness()
        assert "result" in await harness.call("initialize")
        yield harness
        await harness.gateway.close()

    async def test_list_save_delete(self, h):
        name = f"gw-agent-{uuid.uuid4().hex[:8]}"
        listed = (await h.call("agents.list"))["result"]["agents"]
        assert {"harness", "researcher", "coder"} <= {a["name"] for a in listed}

        saved = (await h.call("agents.save", {"name": name, "description": "x", "tools": ["read_file"]}))["result"]
        assert saved["agent"]["name"] == name and saved["agent"]["tools"] == ["read_file"]

        assert (await h.call("agents.delete", {"name": name}))["result"] == {"deleted": True}
        assert (await h.call("agents.delete", {"name": name}))["error"]["code"] == NOT_FOUND

    async def test_cannot_delete_builtin_or_use_reserved_name(self, h):
        assert (await h.call("agents.delete", {"name": "harness"}))["error"]["code"] == INVALID_PARAMS
        assert (await h.call("agents.save", {"name": "orchestrator"}))["error"]["code"] == INVALID_PARAMS

    async def test_run_sequential_and_history(self, h):
        parent = (await h.call("session.create", {"title": "orchestration"}))["result"]["session"]["id"]
        run = await h.call(
            "agents.run",
            {
                "sessionId": parent,
                "mode": "sequential",
                "steps": [
                    {"agent": "researcher", "task": "research"},
                    {"agent": "coder", "task": "implement"},
                ],
            },
        )
        results = run["result"]["results"]
        assert [r["agent"] for r in results] == ["researcher", "coder"]
        assert all(r["status"] == "complete" for r in results)

        history = (await h.call("agents.history", {"sessionId": parent}))["result"]["messages"]
        assert len(history) == 2

    async def test_run_validates_steps_and_mode(self, h):
        assert (await h.call("agents.run", {"steps": []}))["error"]["code"] == INVALID_PARAMS
        bad = await h.call("agents.run", {"steps": [{"agent": "x"}]})
        assert bad["error"]["code"] == INVALID_PARAMS
        bad_mode = await h.call("agents.run", {"mode": "race", "steps": [{"agent": "harness", "task": "t"}]})
        assert bad_mode["error"]["code"] == INVALID_PARAMS

    async def test_run_unknown_agent(self, h):
        response = await h.call("agents.run", {"steps": [{"agent": "ghost", "task": "t"}]})
        assert response["error"]["code"] == NOT_FOUND


async def _parent_session() -> uuid.UUID:
    from ah.core.session import session_manager

    return (await session_manager.create(title="parent")).id
