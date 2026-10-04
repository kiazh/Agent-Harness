"""Gap 5: SoulSpec schema, merge semantics, conformance, adapters."""
from __future__ import annotations

import textwrap

import pytest
import yaml

from ah.core.agent_def import AgentDef
from ah.soulspec import (
    AgentHarnessAdapter,
    ClaudeCodeAdapter,
    CodexAdapter,
    SoulSpec,
    SoulSpecConformance,
    SoulSpecMerger,
)

# ─── Fixtures ───────────────────────────────────────────────────────────────

SAMPLE_YAML = textwrap.dedent("""\
    name: "Harness Agent"
    version: "1.0.0"
    persona:
      name: "Harness"
      description: "General-purpose AI agent"
      values:
        - name: "accuracy"
          weight: 0.9
        - name: "helpfulness"
          weight: 0.8
      traits:
        - name: "analytical"
          strength: 0.8
      voice:
        tone: "professional"
        style: "concise"
      system_prompt: |
        You are {name}. {description}
        Core values: {values}
        Communication style: {voice.style}
    workflow:
      - name: "code-review"
        description: "Review code for issues"
        steps:
          - "read_file"
          - "analyze"
          - "suggest_fixes"
        tools: ["read_file", "search_files"]
    skills:
      - name: "python-expert"
        triggers: ["python", "pytest", "django"]
        path: "skills/python-expert.md"
        progressive_disclosure:
          level: 2
    config:
      model: "anthropic/claude-3.5-sonnet"
      max_iterations: 10
      context_budget: 8000
""")


@pytest.fixture
def sample_spec() -> SoulSpec:
    return SoulSpec.from_yaml(SAMPLE_YAML)


@pytest.fixture
def sample_agent_def() -> AgentDef:
    return AgentDef(
        name="Harness Agent",
        description="General-purpose AI agent",
        system_prompt="You are Harness. General-purpose AI agent",
        tools=["read_file", "search_files"],
        model="anthropic/claude-3.5-sonnet",
        max_iterations=10,
    )


# ─── Phase 5A: Schema ───────────────────────────────────────────────────────

class TestSoulSpecSchema:
    def test_soulspec_schema_creation(self, sample_spec: SoulSpec) -> None:
        """SoulSpec can be created from YAML with all sections populated."""
        assert sample_spec.name == "Harness Agent"
        assert sample_spec.version == "1.0.0"
        assert sample_spec.persona is not None
        assert sample_spec.persona.name == "Harness"
        assert sample_spec.persona.description == "General-purpose AI agent"
        assert len(sample_spec.persona.values) == 2
        assert sample_spec.persona.values[0].name == "accuracy"
        assert sample_spec.persona.values[0].weight == 0.9
        assert len(sample_spec.persona.traits) == 1
        assert sample_spec.persona.traits[0].name == "analytical"
        assert sample_spec.persona.voice is not None
        assert sample_spec.persona.voice.tone == "professional"
        assert sample_spec.persona.voice.style == "concise"
        assert "You are {name}" in sample_spec.persona.system_prompt
        assert len(sample_spec.workflow) == 1
        assert sample_spec.workflow[0].name == "code-review"
        assert sample_spec.workflow[0].steps == ["read_file", "analyze", "suggest_fixes"]
        assert sample_spec.workflow[0].tools == ["read_file", "search_files"]
        assert len(sample_spec.skills) == 1
        assert sample_spec.skills[0].name == "python-expert"
        assert sample_spec.skills[0].triggers == ["python", "pytest", "django"]
        assert sample_spec.skills[0].progressive_disclosure_level == 2
        assert sample_spec.config is not None
        assert sample_spec.config.model == "anthropic/claude-3.5-sonnet"
        assert sample_spec.config.max_iterations == 10
        assert sample_spec.config.context_budget == 8000

    def test_soulspec_to_yaml_roundtrip(self, sample_spec: SoulSpec) -> None:
        """SoulSpec can be serialized to YAML and back."""
        yaml_str = sample_spec.to_yaml()
        restored = SoulSpec.from_yaml(yaml_str)
        assert restored.name == sample_spec.name
        assert restored.version == sample_spec.version
        assert restored.persona is not None
        assert restored.persona.name == sample_spec.persona.name
        assert restored.config is not None
        assert restored.config.model == sample_spec.config.model

    def test_soulspec_from_dict(self) -> None:
        """SoulSpec can be created from a plain dict."""
        data = yaml.safe_load(SAMPLE_YAML)
        spec = SoulSpec.from_dict(data)
        assert spec.name == "Harness Agent"
        assert spec.persona is not None
        assert spec.persona.name == "Harness"

    def test_soulspec_minimal(self) -> None:
        """SoulSpec with only required fields can be created."""
        spec = SoulSpec(name="Minimal")
        assert spec.name == "Minimal"
        assert spec.version == "1.0.0"
        assert spec.persona is None
        assert spec.workflow == []
        assert spec.skills == []
        assert spec.config is None


