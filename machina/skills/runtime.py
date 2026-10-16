"""Cached metadata routing with bounded automatic instruction disclosure."""

from __future__ import annotations

import math
import re
import weakref
from collections import Counter

import machina.skills.registry as skills_module
import machina.skills.search as search_module
from machina.core.assembler import get_token_count, truncate_to_tokens
from machina.skills.registry import Skill, SkillRegistry

_INDEXES: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
# Alias normalization (stopwords, synonyms, C++/C#/.NET folding) now lives in
# ``machina.skills.search`` so the ranking and the explanations it builds use
# one home; these names stay re-exported because workflow.py imports them.
_TECHNOLOGIES = search_module.TECHNOLOGY_MARKERS
_SPECIALTIES = search_module.SPECIALTY_MARKERS


def _words(text: str) -> list[str]:
    """Canonical tokens; single home in :mod:`machina.skills.search`."""
    return search_module.normalize_tokens(text)


class SkillIndex:
    def __init__(self, skills: list[Skill], *, include_disabled: bool = False):
        self.skills = list(skills) if include_disabled else [s for s in skills if s.enabled]
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
        self.df = df
        self.doc_count = len(self.skills)
        self.idf = {
            word: math.log(1 + (len(self.skills) - count + 0.5) / (count + 0.5))
            for word, count in df.items()
        }
        self.average = sum(sum(d.values()) for d in self.docs) / max(1, len(self.docs))

    def rank(self, query: str, *, weights: dict[str, float] | None = None) -> list[Skill]:
        words = set(_words(query))
        ranked = []
        for skill, terms, name in zip(self.skills, self.docs, self.names, strict=True):
            triggered = skill.name.casefold() in query.casefold() or any(
                re.search(r"(?<![A-Za-z0-9_])" + re.escape(t) + r"(?![A-Za-z0-9_])", query, re.I)
                for t in skill.triggers
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
            score *= max(0.8, min(1.2, (weights or {}).get(skill.name, 1)))
            explicit = skill.name.casefold() in query.casefold()
            ranked.append((explicit, score, skill.name, skill))
        return [
            skill
            for _, _, _, skill in sorted(ranked, key=lambda item: (-item[0], -item[1], item[2]))
        ]


def rank_skills(
    skills: SkillRegistry, query: str, *, weights: dict[str, float] | None = None
) -> list[Skill]:
    skills.load_all()
    # Registry replaces its objects on reload; telemetry changes retain them.
    signature = tuple(
        (s.name, s.description, tuple(s.triggers), s.enabled) for s in skills.list_skills()
    )
    cached = _INDEXES.get(skills)
    if cached is None or cached[0] != signature:
        cached = (signature, SkillIndex(skills.list_skills()))
        _INDEXES[skills] = cached
    return cached[1].rank(query, weights=weights)


def search_skills(
    skills: SkillRegistry,
    query: str,
    *,
    weights: dict[str, float] | None = None,
    limit: int = 10,
    cursor: str | None = None,
    include_excluded: bool = False,
    available_tools: set[str] | frozenset[str] | list[str] | tuple[str, ...] | None = None,
    typo_tolerance: bool = False,
) -> search_module.SearchResult:
    """Typed, deterministic skill search (spec §9) over this registry.

    Reuses the cached lexical index as the fast baseline path (``rank_skills``
    and this function share ``_INDEXES``; behavior of ``rank_skills`` is
    unchanged). Auto mode excludes disabled skills entirely; the browsing path
    (``include_excluded=True``, used by catalog UIs) keeps them in the data
    flagged ``excluded``. Scores are ranking signals with named components —
    never calibrated probabilities. ``typo_tolerance`` is OFF by default.
    """
    skills.load_all()
    all_skills = skills.list_skills()
    # Registry replaces its objects on reload; telemetry changes retain them.
    signature = tuple((s.name, s.description, tuple(s.triggers), s.enabled) for s in all_skills)
    cached = _INDEXES.get(skills)
    if cached is None or cached[0] != signature:
        cached = (signature, SkillIndex(all_skills))
        _INDEXES[skills] = cached
    index = cached[1]
    if include_excluded and any(not skill.enabled for skill in all_skills):
        # Browsing is a rare, user-triggered path: build a one-shot full index
        # instead of growing the per-turn cache the automatic path shares.
        index = SkillIndex(all_skills, include_disabled=True)
    return search_module.execute_search(
        index,
        query,
        weights=weights,
        limit=limit,
        cursor=cursor,
        available_tools=available_tools,
        typo_tolerance=typo_tolerance,
    )


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
    skill_weights: dict[str, float] | None = None,
    activated: list[str] | None = None,
) -> str:
    """Activate relevant workflows before the first model call, never all bodies."""
    skills = skills or skills_module.skill_registry
    budget = min(1200, max(0, budget))
    if budget < 100:
        return ""
    from machina.skills.workflow import plan_workflow

    workflow = plan_workflow(
        query, skills=skills, recent=recent, goal=goal, skill_weights=skill_weights
    )
    matches = workflow.skills[:2]
    header = (
        "\n\n## Automatic skills\n"
        "Relevant references for this task — consult a workflow only if the task calls "
        "for it; otherwise ignore these and act as a general agent. They are reference "
        "instructions, subject to your core role, user instructions, available tools "
        "and permission policy. Answer greetings and self-contained conceptual questions "
        "directly; requested live settings, repository evidence or external facts need read tools. "
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
            "and self-contained conceptual questions directly — never call skill_list for "
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
        # No skill evidence: inject nothing. The system prompt already
        # covers general conduct — every turn need not pay ~116 tokens
        # of discovery boilerplate.
        return ""
    result = header
    per_skill = max(0, (budget - get_token_count(header) - 120) // len(matches))
    for skill in matches:
        content = skills.get_skill_content(skill.name)
        if not content:
            continue
        excerpt = truncate_to_tokens(content, per_skill)
        if not content.startswith(excerpt):
            # A token cut can split a codepoint; the decoder then yields a
            # replacement character, so the excerpt is not a real prefix of
            # the content. Clamp to the prefix actually served — the
            # continuation offset below is len(excerpt), and excerpt plus the
            # continuation must tile the content without gaps or repeats.
            limit = min(len(content), len(excerpt))
            prefix_length = 0
            while prefix_length < limit and content[prefix_length] == excerpt[prefix_length]:
                prefix_length += 1
            excerpt = content[:prefix_length]
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
        if activated is not None:
            activated.append(skill.name)
    return result
