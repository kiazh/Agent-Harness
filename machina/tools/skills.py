"""Progressive skill discovery for the running agent."""

from __future__ import annotations

import re

import machina.skills.registry as skills_module
from machina.tools.base import registry


@registry.register(
    name="skill_list",
    description="Search and rank skills for a task by query (4 per page). Use a specific query; page with offset only while results stay relevant. Never enumerate the whole catalog; empty query browses.",
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "default": "",
                "description": (
                    "Case-insensitive term matched against skill names, descriptions, "
                    "and triggers; leave empty to browse (4 skills per page)"
                ),
            },
            "offset": {
                "type": "integer",
                "minimum": 0,
                "default": 0,
                "description": "Number of matching skills to skip when paging (default 0)",
            },
        },
        "required": [],
    },
)
def skill_list(query: str = "", offset: int = 0) -> str:
    skills = skills_module.skill_registry
    skills.load_all()
    if offset < 0:
        return "Error: offset must be non-negative"
    from machina.skills.runtime import rank_skills

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
    if needle:
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
    description="Read skill instructions. query jumps to a relevant heading or passage; offset continues an excerpt. limit defaults to 800, up to 8000 characters. Read relevant workflow steps before using them.",
    parameters={
        "type": "object",
        "properties": {
            "skill_name": {
                "type": "string",
                "description": "Skill name to read, e.g. 'python-patterns'",
            },
            "offset": {
                "type": "integer",
                "minimum": 0,
                "default": 0,
                "description": "Character offset into the skill text to start from (default 0)",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 8000,
                "default": 800,
                "description": "Maximum characters to return (1–8000, default 800)",
            },
            "query": {
                "type": "string",
                "default": "",
                "description": (
                    "Optional keyword; jumps to the first matching heading or passage "
                    "instead of the offset"
                ),
            },
        },
        "required": ["skill_name"],
    },
)
def skill_read(skill_name: str, offset: int = 0, limit: int = 800, query: str = "") -> str:
    skills = skills_module.skill_registry
    skills.load_all()
    content = skills.get_skill_content(skill_name)
    if content is None:
        return "Error: skill not found or disabled"
    if query:
        # Jump to a matching heading or passage instead of paging the entire manual.
        match = re.search(re.escape(query), content[offset:], re.I)
        if match is None:
            return "No matching passage. Try a shorter keyword or read without query."
        position = offset + match.start()
        offset = content.rfind("\n", 0, position) + 1
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
    description="List or read ECC supporting files. Empty path lists files; path accepts skill-relative references/scripts or ECC-root paths such as agents/planner.md. Paginate with offset and limit (max 8000 characters). Reading does not execute scripts.",
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
            if path not in bundle.manifest["files"]:
                from pathlib import PurePosixPath

                path = str(PurePosixPath(skill.metadata["bundle_path"]).parent / path)
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
