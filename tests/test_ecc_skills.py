"""Bundled ECC skills: offline defaults, task routing and confined resources."""

import pytest

from machina.skills.registry import SkillRegistry


def test_curated_ecc_skills_are_mandatory_even_with_local_override(tmp_path, monkeypatch):
    monkeypatch.setenv("MACHINA_SKILLS_DIR", str(tmp_path))
    skills = SkillRegistry()
    skills.load_all()
    bundled = [s for s in skills.list_skills() if s.source_type == "builtin-ecc"]
    assert len(bundled) == 42
    assert skills.get("ecc-visa-doc-translate") is None
    assert skills.get("ecc-network-bgp-diagnostics") is None
    assert skills.get("ecc-strategic-compact") is None
    skill = skills.get("ecc-python-patterns")
    assert skill and "Python Development Patterns" in skills.get_skill_content(skill.name)
    assert skills.set_enabled(skill.name, False) is None
    assert skills.update_skill(skill.name, content="changed") is None
    assert skills.delete_skill(skill.name) is False
    assert skills.get(skill.name).enabled


def test_ecc_router_and_budget():
    from machina.skills.runtime import automatic_skill_context, rank_skills

    skills = SkillRegistry()
    skills.load_all()
    for query, expected in [
        ("Write Python unit tests using pytest fixtures", "ecc-python-testing"),
        ("Optimize slow PostgreSQL queries and indexes", "ecc-postgres-patterns"),
        ("Design a FastAPI service", "ecc-fastapi-patterns"),
        ("Review API authentication security vulnerabilities", "ecc-security-review"),
    ]:
        assert expected in [s.name for s in rank_skills(skills, query)[:3]]
    assert not rank_skills(skills, "hello thanks")
    from machina.core.assembler import get_token_count

    text = automatic_skill_context(
        "Write Python unit tests using pytest", skills=skills, budget=600
    )
    assert "ecc-python-testing" in text
    assert "skill_read" in text
    assert get_token_count(text) <= 600


def test_resources_reject_escape_and_detect_tampering(tmp_path):
    from machina.skills.bundle import ECCBundle

    bundle = ECCBundle()
    with pytest.raises(ValueError):
        bundle.read_resource("../../.env")
    with pytest.raises(ValueError):
        bundle.read_resource("/etc/passwd")
    assert "MIT" in bundle.read_resource("LICENSE")
    # Verify actual bytes, not just file size or path names.
    copy = tmp_path / "bundle"
    copy.mkdir()
    (copy / "manifest.json").write_bytes((bundle.root / "manifest.json").read_bytes())
    (copy / "LICENSE").write_text("tampered")
    with pytest.raises(ValueError, match="integrity"):
        ECCBundle(copy).read_resource("LICENSE")