# ─── AgentDef ↔ SoulSpec conversion ─────────────────────────────────────────

class TestAgentDefConversion:
    def test_soulspec_to_agent_def(self, sample_spec: SoulSpec) -> None:
        """SoulSpec can be converted to AgentDef."""
        agent = sample_spec.to_agent_def()
        assert isinstance(agent, AgentDef)
        assert agent.name == "Harness Agent"
        assert agent.description == "General-purpose AI agent"
        assert agent.system_prompt == sample_spec.persona.system_prompt.strip()
        assert agent.tools == ["read_file", "search_files"]
        assert agent.model == "anthropic/claude-3.5-sonnet"
        assert agent.max_iterations == 10

    def test_agent_def_to_soulspec(self, sample_agent_def: AgentDef) -> None:
        """AgentDef can be converted to SoulSpec."""
        spec = AgentHarnessAdapter.to_soulspec(sample_agent_def)
        assert isinstance(spec, SoulSpec)
        assert spec.name == "Harness Agent"
        assert spec.persona is not None
        assert spec.persona.name == "Harness Agent"
        assert spec.persona.description == "General-purpose AI agent"
        assert spec.persona.system_prompt == "You are Harness. General-purpose AI agent"
        assert spec.config is not None
        assert spec.config.model == "anthropic/claude-3.5-sonnet"
        assert spec.config.max_iterations == 10

    def test_roundtrip_agent_def(self, sample_agent_def: AgentDef) -> None:
        """AgentDef → SoulSpec → AgentDef preserves key fields."""
        spec = AgentHarnessAdapter.to_soulspec(sample_agent_def)
        agent = spec.to_agent_def()
        assert agent.name == sample_agent_def.name
        assert agent.description == sample_agent_def.description
        assert agent.model == sample_agent_def.model
        assert agent.max_iterations == sample_agent_def.max_iterations


# ─── Phase 5B: Merge Semantics ──────────────────────────────────────────────

class TestMergeSemantics:
    def test_merge_scalar_override(self) -> None:
        """Scalar fields: override wins."""
        base = SoulSpec(
            name="Base",
            version="1.0.0",
            config=SoulSpec.Config(model="gpt-4", max_iterations=5),
        )
        override = SoulSpec(
            name="Override",
            version="2.0.0",
            config=SoulSpec.Config(model="claude-3", max_iterations=15),
        )
        merged = SoulSpecMerger.merge(base, override)
        assert merged.name == "Override"
        assert merged.version == "2.0.0"
        assert merged.config is not None
        assert merged.config.model == "claude-3"
        assert merged.config.max_iterations == 15

    def test_merge_list_union(self) -> None:
        """Lists: union with deduplication."""
        base = SoulSpec(
            name="Base",
            workflow=[
                SoulSpec.Workflow(name="wf1", steps=["a", "b"]),
                SoulSpec.Workflow(name="wf2", steps=["c"]),
            ],
        )
        override = SoulSpec(
            name="Override",
            workflow=[
                SoulSpec.Workflow(name="wf2", steps=["c", "d"]),
                SoulSpec.Workflow(name="wf3", steps=["e"]),
            ],
        )
        merged = SoulSpecMerger.merge(base, override)
        names = [w.name for w in merged.workflow]
        assert "wf1" in names
        assert "wf2" in names
        assert "wf3" in names
        # wf2 should be merged (steps unioned), not duplicated
        wf2 = next(w for w in merged.workflow if w.name == "wf2")
        assert set(wf2.steps) == {"c", "d"}

    def test_merge_dict_recursive(self) -> None:
        """Dicts: recursive merge."""
        base = SoulSpec(
            name="Base",
            persona=SoulSpec.Persona(
                name="Base",
                values=[SoulSpec.PersonaValue(name="accuracy", weight=0.5)],
            ),
        )
        override = SoulSpec(
            name="Override",
            persona=SoulSpec.Persona(
                name="Override",
                values=[SoulSpec.PersonaValue(name="speed", weight=0.7)],
            ),
        )
        merged = SoulSpecMerger.merge(base, override)
        assert merged.persona is not None
        assert merged.persona.name == "Override"
        value_names = [v.name for v in merged.persona.values]
        assert "accuracy" in value_names
        assert "speed" in value_names

    def test_merge_permissions_intersection(self) -> None:
        """Permissions: most restrictive wins (intersection)."""
        base = SoulSpec(
            name="Base",
            config=SoulSpec.Config(
                permissions=SoulSpec.Permissions(
                    allowed_tools=["read_file", "write_file", "terminal"],
                    denied_tools=["terminal"],
                )
            ),
        )
        override = SoulSpec(
            name="Override",
            config=SoulSpec.Config(
                permissions=SoulSpec.Permissions(
                    allowed_tools=["read_file", "search_files"],
                    denied_tools=["write_file"],
                )
            ),
        )
        merged = SoulSpecMerger.merge(base, override)
        assert merged.config is not None
        assert merged.config.permissions is not None
        # Intersection of allowed_tools
        allowed = set(merged.config.permissions.allowed_tools)
        assert "read_file" in allowed
        assert "write_file" not in allowed  # not in override's allowed
        assert "search_files" not in allowed  # not in base's allowed
        # Union of denied_tools
        denied = set(merged.config.permissions.denied_tools)
        assert "terminal" in denied
        assert "write_file" in denied

    def test_merge_system_prompt_concatenation(self) -> None:
        """System prompts: concatenate with separator."""
        base = SoulSpec(
            name="Base",
            persona=SoulSpec.Persona(name="Base", system_prompt="Base prompt."),
        )
        override = SoulSpec(
            name="Override",
            persona=SoulSpec.Persona(name="Override", system_prompt="Override prompt."),
        )
        merged = SoulSpecMerger.merge(base, override)
        assert merged.persona is not None
        assert "Base prompt." in merged.persona.system_prompt
        assert "Override prompt." in merged.persona.system_prompt

    def test_merge_skills_union(self) -> None:
        """Skills: union with deduplication by name."""
        base = SoulSpec(
            name="Base",
            skills=[SoulSpec.Skill(name="python", triggers=["py"])],
        )
        override = SoulSpec(
            name="Override",
            skills=[
                SoulSpec.Skill(name="python", triggers=["py", "pytest"]),
                SoulSpec.Skill(name="rust", triggers=["rs"]),
            ],
        )
        merged = SoulSpecMerger.merge(base, override)
        skill_names = [s.name for s in merged.skills]
        assert "python" in skill_names
        assert "rust" in skill_names
        # python skill should have merged triggers
        python = next(s for s in merged.skills if s.name == "python")
        assert set(python.triggers) == {"py", "pytest"}


