"""Cached metadata routing with bounded automatic instruction disclosure."""

from __future__ import annotations

import math
import weakref
from collections import Counter
from dataclasses import replace

import machina.skills.registry as skills_module
import machina.skills.search as search_module
from machina.core.assembler import get_token_count, truncate_to_tokens
from machina.skills import events as events_module
from machina.skills.planned_steps import (
    DisclosurePlan,
    StageState,
    normalize_constraints,
    normalize_stage_state,
    plan_disclosure,
    task_revision_from,
)
from machina.skills.registry import Skill, SkillRegistry
from machina.skills.revisions import RevisionRegistry, task_state_revision

_INDEXES: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
# Alias normalization (stopwords, synonyms, C++/C#/.NET folding) lives in
# ``machina.skills.search`` so the ranking and the explanations it builds use
# one home. ``_TECHNOLOGIES`` is re-exported here because
# ``machina.core.task_descriptor`` imports it from this module.
_TECHNOLOGIES = search_module.TECHNOLOGY_MARKERS


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

    The one ranking implementation: the cached lexical index feeds
    ``search.execute_search`` and every consumer (automatic selection, the
    human browse path, the ``skills.ports`` dispatch) reads its result. Auto
    mode excludes disabled skills entirely; the browsing path
    (``include_excluded=True``, used by catalog UIs) keeps them in the data
    flagged ``excluded``. Scores are ranking signals with named components —
    never calibrated probabilities. ``typo_tolerance`` is OFF by default.
    Results carry ``catalog_revision`` / ``policy_revision`` /
    ``content_revision``; :func:`revisions_for` exposes the same identities for
    caller-side caches (see :meth:`RevisionRegistry.identity_for`).
    """
    skills.load_all()
    all_skills = skills.list_skills()
    # Registry replaces its objects on reload; telemetry changes retain them.
    # The signature must therefore cover what the cached index serves: the
    # routing fields AND the content-derived revision (body + version + bundle
    # metadata + typed metadata digest, ``search.content_revision``). Without
    # the digest an out-of-band SKILL.md body or v2-metadata edit that leaves
    # name/description/triggers/enabled unchanged would keep serving the stale
    # index: candidates would carry a ``content_revision`` that the very next
    # section read rejects as stale (P0-8 / spec §11 freshness contract).
    signature = tuple(
        (
            s.name,
            s.description,
            tuple(s.triggers),
            s.enabled,
            getattr(s, "source_type", "local"),
            search_module.content_revision(s),
        )
        for s in all_skills
    )
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


def automatic_skills(
    skills: SkillRegistry,
    query: str,
    *,
    weights: dict[str, float] | None = None,
    available_tools: set[str] | frozenset[str] | list[str] | tuple[str, ...] | None = None,
) -> list[Skill]:
    """Canonical automatic-selection order: enabled, non-excluded skills.

    The single automatic consumer of :func:`search_skills` (used by
    ``plan_workflow`` and the ``skills.ports`` dispatch). Disabled and
    hard-excluded candidates never appear — the automatic scope. Exact-ID/name
    matches, boundary-aware triggers and deterministic ties come from
    ``search_skills`` unchanged. No second scoring implementation exists: the
    legacy ``SkillIndex.rank`` selector was removed when this helper replaced
    its only product callers.

    Ordering is by canonical relevance score (a stable sort over the canonical
    list, so applicability-bucket order breaks score ties exactly as
    ``search_skills`` does). Planning has no tool-availability or stack fact to
    resolve conditional candidates, and the corpus routing gates are defined on
    relevance; the applicability decision is therefore used as the automatic
    *scope* (exclude disabled / hard-excluded / known-missing-tool), while the
    ranked order stays the relevance order planning has always used.

    One ported legacy preference survives here (it is a scope decision, not a
    second matcher): a skill whose name advertises ``testing`` is only a
    specialist when the task itself names testing, so it does not displace an
    equally relevant non-testing skill on a build query (spec §9.2 — a
    technical skill must not crowd out a generic relevant one).
    """

    result = search_skills(
        skills,
        query,
        weights=weights,
        available_tools=available_tools,
        limit=max(1, len(skills.list_skills())),
    )
    words = set(_words(query))
    eligible: list[tuple[search_module.SearchCandidate, Skill]] = []
    for candidate in result.candidates:
        if candidate.applicability == search_module.EXCLUDED:
            continue
        skill = skills.get(candidate.skill_id)
        if skill is not None and skill.enabled:
            eligible.append((candidate, skill))
    # Stable: equal relevance keeps the canonical tie-break (source trust then
    # case-insensitive name) already applied in ``result.candidates``.
    eligible.sort(key=lambda pair: -pair[0].score)
    return _apply_testing_preference([skill for _, skill in eligible], words)


def _apply_testing_preference(matches: list[Skill], query_words: set[str]) -> list[Skill]:
    """Move name-announced ``testing`` specialists behind generic matches.

    Ported from the retired legacy selector (``SkillIndex.rank``) as a stable
    reorder within the canonical order, so deterministic ties and canonical
    precedence are preserved for every other skill. A task that names testing
    (``test``/``tests``/``testing`` all normalize to ``testing``) is unaffected.
    """

    if "testing" in query_words:
        return matches
    generic: list[Skill] = []
    testing: list[Skill] = []
    for skill in matches:
        if "testing" in set(_words(skill.name.removeprefix("ecc-"))):
            testing.append(skill)
        else:
            generic.append(skill)
    return generic + testing


def revisions_for(
    skills: SkillRegistry | None = None, *, task_state: dict | None = None
) -> RevisionRegistry:
    """§11 revision identities for the registry's currently loaded catalog.

    Extends :meth:`SkillRegistry.revisions` with an explicit task-state
    revision so search/disclosure caches can be keyed by exactly the
    identities that affect them: ``identity_for("search_results")`` =
    (catalog, ranking policy), ``identity_for("disclosure_plan")`` =
    (catalog, ranking policy, task state). Telemetry identity is reported but
    is deliberately absent from both — recording activity never invalidates
    content-derived caches.
    """
    registry = skills or skills_module.skill_registry
    snapshot = registry.revisions()
    revision = task_state_revision(task_state) if task_state is not None else 0
    if revision == 0:
        return snapshot
    return replace(snapshot, task_state=str(revision))


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
    task_state: dict | None = None,
    stage_state: object = None,
) -> str:
    """Activate relevant workflows before the first model call, never all bodies.

    Disclosure derives from ``SkillWorkflow.steps`` (spec §10.1): the same
    authoritative order the workflow line displays — never a ``skills[:2]``
    prefix. With an explicit stage fact (``stage_state``, or the
    ``stage``/``stage_source`` keys of ``task_state``) the current-stage
    sections and their prerequisites are disclosed; without one, only
    bootstrap material is loaded and later sections stay discoverable through
    locators (spec §10.2). Skills without metadata/sections keep the pre-A3
    whole-body excerpt path byte-for-byte. The rollback switch
    ``skills_disclosure_planner`` (read via ``config.get``; default ON)
    restores the legacy path entirely.
    """
    skills = skills or skills_module.skill_registry
    budget = min(1200, max(0, budget))
    if budget < 100:
        return ""
    from machina.skills.workflow import plan_workflow

    workflow = plan_workflow(
        query,
        skills=skills,
        recent=recent,
        goal=goal,
        skill_weights=skill_weights,
        task_state=task_state,
    )
    # Selection is an observed event (spec §13): bounded, structured reasons
    # derived from the same planned steps the disclosure uses.
    for step in workflow.steps:
        events_module.record_selected_for_plan(
            skill_id=step.skill_id,
            step_id=step.step_id,
            stage=step.stage,
            reasons=(f"label={step.label}", f"provenance={step.provenance}"),
        )
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
    if not workflow.steps:
        # No skill evidence: inject nothing. The system prompt already
        # covers general conduct — every turn need not pay ~116 tokens
        # of discovery boilerplate.
        return ""
    raw_state = stage_state if stage_state is not None else task_state
    state = normalize_stage_state(raw_state)
    if state is not None:
        events_module.record_stage_requested_or_entered(state.stage, source=state.source)
    constraints = (
        normalize_constraints(task_state.get("constraints")) if isinstance(task_state, dict) else ()
    )
    if not _disclosure_planner_enabled():
        return _legacy_disclosure(header, workflow, skills, budget, can_read, activated)
    return _planned_disclosure(
        header=header,
        workflow=workflow,
        skills=skills,
        budget=budget,
        can_read=can_read,
        state=state,
        constraints=constraints,
        task_state=task_state,
        activated=activated,
    )


#: Rollback switch for stage-aware disclosure, registered in
#: ``machina.core.config.DEFAULTS`` (``skills_disclosure_planner: bool =
#: True``) so both ``config.set`` and ``MACHINA_SKILLS_DISCLOSURE_PLANNER``
#: env overrides resolve as booleans. A missing/unregistered key still means
#: the planner is ON.
_DISCLOSURE_PLANNER_KEY = "skills_disclosure_planner"


def _config_flag(raw: object) -> bool:
    """Boolean-ish reader for the rollback key (safe default ON)."""
    if raw is None:
        return True
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().casefold() not in ("0", "false", "no", "off", "disabled")


def _disclosure_planner_enabled() -> bool:
    try:
        from machina.core.config import config

        return _config_flag(config.get(_DISCLOSURE_PLANNER_KEY))
    except (ImportError, AttributeError, ValueError):
        return True


def _legacy_disclosure(
    header: str,
    workflow: object,
    skills: SkillRegistry,
    budget: int,
    can_read: bool,
    activated: list[str] | None,
) -> str:
    """Pre-A3 first-two whole-body excerpt path (rollback switch target)."""
    matches = workflow.skills[:2]
    if not matches:
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
        events_module.record_instruction_section_disclosed(
            skill_id=skill.name,
            section_id="body",
            content_revision=search_module.content_revision(skill),
        )
        if activated is not None:
            activated.append(skill.name)
    return result


def _not_loaded_reminder(plan: DisclosurePlan, can_read: bool) -> list[str]:
    """Compact locator lines for sections that were not loaded (bounded)."""
    entries: list[str] = []
    seen: set[str] = set()
    locators = [item.locator for item in plan.omitted if item.reason != "unavailable"]
    locators.extend(item.locator for item in plan.unresolved_prerequisites if item.locator)
    for locator in locators:
        if not locator.section_id:
            continue
        text = locator.describe()
        if text in seen:
            continue
        seen.add(text)
        entries.append(text)
    if not entries:
        return []
    shown = entries[:6]
    first = "Not loaded now: " + "; ".join(shown) + "."
    if len(entries) > len(shown):
        first = first[:-1] + f" (+{len(entries) - len(shown)} more)."
    lines = [first]
    if can_read:
        lines.append("Read each section with skill_read before its step.")
    return lines


def _planned_disclosure(
    *,
    header: str,
    workflow: object,
    skills: SkillRegistry,
    budget: int,
    can_read: bool,
    state: StageState | None,
    constraints: tuple[str, ...],
    task_state: dict | None,
    activated: list[str] | None,
) -> str:
    """Stage-aware disclosure derived from ``workflow.steps`` (spec §10.1–10.3)."""
    result = header
    if constraints:
        result += "\nTask constraints: " + "; ".join(constraints) + ".\n"
    if state is not None:
        result += f"\nCurrent stage ({state.source}): {state.stage}.\n"
    plan = plan_disclosure(
        workflow.steps,
        skill_for=skills.get,
        content_for=skills.get_skill_content,
        budget=max(0, budget - get_token_count(result)),
        stage_state=state,
        task_revision=task_revision_from(task_state),
        catalog_revision=workflow.catalog_revision,
        constraints=constraints,
        token_count=get_token_count,
        token_basis="assembler.get_token_count",
    )
    revealed: dict[str, None] = {}
    for entry in plan.chosen:
        block = f"\n### {entry.skill_id} \u00b7 {entry.heading}\n{entry.text}\n"
        if get_token_count(result + block) > budget:
            break
        result += block
        revealed.setdefault(entry.skill_id, None)
        events_module.record_instruction_section_disclosed(
            skill_id=entry.skill_id,
            section_id=entry.section_id,
            step_id=entry.step_id,
            stage=entry.stage,
            content_revision=entry.content_revision,
        )
    if plan.whole_body:
        per_unit = max(0, (budget - get_token_count(result) - 120) // len(plan.whole_body))
        for unit in plan.whole_body:
            excerpt = truncate_to_tokens(unit.content, per_unit)
            if not unit.content.startswith(excerpt):
                limit = min(len(unit.content), len(excerpt))
                prefix_length = 0
                while (
                    prefix_length < limit and unit.content[prefix_length] == excerpt[prefix_length]
                ):
                    prefix_length += 1
                excerpt = unit.content[:prefix_length]
            block = f"\n### {unit.skill_id}\n{excerpt}\n"
            if len(excerpt) < len(unit.content):
                if can_read:
                    block += (
                        "[Instruction excerpt ends; continue skill_read at "
                        f"offset={len(excerpt)}, limit=8000.]\n"
                    )
                else:
                    block += (
                        f"[Excerpt ends; full instructions at {unit.file_path}, "
                        "if an authorized file reader is available.]\n"
                    )
            if get_token_count(result + block) > budget:
                break
            result += block
            revealed.setdefault(unit.skill_id, None)
            events_module.record_instruction_section_disclosed(
                skill_id=unit.skill_id,
                section_id="body",
                step_id=unit.step_id,
                content_revision=unit.content_revision,
            )
    for line in _not_loaded_reminder(plan, can_read):
        if get_token_count(result + "\n" + line) > budget:
            break
        result += "\n" + line
    for skill_id in revealed:
        skills.record_use(skill_id)
        if activated is not None:
            activated.append(skill_id)
    return result
