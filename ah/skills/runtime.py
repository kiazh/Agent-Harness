"""Small prompt catalog for matching skills."""

from __future__ import annotations

import ah.skills.registry as skills_module


def matching_skill_catalog(query: str) -> str:
    """Return validated skill names only; bodies require skill_read."""
    skills = skills_module.skill_registry
    skills.load_all()
    matches = skills.match_triggers(query)[:3]
    if not matches:
        return ""
    lines = "\n".join(f"- {skill.name}" for skill in matches)
    return (
        "\n\n## Relevant skills\n"
        "Use skill_read(skill_name, offset) to read a skill before following it.\n" + lines
    )
