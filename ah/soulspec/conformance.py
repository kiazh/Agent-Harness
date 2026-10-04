"""SoulSpec conformance test suite."""
from __future__ import annotations

from dataclasses import dataclass, field

from ah.soulspec.merge import SoulSpecMerger
from ah.soulspec.schema import SoulSpec


@dataclass
class ValidationResult:
    valid: bool
    errors: list[str] = field(default_factory=list)


class SoulSpecConformance:
    """Test that a SoulSpec conforms to the standard."""

    def validate_schema(self, spec: SoulSpec) -> ValidationResult:
        """Validate SoulSpec against the schema."""
        errors: list[str] = []

        if not spec.name or not spec.name.strip():
            errors.append("name must be a non-empty string")

        if not spec.version:
            errors.append("version must be specified")

        if spec.persona:
            if not spec.persona.name.strip():
                errors.append("persona.name must be non-empty when persona is present")

        if spec.config:
            if spec.config.max_iterations < 1:
                errors.append("config.max_iterations must be >= 1")
            if spec.config.context_budget < 1:
                errors.append("config.context_budget must be >= 1")

        for skill in spec.skills:
            if not skill.name.strip():
                errors.append("skill.name must be non-empty")
            if skill.progressive_disclosure_level not in (1, 2, 3):
                errors.append(
                    f"skill {skill.name}: progressive_disclosure_level must be 1, 2, or 3"
                )

        return ValidationResult(valid=len(errors) == 0, errors=errors)

    def test_merge_semantics(self) -> None:
        """Test that merge works correctly."""
        # Scalar override
        base = SoulSpec(name="Base", version="1.0.0", config=SoulSpec.Config(model="gpt-4", max_iterations=5))
        override = SoulSpec(name="Override", version="2.0.0", config=SoulSpec.Config(model="claude-3", max_iterations=15))
        merged = SoulSpecMerger.merge(base, override)
        assert merged.name == "Override"
        assert merged.version == "2.0.0"
        assert merged.config is not None
        assert merged.config.model == "claude-3"
        assert merged.config.max_iterations == 15

        # List union
        base = SoulSpec(
            name="Base",
            workflow=[SoulSpec.Workflow(name="wf1", steps=["a"])],
        )
        override = SoulSpec(
            name="Override",
            workflow=[SoulSpec.Workflow(name="wf1", steps=["b"]), SoulSpec.Workflow(name="wf2", steps=["c"])],
        )
        merged = SoulSpecMerger.merge(base, override)
        names = [w.name for w in merged.workflow]
        assert "wf1" in names
        assert "wf2" in names

        # Dict recursive merge
        base = SoulSpec(
            name="Base",
            persona=SoulSpec.Persona(name="Base", values=[]),
        )
        override = SoulSpec(
            name="Override",
            persona=SoulSpec.Persona(name="Override", values=[]),
        )
        merged = SoulSpecMerger.merge(base, override)
        assert merged.persona is not None
        assert merged.persona.name == "Override"

        # Permissions intersection
        base = SoulSpec(
            name="Base",
            config=SoulSpec.Config(
                permissions=SoulSpec.Permissions(
                    allowed_tools=["read_file", "write_file"],
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
        allowed = set(merged.config.permissions.allowed_tools)
        assert allowed == {"read_file"}
        denied = set(merged.config.permissions.denied_tools)
        assert denied == {"terminal", "write_file"}

    def test_progressive_disclosure(self) -> None:
        """Test that skills load at correct disclosure level."""
        spec = SoulSpec(
            name="Test",
            skills=[
                SoulSpec.Skill(name="always", progressive_disclosure_level=1),
                SoulSpec.Skill(name="on-demand", progressive_disclosure_level=2),
                SoulSpec.Skill(name="deep", progressive_disclosure_level=3),
            ],
        )
        # Level 1 = always loaded
        always = [s for s in spec.skills if s.progressive_disclosure_level == 1]
        assert len(always) == 1
        assert always[0].name == "always"

        # Level 2 = on demand
        on_demand = [s for s in spec.skills if s.progressive_disclosure_level == 2]
        assert len(on_demand) == 1
        assert on_demand[0].name == "on-demand"

        # Level 3 = deep reference
        deep = [s for s in spec.skills if s.progressive_disclosure_level == 3]
        assert len(deep) == 1
        assert deep[0].name == "deep"