@pytest.mark.asyncio
async def test_skill_read_continuation_reaches_model_without_silent_truncation(monkeypatch):
    """The general tool preview cap must not discard a skill page's final instructions."""
    import uuid
    from unittest.mock import AsyncMock

    import machina.skills.registry as module
    from machina.core.agent import BaseReActAgent
    from machina.core.models import LLMResponse

    skills = SkillRegistry()
    skills.load_all()
    monkeypatch.setattr(module, "skill_registry", skills)
    monkeypatch.setattr("machina.core.agent.context_manager.add_chunk", AsyncMock())
    monkeypatch.setattr(
        "machina.core.agent.context_manager.get_latest_images", AsyncMock(return_value=[])
    )
    agent = BaseReActAgent(provider=object(), allowed_tools=["skill_read"])
    messages = []
    response = LLMResponse(
        content="",
        model="fake",
        tool_calls=[
            {
                "id": "read",
                "type": "function",
                "function": {
                    "name": "skill_read",
                    "arguments": '{"skill_name":"ecc-python-patterns","limit":8000}',
                },
            }
        ],
    )
    await agent._execute_tool_calls(response, messages, [], uuid.uuid4(), False)
    body = skills.get_skill_content("ecc-python-patterns")[:8000]
    assert body[-150:] in messages[-1]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_default_skills_reach_first_provider_call(streaming, monkeypatch):
    import copy
    import uuid
    from unittest.mock import AsyncMock

    from machina.core.agent import ReActAgent
    from machina.core.config import config
    from machina.core.models import LLMResponse, Session, StreamEvent

    session = Session(id=uuid.uuid4(), context_budget=8000)
    for field in ("memory_enabled", "rag_enabled", "personality_growth_enabled"):
        monkeypatch.setattr(config, field, False)
    monkeypatch.setattr("machina.core.agent.session_manager.get", AsyncMock(return_value=session))
    monkeypatch.setattr("machina.core.agent.session_manager.update_activity", AsyncMock())
    monkeypatch.setattr(
        "machina.core.agent.context_manager.get_recent_context", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        "machina.core.agent.context_manager.search_archive_text", AsyncMock(return_value=[])
    )
    monkeypatch.setattr("machina.core.agent.context_manager.add_chunks_batch", AsyncMock())
    monkeypatch.setattr(
        "machina.core.agent.context_manager.get_latest_images", AsyncMock(return_value=[])
    )
    monkeypatch.setattr("machina.core.agent.BaseReActAgent._reserve_usage", AsyncMock(return_value=None))
    monkeypatch.setattr("machina.core.agent.BaseReActAgent._finish_usage", AsyncMock())

    class Provider:
        calls = []

        async def complete(self, **kwargs):
            self.calls.append(copy.deepcopy(kwargs))
            return LLMResponse(content="Done", model="fake", usage={"total_tokens": 1})

        async def stream_complete(self, **kwargs):
            yield StreamEvent(type="done", response=await self.complete(**kwargs))

    provider = Provider()
    provider.calls = []
    agent = ReActAgent(provider=provider, allowed_tools=[])
    if streaming:
        events = [
            event
            async for event in agent.run_stream(
                session.id, "Write Python unit tests using pytest", verbose=False
            )
        ]
        assert any(event.response and event.response.content == "Done" for event in events)
    else:
        assert (
            await agent.run(session.id, "Write Python unit tests using pytest", verbose=False)
        ).content == "Done"
    prompt = provider.calls[0]["messages"][0]["content"]
    assert "ecc-python-testing" in prompt
    assert "# Python Testing" in prompt
    assert provider.calls[0]["tools"] == []


@pytest.mark.asyncio
async def test_ecc_resource_tool_pages_and_read_policy(monkeypatch):
    import machina.skills.registry as module
    from machina.permissions.tools import action_for_tool
    from machina.tools.skills import skill_resource

    skills = SkillRegistry()
    skills.load_all()
    monkeypatch.setattr(module, "skill_registry", skills)
    first = skill_resource("ecc-python-patterns", "LICENSE", limit=100)
    second = skill_resource("ecc-python-patterns", "LICENSE", offset=100, limit=100)
    assert "[0:100/" in first and "[100:200/" in second
    assert "Error:" in skill_resource("ecc-python-patterns", "../../.env")
    assert (
        action_for_tool("skill_resource", {"skill_name": "ecc-python-patterns"})["operation"]
        == "skill.read"
    )


def test_all_canonical_bodies_are_readable_and_telemetry_does_not_reparse(tmp_path, monkeypatch):
    from machina.skills.registry import SkillParser

    monkeypatch.setenv("MACHINA_SKILLS_DIR", str(tmp_path))
    skills = SkillRegistry()
    skills.load_all()
    canonical = [s for s in skills.list_skills() if s.source_type == "builtin-ecc"]
    assert all(skills.get_skill_content(s.name) for s in canonical)

    def unexpected_parse(*args, **kwargs):
        pytest.fail("telemetry must not reparse skill instructions")

    monkeypatch.setattr(SkillParser, "parse", unexpected_parse)
    skills.record_use("ecc-python-patterns")
    skills.load_all()
    assert skills.get("ecc-python-patterns").usage_count == 1


@pytest.mark.parametrize("budget", [0, 50, 100, 150, 300, 600, 1200, 10000])
def test_skill_context_never_exceeds_its_budget(budget, tmp_path, monkeypatch):
    from machina.core.assembler import get_token_count
    from machina.skills.runtime import automatic_skill_context

    monkeypatch.setenv("MACHINA_SKILLS_DIR", str(tmp_path))
    text = automatic_skill_context(
        "Create Python tests with pytest and PostgreSQL queries",
        skills=SkillRegistry(),
        budget=budget,
    )
    assert get_token_count(text) <= min(budget, 1200)
    assert "ecc-rust" not in text