# ─── Phase 5C: Conformance ──────────────────────────────────────────────────

class TestConformance:
    def test_conformance_schema_validation(self, sample_spec: SoulSpec) -> None:
        """Schema validation works for valid and invalid specs."""
        conf = SoulSpecConformance()
        result = conf.validate_schema(sample_spec)
        assert result.valid
        assert result.errors == []

        invalid = SoulSpec(name="")
        result = conf.validate_schema(invalid)
        assert not result.valid
        assert len(result.errors) > 0

    def test_conformance_merge_semantics(self) -> None:
        """Merge semantics are correct per the conformance suite."""
        conf = SoulSpecConformance()
        conf.test_merge_semantics()  # should not raise

    def test_conformance_progressive_disclosure(self) -> None:
        """Skills load at correct disclosure level."""
        conf = SoulSpecConformance()
        conf.test_progressive_disclosure()  # should not raise

    def test_conformance_progressive_disclosure_levels(self) -> None:
        """Progressive disclosure levels are correctly assigned."""
        spec = SoulSpec(
            name="Test",
            skills=[
                SoulSpec.Skill(name="always", progressive_disclosure_level=1),
                SoulSpec.Skill(name="on-demand", progressive_disclosure_level=2),
                SoulSpec.Skill(name="deep", progressive_disclosure_level=3),
            ],
        )
        # Level 1 = always loaded, Level 2 = on demand, Level 3 = deep reference
        always = [s for s in spec.skills if s.progressive_disclosure_level == 1]
        on_demand = [s for s in spec.skills if s.progressive_disclosure_level == 2]
        deep = [s for s in spec.skills if s.progressive_disclosure_level == 3]
        assert len(always) == 1
        assert always[0].name == "always"
        assert len(on_demand) == 1
        assert on_demand[0].name == "on-demand"
        assert len(deep) == 1
        assert deep[0].name == "deep"


# ─── Phase 5D: Adapters ─────────────────────────────────────────────────────

class TestAdapters:
    def test_adapter_claude_code(self, sample_spec: SoulSpec) -> None:
        """Export to Claude Code CLAUDE.md format."""
        adapter = ClaudeCodeAdapter()
        output = adapter.export(sample_spec)
        assert isinstance(output, str)
        assert "Harness Agent" in output
        assert "General-purpose AI agent" in output
        assert "code-review" in output
        assert "python-expert" in output

    def test_adapter_codex(self, sample_spec: SoulSpec) -> None:
        """Export to Codex AGENTS.md format."""
        adapter = CodexAdapter()
        output = adapter.export(sample_spec)
        assert isinstance(output, str)
        assert "Harness Agent" in output
        assert "General-purpose AI agent" in output
        assert "code-review" in output
        assert "python-expert" in output

    def test_adapter_agent_harness(self, sample_agent_def: AgentDef) -> None:
        """AgentHarness adapter round-trips AgentDef."""
        adapter = AgentHarnessAdapter()
        spec = adapter.to_soulspec(sample_agent_def)
        assert isinstance(spec, SoulSpec)
        assert spec.name == sample_agent_def.name
        agent = adapter.from_soulspec(spec)
        assert isinstance(agent, AgentDef)
        assert agent.name == sample_agent_def.name
        assert agent.model == sample_agent_def.model
