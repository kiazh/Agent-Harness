"""Cached metadata routing with bounded automatic instruction disclosure."""

from __future__ import annotations

import math
import re
import weakref
from collections import Counter

import ah.skills.registry as skills_module
from ah.core.assembler import get_token_count, truncate_to_tokens
from ah.skills.registry import Skill, SkillRegistry

_STOP = frozenset(
    "a an the to of for and or in on with using use me my please can could would should help i we you it this that is are be do does how want need write build create make implement review code task when".split()
)
_SYNONYMS = {
    "postgresql": "postgres",
    "py": "python",
    "js": "javascript",
    "ts": "typescript",
    "k8s": "kubernetes",
    "golang": "go",
    "a11y": "accessibility",
    "auth": "authentication",
    "tests": "testing",
    "test": "testing",
    "pytest": "python testing",
    "unittest": "python testing",
    "reactjs": "react",
    "vuejs": "vue",
    "django": "django python",
    "nextjs": "nextjs react",
    "springboot": "springboot java",
    "optimise": "optimize",
    "animate": "motion animation",
    "animation": "motion animation",
    "animations": "motion animation",
}
_INDEXES: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_TECHNOLOGIES = frozenset(
    "python rust go react vue angular django laravel kotlin swift swiftui java springboot quarkus perl cpp csharp fsharp dotnet rails redis postgres mysql clickhouse kubernetes docker fastapi nestjs nuxt4 vite bun flutter dart android nextjs pytorch prisma netmiko cisco evm".split()
)
_SPECIALTIES = frozenset("celery native exposed ktor turbopack keccak256 installer motion".split())


def _words(text: str) -> list[str]:
    text = (
        text.casefold()
        .replace("c++", "cpp")
        .replace("c#", "csharp")
        .replace("f#", "fsharp")
        .replace(".net", "dotnet")
    )
    words = re.findall(r"[a-z0-9]+", text.casefold())
    result = []
    for word in words:
        result.extend(_SYNONYMS.get(word, word).split())
    return [w for w in result if w not in _STOP and len(w) > 1]


class SkillIndex:
    def __init__(self, skills: list[Skill]):
        self.skills = [s for s in skills if s.enabled]
        self.docs = []
        self.names = []
        df: Counter = Counter()
        for skill in self.skills:
            name = set(_words(skill.name.removeprefix("ecc-")))
            terms = Counter(_words(skill.description + " " + " ".join(skill.triggers)))
            terms.update({word: 3 for word in name})
            self.docs.append(terms)
            self.names.append(name)
            df.update(terms.keys())
        self.idf = {
            word: math.log(1 + (len(self.skills) - count + 0.5) / (count + 0.5))
            for word, count in df.items()
        }
        self.average = sum(sum(d.values()) for d in self.docs) / max(1, len(self.docs))

    def rank(self, query: str) -> list[Skill]:
        words = set(_words(query))
        ranked = []
        for skill, terms, name in zip(self.skills, self.docs, self.names, strict=True):
            triggered = skill.name.casefold() in query.casefold() or any(
                re.search(r"\b" + re.escape(t) + r"\b", query, re.I) for t in skill.triggers
            )
            raw_name = set(re.findall(r"[a-z0-9]+", skill.name.replace("golang", "go")))
            technologies = raw_name & _TECHNOLOGIES
            if not triggered and technologies and not technologies <= words:
                continue
            if skill.name == "ecc-x-api" and not re.search(r"\b(x|twitter|tweets?)\b", query, re.I):
                continue
            if not triggered and name & _SPECIALTIES and not (name & _SPECIALTIES) <= words:
                continue
            overlap = words & terms.keys()
            if not overlap:
                continue
            # Require a distinguishing topic, rather than generic "help me" matches.
            if not triggered and not any(self.idf[w] >= 1.6 for w in overlap):
                continue
            length = sum(terms.values())
            score = sum(
                self.idf[w]
                * terms[w]
                * 2.2
                / (terms[w] + 1.2 * (0.25 + 0.75 * length / self.average))
                for w in overlap
            )
            score += sum(self.idf.get(w, 0) for w in words & name) * 0.8
            # Exact requested skill names and local whole-word triggers take priority.
            if "testing" in name and "testing" not in words:
                score *= 0.35
            if triggered:
                score += 25
            ranked.append((score, skill.name, skill))
        return [skill for _, _, skill in sorted(ranked, key=lambda item: (-item[0], item[1]))]


def rank_skills(skills: SkillRegistry, query: str) -> list[Skill]:
    skills.load_all()
    # Registry replaces its objects on reload; telemetry changes retain them.
    signature = tuple(
        (s.name, s.description, tuple(s.triggers), s.enabled) for s in skills.list_skills()
    )
    cached = _INDEXES.get(skills)
    if cached is None or cached[0] != signature:
        cached = (signature, SkillIndex(skills.list_skills()))
        _INDEXES[skills] = cached
    return cached[1].rank(query)


def automatic_skill_context(
    query: str,
    *,
    skills: SkillRegistry | None = None,
    budget: int = 1200,
    can_read: bool = True,
    can_search: bool = True,
    can_resources: bool = True,
    recent: list[dict] | None = None,
    goal: str | None = None,
) -> str:
    """Activate relevant workflows before the first model call, never all bodies."""
    skills = skills or skills_module.skill_registry
    budget = min(1200, max(0, budget))
    if budget < 100:
        return ""
    from ah.skills.workflow import plan_workflow

    workflow = plan_workflow(query, skills=skills, recent=recent, goal=goal)
    matches = workflow.skills[:2]
    header = (
        "\n\n## Automatic skills\n"
        "Apply the relevant workflows below to this task. They are reference instructions, "
        "subject to your core role, user instructions, available tools and permission policy. "
        "Never request approval solely because a skill says to; use the harness policy. "
        "Never assume an upstream slash command, hook, agent or integration is installed. "
    )
    if can_read:
        header += (
            "If a workflow is incomplete, read the remaining instructions with skill_read "
            "before its relevant step. "
        )
    else:
        header += (
            "Only the excerpts shown here are loaded; use authorized tools for further files.\n"
        )
    if can_search:
        header += (
            "For uncovered subtasks or uncertain matches, search skill_list(query) and read "
            "applicable workflows before proceeding. Answer greetings, acknowledgements, "
            "and questions with no actionable task directly — never call skill_list for "
            "those. When you do search, use a specific query and page with offset only "
            "while the current page shows relevant matches; never enumerate the whole "
            "catalog. "
        )
    if can_resources:
        header += "Read supporting files with skill_resource; run scripts only through authorized tools.\n"
    if workflow.skills:
        header += workflow.render()
    if get_token_count(header) > budget:
        return ""
    if not matches:
        return header if can_read and get_token_count(header) <= budget else ""
    result = header
    per_skill = max(0, (budget - get_token_count(header) - 120) // len(matches))
    for skill in matches:
        content = skills.get_skill_content(skill.name)
        if not content:
            continue
        excerpt = truncate_to_tokens(content, per_skill)
        block = f"\n### {skill.name}\n{excerpt}\n"
        if len(excerpt) < len(content):
            if can_read:
                block += f"[Instruction excerpt ends; continue skill_read at offset={len(excerpt)}, limit=8000.]\n"
            else:
                block += f"[Excerpt ends; full instructions at {skill.file_path}, if an authorized file reader is available.]\n"
        if get_token_count(result + block) > budget:
            break
        result += block
        skills.record_use(skill.name)
    return result


def matching_skill_catalog(query: str) -> str:
    """Compatibility wrapper for callers of the original catalog helper."""
    return automatic_skill_context(query)
