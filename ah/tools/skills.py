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
    from ah.skills.runtime import rank_skills

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
    if needle and not matches:
        matches = rank_skills(skills, query)
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
    description="Read skill instructions. offset continues an excerpt; limit is 800 by default, up to 8000 characters. Read remaining workflow steps before using them.",
)
def skill_read(skill_name: str, offset: int = 0, limit: int = 800) -> str:
    skills = skills_module.skill_registry
    skills.load_all()
    content = skills.get_skill_content(skill_name)
    if content is None:
        return "Error: skill not found or disabled"
    if offset < 0 or offset >= len(content):
        return "Error: offset is outside the skill content"
    if limit < 1 or limit > 8000:
        return "Error: limit must be between 1 and 8000"
    end = min(len(content), offset + limit)
    skills.record_use(skill_name)
    suffix = ""
    skill = skills.get(skill_name)
    if skill.source_type == "builtin-ecc" and end == len(content):
        suffix = "\nUse skill_resource to list supporting files. ECC root: " + str(
            skills._ecc_bundle.root
        )
    return (
        f"Skill {skill_name[:80]} [{offset}:{end}/{len(content)}]:\n{content[offset:end]}{suffix}"
    )


@registry.register(
    name="skill_resource",
    description="List or read ECC supporting files. Empty path lists the skill's files; path is relative to ECC root (e.g. skills/python-testing/references/example.md or agents/planner.md). Paginate with offset and limit (max 8000 characters). Reading does not execute scripts.",
)
def skill_resource(skill_name: str, path: str = "", offset: int = 0, limit: int = 4000) -> str:
    skills = skills_module.skill_registry
    skills.load_all()
    skill = skills.get(skill_name)
    if not skill or skill.source_type != "builtin-ecc":
        return "Error: supporting resources require a bundled ECC skill"
    if offset < 0 or not 1 <= limit <= 8000:
        return "Error: invalid offset or limit"
    bundle = skills._ecc_bundle
    if not path:
        paths = bundle.resource_paths(skill)
        text = (
            "\n".join(paths)
            or "No skill-local resources. Shared files are readable by their ECC-root path (agents/, scripts/, rules/, docs/)."
        )
    else:
        try:
            text = bundle.read_resource(path)
        except (OSError, ValueError) as exc:
            return f"Error: {exc}"
    if offset >= len(text):
        return "Error: offset is outside the resource"
    end = min(len(text), offset + limit)
    return f"ECC resource {path or skill_name} [{offset}:{end}/{len(text)}]:\n{text[offset:end]}"


registry.declare_effects(
    {
        "skill_list": ("skill.read",),
        "skill_read": ("skill.read",),
        "skill_resource": ("skill.read",),
    }
)
