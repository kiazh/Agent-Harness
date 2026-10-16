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
    needle = query.strip()
    if needle:
        # One ranking implementation for humans and automation: the canonical
        # typed search owns retrieval, applicability and ordering, so this
        # browse cannot drift from automatic selection. Disabled skills are
        # excluded automatically; the page shows applicable candidates first.
        from machina.skills.runtime import search_skills

        result = search_skills(skills, query, limit=offset + 4)
        page = [
            skills.get(candidate.skill_id) for candidate in result.candidates[offset : offset + 4]
        ]
        visible = [skill for skill in page if skill is not None]
        has_more = result.next_cursor is not None
    else:
        # Empty query browses the enabled catalog in stable registry order.
        matches = [skill for skill in skills.list_skills() if skill.enabled]
        visible = matches[offset : offset + 4]
        has_more = offset + len(visible) < len(matches)
    if not visible:
        return "No matching skills on this page. Try an empty query or a smaller offset."
    lines = [
        f"{skill.name}: {skill.description.replace(chr(10), ' ').replace(chr(13), ' ')[:120]}"
        for skill in visible
    ]
    if has_more:
        lines.append(f"Next offset: {offset + len(visible)}")
    return "\n".join(lines)


@registry.register(
    name="skill_read",
    description="Read skill instructions. query jumps to a relevant heading or passage; offset continues an excerpt; section reads one exact versioned section (revision from a previous section read detects edits). limit defaults to 800, up to 8000 characters. Read relevant workflow steps before using them.",
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
            "section": {
                "type": "string",
                "default": "",
                "description": (
                    "Optional section anchor/heading; reads that exact version-bound section. "
                    "Continuation responses say how to resume with section, revision, and offset"
                ),
            },
            "revision": {
                "type": "string",
                "default": "",
                "description": (
                    "Optional content revision from a previous section read; a mismatch "
                    "returns a stale notice instead of different text at the same address"
                ),
            },
        },
        "required": ["skill_name"],
    },
)
def skill_read(
    skill_name: str,
    offset: int = 0,
    limit: int = 800,
    query: str = "",
    section: str = "",
    revision: str = "",
) -> str:
    skills = skills_module.skill_registry
    skills.load_all()
    content = skills.get_skill_content(skill_name)
    if content is None:
        return "Error: skill not found or disabled"
    if section:
        return _skill_read_section(skill_name, skills, content, section, revision, offset, limit)
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


def _skill_read_section(
    skill_name: str,
    skills,
    content: str,
    section: str,
    revision: str,
    offset: int,
    limit: int,
) -> str:
    """Version-bound section read (A3 ``sections.read_skill_section``).

    A stale ``revision`` never serves a different passage: the caller is told
    to re-read. Legacy char-offset reads above are untouched.
    """
    from machina.skills.sections import read_skill_section

    if limit < 1 or limit > 8000:
        return "Error: limit must be between 1 and 8000"
    if offset < 0:
        return "Error: offset must be non-negative"
    skill = skills.get(skill_name)
    result = read_skill_section(
        skill,
        section,
        content=content,
        expected_revision=revision or None,
        offset=offset,
        limit=limit,
    )
    if result.stale:
        return (
            f"Error: skill {skill_name!r} section {section!r} was read against revision "
            f"{revision!r}, but the current content revision is {result.content_revision}. "
            "Re-read the section without a revision to continue."
        )
    if not result.text:
        detail = result.diagnostics[0].message if result.diagnostics else "no matching section"
        return f"Error: {detail}"
    skills.record_use(skill_name)
    served_end = offset + len(result.text)
    body = (
        f"Skill {skill_name[:80]} section {result.section_id} "
        f"[{offset}:{served_end}/{result.section_total}] revision {result.content_revision}:\n"
        f"{result.text}"
    )
    if result.continuation is not None:
        body += (
            "\n[Section continues; continue skill_read with "
            f"section={result.section_id!r}, revision={result.content_revision!r}, "
            f"offset={result.continuation}.]"
        )
    return body


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
