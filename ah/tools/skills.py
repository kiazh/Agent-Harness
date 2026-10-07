"""Progressive skill discovery for the running agent."""

from __future__ import annotations

import ah.skills.registry as skills_module
from ah.tools.base import registry


@registry.register(
    name="skill_list",
    description="Browse enabled skills by name or description. Use offset for the next page.",
)
def skill_list(query: str = "", offset: int = 0) -> str:
    skills = skills_module.skill_registry
    skills.load_all()
    if offset < 0:
        return "Error: offset must be non-negative"
    needle = query.casefold()
    matches = [
        skill
        for skill in skills.list_skills()
        if skill.enabled
        and (
            not needle
            or needle in skill.name.casefold()
            or needle in skill.description.casefold()
            or any(needle in trigger.casefold() for trigger in skill.triggers)
        )
    ]
    visible = matches[offset : offset + 4]
    if not visible:
        return "No matching skills on this page. Try an empty query or a smaller offset."
    lines = [
        f"{skill.name}: {skill.description.replace(chr(10), ' ').replace(chr(13), ' ')[:120]}"
        for skill in visible
    ]
    if offset + len(visible) < len(matches):
        lines.append(f"Next offset: {offset + len(visible)}")
    return "\n".join(lines)


@registry.register(
    name="skill_read",
    description="Read up to 800 characters of an enabled skill by skill_name. Use offset for the next page.",
)
def skill_read(skill_name: str, offset: int = 0) -> str:
    skills = skills_module.skill_registry
    skills.load_all()
    content = skills.get_skill_content(skill_name)
    if content is None:
        return "Error: skill not found or disabled"
    if offset < 0 or offset >= len(content):
        return "Error: offset is outside the skill content"
    end = min(len(content), offset + 800)
    skills.record_use(skill_name)
    return f"Skill {skill_name[:80]} [{offset}:{end}/{len(content)}]:\n{content[offset:end]}"


registry.declare_effects(
    {
        "skill_list": ("skill.read",),
        "skill_read": ("skill.read",),
    }
)
