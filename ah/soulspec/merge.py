"""SoulSpec merge semantics — merge multiple configurations."""

from __future__ import annotations

from ah.soulspec.schema import SoulSpec


class SoulSpecMerger:
    """Merge multiple SoulSpec configurations.

    Merge rules:
    1. Scalar fields: override wins
    2. Lists: union with deduplication
    3. Dicts: recursive merge
    4. System prompts: concatenate with separator
    5. Permissions: intersection (most restrictive wins)
    """

    @staticmethod
    def merge(base: SoulSpec, override: SoulSpec) -> SoulSpec:
        """Merge override into base, returning a new SoulSpec."""
        # Scalar fields: override wins
        name = override.name if override.name else base.name
        version = override.version if override.version is not None else base.version

        # Persona: recursive merge
        persona = SoulSpecMerger._merge_persona(base.persona, override.persona)

        # Workflow: union by name with recursive merge
        workflow = SoulSpecMerger._merge_workflow(base.workflow, override.workflow)

        # Skills: union by name with recursive merge
        skills = SoulSpecMerger._merge_skills(base.skills, override.skills)

        # Config: recursive merge
        config = SoulSpecMerger._merge_config(base.config, override.config)

        return SoulSpec(
            name=name,
            version=version,
            persona=persona,
            workflow=workflow,
            skills=skills,
            config=config,
        )

    @staticmethod
    def _merge_persona(
        base: SoulSpec.Persona | None, override: SoulSpec.Persona | None
    ) -> SoulSpec.Persona | None:
        if base is None:
            return override
        if override is None:
            return base

        # System prompt: concatenate
        system_prompt = base.system_prompt
        if override.system_prompt and override.system_prompt != base.system_prompt:
            if system_prompt:
                system_prompt = f"{system_prompt}\n\n{override.system_prompt}"
            else:
                system_prompt = override.system_prompt

        # Values: union by name
        values = SoulSpecMerger._merge_persona_values(base.values, override.values)

        # Traits: union by name
        traits = SoulSpecMerger._merge_persona_traits(base.traits, override.traits)

        # Voice: override wins for non-default fields
        voice = base.voice
        if override.voice:
            if voice is None:
                voice = override.voice
            else:
                voice = SoulSpec.Voice(
                    tone=override.voice.tone if override.voice.tone != "neutral" else voice.tone,
                    style=override.voice.style
                    if override.voice.style != "concise"
                    else voice.style,
                )

        return SoulSpec.Persona(
            name=override.name if override.name else base.name,
            description=override.description if override.description else base.description,
            values=values,
            traits=traits,
            voice=voice,
            system_prompt=system_prompt,
        )

    @staticmethod
    def _merge_persona_values(
        base: list[SoulSpec.PersonaValue], override: list[SoulSpec.PersonaValue]
    ) -> list[SoulSpec.PersonaValue]:
        """Union by name; override weight wins."""
        result = {v.name: v for v in base}
        for v in override:
            result[v.name] = v  # override wins
        return list(result.values())

    @staticmethod
    def _merge_persona_traits(
        base: list[SoulSpec.PersonaTrait], override: list[SoulSpec.PersonaTrait]
    ) -> list[SoulSpec.PersonaTrait]:
        """Union by name; override strength wins."""
        result = {t.name: t for t in base}
        for t in override:
            result[t.name] = t
        return list(result.values())

    @staticmethod
    def _merge_workflow(
        base: list[SoulSpec.Workflow], override: list[SoulSpec.Workflow]
    ) -> list[SoulSpec.Workflow]:
        """Union by name; merge steps and tools."""
        result: dict[str, SoulSpec.Workflow] = {w.name: w for w in base}
        for w in override:
            if w.name in result:
                existing = result[w.name]
                # Union steps and tools
                steps = list(dict.fromkeys(existing.steps + w.steps))
                tools = list(dict.fromkeys(existing.tools + w.tools))
                result[w.name] = SoulSpec.Workflow(
                    name=w.name,
                    description=w.description or existing.description,
                    steps=steps,
                    tools=tools,
                )
            else:
                result[w.name] = w
        return list(result.values())

    @staticmethod
    def _merge_skills(
        base: list[SoulSpec.Skill], override: list[SoulSpec.Skill]
    ) -> list[SoulSpec.Skill]:
        """Union by name; merge triggers."""
        result: dict[str, SoulSpec.Skill] = {s.name: s for s in base}
        for s in override:
            if s.name in result:
                existing = result[s.name]
                triggers = list(dict.fromkeys(existing.triggers + s.triggers))
                result[s.name] = SoulSpec.Skill(
                    name=s.name,
                    triggers=triggers,
                    path=s.path or existing.path,
                    progressive_disclosure_level=s.progressive_disclosure_level,
                )
            else:
                result[s.name] = s
        return list(result.values())

    @staticmethod
    def _merge_config(
        base: SoulSpec.Config | None, override: SoulSpec.Config | None
    ) -> SoulSpec.Config | None:
        if base is None:
            return override
        if override is None:
            return base

        # Permissions: intersection (most restrictive wins)
        permissions = SoulSpecMerger._merge_permissions(base.permissions, override.permissions)

        return SoulSpec.Config(
            model=override.model if override.model else base.model,
            max_iterations=override.max_iterations
            if override.max_iterations is not None
            else base.max_iterations,
            context_budget=override.context_budget
            if override.context_budget is not None
            else base.context_budget,
            permissions=permissions,
        )

    @staticmethod
    def _merge_permissions(
        base: SoulSpec.Permissions | None, override: SoulSpec.Permissions | None
    ) -> SoulSpec.Permissions | None:
        """Intersection of allowed_tools; union of denied_tools."""
        if base is None:
            return override
        if override is None:
            return base

        # Intersection of allowed_tools (most restrictive)
        allowed = sorted(set(base.allowed_tools) & set(override.allowed_tools))
        # Union of denied_tools
        denied = sorted(set(base.denied_tools) | set(override.denied_tools))

        return SoulSpec.Permissions(allowed_tools=allowed, denied_tools=denied)
