"""ECC specialists are shipped defaults, scoped tools, and ordinary delegated agents."""

import hashlib
import json
import uuid
from importlib.resources import files
from unittest.mock import AsyncMock

import pytest

from machina.core.agent_def import BUILTIN_AGENTS, AgentRegistry
from machina.core.models import Session


def test_importer_copies_only_agent_data_and_license_byte_for_byte(tmp_path):
    from tools.vendor_ecc_agents import PIN, vendor

    source = tmp_path / "source"
    (source / "agents").mkdir(parents=True)
    raw = b"---\nname: fixture\ndescription: Review fixture\ntools: Read\nmodel: opus\n---\n\nReview the task.\n"
    (source / "agents" / "fixture.md").write_bytes(raw)
    (source / "LICENSE").write_text("MIT fixture license\n")
    (source / "source.json").write_text(json.dumps({"commit": PIN}))
    (source / "skills").mkdir()
    (source / "skills" / "not-requested.md").write_text("Do not import")
    destination = tmp_path / "bundle"
    manifest = vendor(source, destination)
    assert set(path.name for path in destination.iterdir()) == {
        "fixture.md",
        "LICENSE",
        "manifest.json",
    }
    assert (destination / "fixture.md").read_bytes() == raw
    assert manifest["agents"]["fixture"]["sha256"] == hashlib.sha256(raw).hexdigest()


def test_importer_rejects_a_different_revision_before_writing(tmp_path):
    from tools.vendor_ecc_agents import vendor

    source = tmp_path / "source"
    source.mkdir()
    (source / "source.json").write_text(json.dumps({"commit": "wrong"}))
    destination = tmp_path / "bundle"
    with pytest.raises(ValueError, match="pinned ECC revision"):
        vendor(source, destination)
    assert not destination.exists()


def test_all_pinned_ecc_agents_and_only_agents_are_bundled():
    bundle = files("machina").joinpath("bundled_agents", "ecc")
    manifest = json.loads(bundle.joinpath("manifest.json").read_text())
    assert manifest["revision"] == "b92f181b3b748d1abbc2afd94af6aa7d8c8fca29"
    assert len(manifest["agents"]) == 71
    assert {path.name for path in bundle.iterdir()} == {
        "LICENSE",
        "manifest.json",
        *(f"{name}.md" for name in manifest["agents"]),
    }
    for name, metadata in manifest["agents"].items():
        raw = bundle.joinpath(f"{name}.md").read_bytes()
        assert hashlib.sha256(raw).hexdigest() == metadata["sha256"]
        agent = BUILTIN_AGENTS[name]
        assert agent.source == "ecc"
        assert agent.name == name
        assert agent.description and agent.system_prompt
        assert agent.model is None and agent.provider is None
    assert "MIT License" in bundle.joinpath("LICENSE").read_text()


async def test_ecc_agents_are_in_registry_and_gateway_with_custom_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("MACHINA_AGENTS_DIR", str(tmp_path))
    monkeypatch.setattr("machina.core.agent_def.db.fetch", AsyncMock(return_value=[]))
    monkeypatch.setattr("machina.core.agent_def.db.fetchrow", AsyncMock(return_value=None))
    registry = AgentRegistry()
    agents = await registry.list()
    assert len([agent for agent in agents if agent.source == "ecc"]) == 71
    assert (await registry.get("planner")).source == "ecc"
    from machina.gateway.features.agents import agents_list

    class Gateway:
        def require_db(self):
            pass

    result = await agents_list(Gateway(), {})
    assert len([agent for agent in result["agents"] if agent["source"] == "ecc"]) == 71


def test_upstream_tool_scopes_map_to_real_harness_tools_without_all_tool_fallback():
    import machina.tools  # noqa: F401
    from machina.tools.base import registry

    known = {definition.name for definition in registry.get_tool_definitions()}
    agents = [agent for agent in BUILTIN_AGENTS.values() if agent.source == "ecc"]
    assert len(agents) == 71
    for agent in agents:
        assert agent.tools and set(agent.tools) <= known
    assert set(BUILTIN_AGENTS["planner"].tools) == {"read_file", "search_files", "list_files"}
    assert "write_file" not in BUILTIN_AGENTS["planner"].tools
    assert "terminal" not in BUILTIN_AGENTS["planner"].tools
    assert "write_file" in BUILTIN_AGENTS["build-error-resolver"].tools
    assert "terminal" in BUILTIN_AGENTS["build-error-resolver"].tools
    assert "mcp__context7__query-docs" not in BUILTIN_AGENTS["docs-lookup"].tools
    assert "unavailable" in BUILTIN_AGENTS["docs-lookup"].system_prompt.lower()


async def test_factory_builds_selected_ecc_role_on_session_model_and_parent_caps(monkeypatch):
    from machina.core import agent_factory

    monkeypatch.setattr("machina.core.agent_def.db.fetchrow", AsyncMock(return_value=None))
    monkeypatch.setattr(agent_factory, "_memory_enabled", lambda: False)
    monkeypatch.setattr(agent_factory, "_shared_rag_pipeline", AsyncMock(return_value=None))
    selected = []

    def provider(**kwargs):
        selected.append(kwargs)
        return object()

    monkeypatch.setattr("machina.core.provider.get_provider", provider)
    agent = await agent_factory.build_agent_for_session(
        Session(id=uuid.uuid4(), agent_id="planner", model="chosen/model", provider="openrouter"),
        authority={"tools": ["read_file", "write_file"], "max_mode": "ask"},
    )
    assert selected == [{"model": "chosen/model", "provider": "openrouter"}]
    assert agent.allowed_tools == ["read_file"]
    assert agent.system_prompt == BUILTIN_AGENTS["planner"].system_prompt
    assert "planning specialist" in agent.system_prompt
    assert "security-reviewer" not in agent.system_prompt


async def test_ecc_agent_delegation_uses_existing_orchestrator(db_pool, monkeypatch):
    from machina.core.agent import AgentResponse
    from machina.core.orchestrator import Orchestrator
    from machina.core.session import SessionManager

    monkeypatch.setattr("machina.core.agent_def.db", db_pool)
    monkeypatch.setattr("machina.core.orchestrator.db", db_pool)
    monkeypatch.setattr("machina.core.session.db", db_pool)
    monkeypatch.setattr("machina.core.context.db", db_pool)
    parent = await SessionManager().create(agent_id="harness", title="ECC delegation proof")
    definitions = []

    class FixtureAgent:
        async def run(self, session_id, task, verbose=False):
            return AgentResponse(content=f"plan: {task}", tokens_used=7, iterations=1)

    def build(definition):
        definitions.append(definition)
        return FixtureAgent()

    orchestrator = Orchestrator(agent_factory=build)
    try:
        result = await orchestrator.delegate(
            "planner", "Plan a safe change", parent_session_id=parent.id
        )
        assert definitions[0].name == "planner" and definitions[0].source == "ecc"
        assert result.status == "complete" and result.tokens == 7
        assert "Plan a safe change" in result.response
        await SessionManager().delete(result.session_id)
    finally:
        await SessionManager().delete(parent.id)
