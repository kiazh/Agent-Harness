"""SoulSpec adapters — export to various agent frameworks."""

from __future__ import annotations

from ah.core.agent_def import AgentDef
from ah.soulspec.schema import SoulSpec


class AgentHarnessAdapter:
    """Export AgentHarness config to/from SoulSpec."""

    @staticmethod
    def to_soulspec(agent_def: AgentDef) -> SoulSpec:
        """Convert AgentDef to SoulSpec."""
        persona = SoulSpec.Persona(
            name=agent_def.name,
            description=agent_def.description,
            system_prompt=agent_def.system_prompt,
        )
        config = SoulSpec.Config(
            model=agent_def.model,
            max_iterations=agent_def.max_iterations,
        )
        return SoulSpec(
            name=agent_def.name,
            persona=persona,
            workflow=[SoulSpec.Workflow(name="agent-tools", tools=list(agent_def.tools))]
            if agent_def.tools
            else [],
            config=config,
        )

    @staticmethod
    def from_soulspec(spec: SoulSpec) -> AgentDef:
        """Import SoulSpec to AgentDef."""
        return spec.to_agent_def()


class ClaudeCodeAdapter:
    """Export SoulSpec to Claude Code CLAUDE.md format."""

    @staticmethod
    def export(spec: SoulSpec) -> str:
        """Export SoulSpec as CLAUDE.md content."""
        sections: list[str] = []

        # Header
        sections.append(f"# {spec.name}")
        if spec.persona and spec.persona.description:
            sections.append(f"\n{spec.persona.description}\n")

        # Persona
        if spec.persona:
            if spec.persona.values:
                values_str = ", ".join(f"{v.name} ({v.weight})" for v in spec.persona.values)
                sections.append(f"## Values\n{values_str}\n")
            if spec.persona.traits:
                traits_str = ", ".join(f"{t.name} ({t.strength})" for t in spec.persona.traits)
                sections.append(f"## Traits\n{traits_str}\n")
            if spec.persona.voice:
                sections.append(
                    f"## Voice\nTone: {spec.persona.voice.tone}, Style: {spec.persona.voice.style}\n"
                )

        # Workflow
        if spec.workflow:
            wf_lines: list[str] = ["## Workflows"]
            for wf in spec.workflow:
                wf_lines.append(f"### {wf.name}")
                if wf.description:
                    wf_lines.append(wf.description)
                if wf.steps:
                    wf_lines.append("Steps: " + " → ".join(wf.steps))
                if wf.tools:
                    wf_lines.append("Tools: " + ", ".join(wf.tools))
                wf_lines.append("")
            sections.append("\n".join(wf_lines))

        # Skills
        if spec.skills:
            skill_lines: list[str] = ["## Skills"]
            for skill in spec.skills:
                level_label = {1: "always", 2: "on-demand", 3: "deep"}.get(
                    skill.progressive_disclosure_level, "on-demand"
                )
                skill_lines.append(f"- **{skill.name}** ({level_label})")
                if skill.triggers:
                    skill_lines.append(f"  Triggers: {', '.join(skill.triggers)}")
            sections.append("\n".join(skill_lines))

        # Config
        if spec.config:
            config_lines: list[str] = ["## Config"]
            if spec.config.model:
                config_lines.append(f"- Model: {spec.config.model}")
            config_lines.append(f"- Max iterations: {spec.config.max_iterations}")
            config_lines.append(f"- Context budget: {spec.config.context_budget}")
            sections.append("\n".join(config_lines))

        return "\n\n".join(sections) + "\n"


class CodexAdapter:
    """Export SoulSpec to Codex AGENTS.md format."""

    @staticmethod
    def export(spec: SoulSpec) -> str:
        """Export SoulSpec as AGENTS.md content."""
        sections: list[str] = []

        # Header
        sections.append(f"# AGENTS.md — {spec.name}")
        if spec.persona and spec.persona.description:
            sections.append(f"\n> {spec.persona.description}\n")

        # Persona
        if spec.persona:
            if spec.persona.values:
                values_lines = [f"- **{v.name}**: {v.weight}" for v in spec.persona.values]
                sections.append("## Core Values\n" + "\n".join(values_lines) + "\n")
            if spec.persona.traits:
                traits_lines = [f"- **{t.name}**: {t.strength}" for t in spec.persona.traits]
                sections.append("## Traits\n" + "\n".join(traits_lines) + "\n")
            if spec.persona.voice:
                sections.append(
                    f"## Communication\n- Tone: {spec.persona.voice.tone}\n- Style: {spec.persona.voice.style}\n"
                )

        # Workflow
        if spec.workflow:
            wf_lines: list[str] = ["## Workflows"]
            for wf in spec.workflow:
                wf_lines.append(f"### {wf.name}")
                if wf.description:
                    wf_lines.append(f"> {wf.description}")
                if wf.steps:
                    for i, step in enumerate(wf.steps, 1):
                        wf_lines.append(f"{i}. {step}")
                if wf.tools:
                    wf_lines.append(f"Tools: {', '.join(wf.tools)}")
                wf_lines.append("")
            sections.append("\n".join(wf_lines))

        # Skills
        if spec.skills:
            skill_lines: list[str] = ["## Skills"]
            for skill in spec.skills:
                level_label = {1: "always loaded", 2: "load on demand", 3: "deep reference"}.get(
                    skill.progressive_disclosure_level, "load on demand"
                )
                skill_lines.append(f"### {skill.name} ({level_label})")
                if skill.triggers:
                    skill_lines.append(f"Triggers: {', '.join(skill.triggers)}")
                if skill.path:
                    skill_lines.append(f"Path: {skill.path}")
                skill_lines.append("")
            sections.append("\n".join(skill_lines))

        # Config
        if spec.config:
            config_lines: list[str] = ["## Configuration"]
            if spec.config.model:
                config_lines.append(f"- Model: `{spec.config.model}`")
            config_lines.append(f"- Max iterations: {spec.config.max_iterations}")
            config_lines.append(f"- Context budget: {spec.config.context_budget}")
            if spec.config.permissions:
                if spec.config.permissions.allowed_tools:
                    config_lines.append(
                        f"- Allowed tools: {', '.join(spec.config.permissions.allowed_tools)}"
                    )
                if spec.config.permissions.denied_tools:
                    config_lines.append(
                        f"- Denied tools: {', '.join(spec.config.permissions.denied_tools)}"
                    )
            sections.append("\n".join(config_lines))

        return "\n\n".join(sections) + "\n"