def test_skill_context_tells_model_not_to_page_greetings(tmp_path, monkeypatch):
    from machina.skills.runtime import automatic_skill_context

    monkeypatch.setenv("MACHINA_SKILLS_DIR", str(tmp_path))
    # No skill evidence: nothing injected at all (conduct lives in SYSTEM_PROMPT).
    assert automatic_skill_context("hello!", skills=SkillRegistry()) == ""
    text = automatic_skill_context("Implement a Python API", skills=SkillRegistry())
    assert "never call skill_list" in text
    assert "never enumerate the whole catalog" in text


def test_skill_list_description_discourages_catalog_enumeration():
    import machina.tools.skills  # noqa: F401 — registers skill_list on the global registry
    from machina.tools.base import registry

    desc = next(d.description for d in registry.get_tool_definitions() if d.name == "skill_list")
    assert "Never enumerate the whole catalog" in desc


@pytest.mark.parametrize(
    "query,expected",
    [
        ("Build Docker containers for this service", "ecc-docker-patterns"),
        ("Research official sources and produce a cited report", "ecc-deep-research"),
        ("Implement safe database migrations", "ecc-database-migrations"),
        ("Design a Python MCP server", "ecc-mcp-server-patterns"),
    ],
)
def test_representative_ecc_routing(query, expected):
    from machina.skills.runtime import rank_skills

    assert expected in [s.name for s in rank_skills(SkillRegistry(), query)[:3]]


def test_bundled_names_cannot_be_shadowed_but_derived_names_survive(tmp_path, monkeypatch):
    monkeypatch.setenv("MACHINA_SKILLS_DIR", str(tmp_path))
    skills = SkillRegistry()
    with pytest.raises(ValueError, match="bundled"):
        skills.create_skill("ecc-python-testing", "replacement", "wrong instructions")
    assert not (tmp_path / "ecc-python-testing").exists()
    skills.create_skill(
        "ecc-python-testing-learned", "Derived testing workflow", "Additional steps"
    )
    skills.load_all()
    assert skills.get_skill_content("ecc-python-testing-learned") == "Additional steps"
    assert "# Python Testing" in skills.get_skill_content("ecc-python-testing")


def test_builtin_curation_and_restricted_context(tmp_path, monkeypatch):
    from machina.skills.registry import SkillCurator
    from machina.skills.runtime import automatic_skill_context

    monkeypatch.setenv("MACHINA_SKILLS_DIR", str(tmp_path))
    skills = SkillRegistry()
    skills.load_all()
    assert SkillCurator(skills).cleanup_unused(dry_run=False) == []
    text = automatic_skill_context(
        "Write Python tests using pytest",
        skills=skills,
        can_read=False,
        can_search=False,
        can_resources=False,
    )
    assert "ecc-python-testing" in text
    assert "skill_read" not in text and "skill_list" not in text and "skill_resource" not in text
    assert "full instructions at" in text


def test_explicit_local_trigger_wins_without_a_technology_keyword(tmp_path):
    from machina.skills.runtime import rank_skills

    skills = SkillRegistry(tmp_path)
    skills.create_skill(
        "python-deploy", "A deployment workflow", "Deploy carefully", triggers=["deploy"]
    )
    assert [s.name for s in rank_skills(skills, "Deploy our service")] == ["python-deploy"]


@pytest.mark.asyncio
async def test_gateway_show_returns_full_default_instructions(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import machina.skills.registry as module
    from machina.gateway.errors import RpcError
    from machina.gateway.features.skills import skills_delete, skills_show

    monkeypatch.setenv("MACHINA_SKILLS_DIR", str(tmp_path))
    monkeypatch.setattr(module, "skill_registry", SkillRegistry())
    gw = SimpleNamespace(require_db=lambda: None)
    result = await skills_show(gw, {"name": "ecc-python-testing"})
    assert "# Python Testing" in result["skill"]["content"]
    with pytest.raises(RpcError, match="bundled defaults"):
        await skills_delete(gw, {"name": "ecc-python-testing"})
