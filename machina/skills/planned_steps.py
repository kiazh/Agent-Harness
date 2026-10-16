"""One authoritative planned-step representation and stage-honest disclosure (spec §10).

Selection, display order, and disclosure order derive from the same
``PlannedStep`` sequence (:func:`build_planned_steps`) — the pre-A3 code kept
two independent lists (``selected`` insertion order vs sorted ``stages``), so
the excerpts in the prompt could contradict the rendered workflow (A0 red
observation, fixed by D01).

Stage honesty (spec §10.2): a step's ``stage`` is the *planned* phase derived
by the deterministic pipeline policy, never a claim about what the user is
doing. The *current* phase only enters through an explicit
:class:`StageState` (task state, command, or observed runtime event); a
sentence such as "now testing" can never select a stage. When no honest phase
state exists, only bootstrap material (untitled preamble + untagged sections)
is disclosed and every later section stays discoverable through stable
locators.

The disclosure contract (spec §10.3) is :class:`DisclosurePlan`: revisions,
chosen sections with exact skill/content revisions, required prerequisites,
included token estimate with its basis, omitted sections with continuation
locators, unresolved prerequisites, and an explicit ``missing_context`` list.
Mandatory coherent sections are loaded whole or omitted whole — a hard cap
never cuts a prerequisite or safety sequence into a misleading fragment.

token estimates use the documented ``char/4`` basis by default (the same
fallback basis as ``machina.core.assembler.TokenCounter``); callers with the
assembler available pass ``token_count=get_token_count`` and a matching
``token_basis`` so planning is measured against the real local counter.
Nothing here calls a provider, embedding, tool, or spawned process.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from machina.skills.metadata import (
    CODE_DIAGNOSTICS_TRUNCATED,
    STAGES,
    MetadataDiagnostic,
    SectionMetadata,
    SkillMetadata,
)
from machina.skills.search import content_revision as skill_content_revision
from machina.skills.sections import (
    SectionCycleError,
    bound_sections,
    build_section_graph,
    content_fingerprint,
    index_sections,
)

__all__ = [
    "CODING_STAGE_ORDER",
    "MAX_PLAN_DIAGNOSTICS",
    "PIPELINE_STAGES",
    "STAGE_SOURCES",
    "STEP_PROVENANCE",
    "TOKEN_BASIS",
    "ChosenSection",
    "DisclosurePlan",
    "OmittedSection",
    "PlannedStep",
    "SectionLocator",
    "StageState",
    "UnresolvedPrerequisite",
    "WholeBodyUnit",
    "build_planned_steps",
    "estimate_tokens",
    "normalize_constraints",
    "normalize_stage_state",
    "order_specs",
    "plan_disclosure",
    "task_revision_from",
]

#: Cheap, documented token basis: the repo's tiktoken fallback (``len//4``).
TOKEN_BASIS = "char/4"

#: Who caused a step / phase fact (spec §10.2). ``policy`` = deterministic
#: planning rule; the others mirror the user/model/observed categories.
STEP_PROVENANCE = ("user", "model", "observed", "policy")
STAGE_SOURCES = ("user", "model", "observed")

#: Coding pipeline labels in execution order (moved here from ``workflow.py``
#: so one table defines the authoritative sequence). The task-specific anchor
#: (a user-requested skill, then the stack/domain match) leads the plan because
#: the first declared excerpts must be the most task-relevant ones; the generic
#: pipeline follows and ends with verification. A0 observed that the rendered
#: stage list used to sort ``inspect`` ahead of ``domain`` while selection and
#: disclosure led with ``domain`` — this single table ends that disagreement.
CODING_STAGE_ORDER: dict[str, int] = {
    "requested workflow": 0,
    "domain": 1,
    "inspect and plan": 2,
    "security preflight and review": 3,
    "implement and test": 4,
    "specialist": 5,
    "verify": 6,
}

#: Honest planned phase per pipeline label; ``""`` means the label makes no
#: phase claim (the step is positioned by policy only).
PIPELINE_STAGES: dict[str, str] = {
    "requested workflow": "",
    "domain": "",
    "inspect and plan": "inspect",
    "security preflight and review": "review",
    "implement and test": "implement",
    "verify": "verify",
    "specialist": "",
    "research": "inspect",
    "verify sources": "verify",
}

#: Policy sequencing edges between pipeline labels (same-plan steps only).
_STEP_PREREQUISITES: dict[str, tuple[str, ...]] = {
    "implement and test": ("inspect and plan",),
    "verify": ("implement and test",),
    "verify sources": ("research",),
}

#: Bounds: one bad skill can never flood a disclosure plan.
MAX_PLAN_DIAGNOSTICS = 16
MAX_OMITTED = 12
MAX_WHOLE_BODY_UNITS = 2
MAX_CONSTRAINTS = 4
MAX_CONSTRAINT_CHARS = 120

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def estimate_tokens(text: str) -> int:
    """Bounded cheap estimate: chars/4, min 1 for non-empty text (0 for empty)."""
    if not text:
        return 0
    return max(1, len(text) // 4)


@dataclass(frozen=True)
class StageState:
    """Explicit phase fact from task state, a command, or an observed event.

    ``source`` records who established the fact; a model-proposed transition
    is ``model`` and does not by itself establish verification (spec §10.2).
    """

    stage: str
    source: str = "user"
    revision: int = 0

    def __post_init__(self) -> None:
        if self.stage not in STAGES:
            raise ValueError(f"stage must be one of {sorted(STAGES)}, got {self.stage!r}")
        if self.source not in STAGE_SOURCES:
            raise ValueError(f"source must be one of {STAGE_SOURCES}, got {self.source!r}")


def normalize_stage_state(raw: Any) -> StageState | None:
    """Bound an explicit stage fact; anything unreliable degrades to ``None``."""
    if raw is None or isinstance(raw, StageState):
        return raw
    if not isinstance(raw, Mapping):
        return None
    stage = raw.get("stage", raw.get("current_stage"))
    if not isinstance(stage, str) or stage not in STAGES:
        return None
    source = raw.get("stage_source", raw.get("source", "user"))
    if source is None:
        source = "user"
    if not isinstance(source, str) or source not in STAGE_SOURCES:
        return None
    revision = raw.get("stage_revision", raw.get("revision", 0))
    try:
        revision = max(0, int(revision))
    except (TypeError, ValueError):
        revision = 0
    return StageState(stage=stage, source=source, revision=revision)


def normalize_constraints(raw: Any) -> tuple[str, ...]:
    """Bound explicit task constraints (bounded count and length)."""
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return ()
    values = [
        str(value).strip()[:MAX_CONSTRAINT_CHARS]
        for value in raw
        if isinstance(value, str) and str(value).strip()
    ]
    return tuple(dict.fromkeys(values))[:MAX_CONSTRAINTS]


def task_revision_from(raw: Any) -> int:
    """Read the explicit task-state revision from a mapping (0 when absent)."""
    if not isinstance(raw, Mapping):
        return 0
    try:
        return max(0, int(raw.get("revision", 0)))
    except (TypeError, ValueError):
        return 0


def _slug(text: str) -> str:
    return _SLUG_RE.sub("-", (text or "").casefold()).strip("-") or "step"


@dataclass(frozen=True)
class PlannedStep:
    """One referenced skill/section unit in the authoritative plan order.

    ``section_ids`` lists the declared sections this step can draw from: the
    stage-matching ones when the label carries a phase, otherwise all declared
    sections (empty for v1 skills).
    """

    step_id: str
    label: str
    stage: str
    skill_id: str
    section_ids: tuple[str, ...]
    provenance: str
    estimate_tokens: int
    prerequisites: tuple[str, ...]
    estimate_basis: str = TOKEN_BASIS

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "label": self.label,
            "stage": self.stage,
            "skill_id": self.skill_id,
            "section_ids": list(self.section_ids),
            "provenance": self.provenance,
            "estimate_tokens": self.estimate_tokens,
            "estimate_basis": self.estimate_basis,
            "prerequisites": list(self.prerequisites),
        }


def order_specs(specs: Sequence[tuple[str, str]], *, kind: str) -> list[tuple[str, str]]:
    """Deterministic execution order: stable stage sort for coding, else insertion."""
    if kind == "coding":
        return sorted(specs, key=lambda item: CODING_STAGE_ORDER.get(item[0], 99))
    return list(specs)


def build_planned_steps(
    specs: Sequence[tuple[str, str]],
    *,
    kind: str,
    skills_by_name: Mapping[str, Any],
    content_for: Callable[[str], str | None] | None = None,
    stage_state: StageState | Mapping[str, Any] | None = None,
    task_revision: int = 0,
) -> tuple[PlannedStep, ...]:
    """Build the single authoritative step sequence for one plan.

    ``specs`` are ``(label, skill_id)`` pairs in insertion order; the returned
    steps are ordered by :func:`order_specs` and carry stable ids, the planned
    phase, referenced section ids, provenance, token estimates, and
    same-plan prerequisite step ids.
    """
    state = normalize_stage_state(stage_state)
    ordered = order_specs(specs, kind=kind)
    ids_by_label: dict[str, str] = {}
    for label, skill_id in ordered:
        ids_by_label.setdefault(label, f"{_slug(label)}:{skill_id}")
    steps: list[PlannedStep] = []
    for label, skill_id in ordered:
        skill = skills_by_name.get(skill_id)
        content = ""
        if content_for is not None:
            content = content_for(skill_id) or ""
        elif skill is not None:
            content = getattr(skill, "content", "") or ""
        metadata = getattr(skill, "skill_metadata", None)
        stage = PIPELINE_STAGES.get(label, "")
        section_ids: tuple[str, ...] = ()
        if metadata is not None:
            section_ids = tuple(
                section.id for section in metadata.sections if not stage or section.stage == stage
            )
        provenance = "user" if label == "requested workflow" else "policy"
        if state is not None and stage and state.stage == stage:
            provenance = state.source
        prerequisites = tuple(
            ids_by_label[required]
            for required in _STEP_PREREQUISITES.get(label, ())
            if required in ids_by_label
        )
        steps.append(
            PlannedStep(
                step_id=f"{_slug(label)}:{skill_id}",
                label=label,
                stage=stage,
                skill_id=skill_id,
                section_ids=section_ids,
                provenance=provenance,
                estimate_tokens=estimate_tokens(content),
                prerequisites=prerequisites,
            )
        )
    return tuple(steps)


@dataclass(frozen=True)
class SectionLocator:
    """Stable continuation locator for one section at one content revision."""

    skill_id: str
    content_revision: str = ""
    section_id: str = ""
    heading: str = ""
    stage: str = ""

    def describe(self) -> str:
        label = self.heading or self.section_id or self.skill_id
        stage = f", {self.stage}" if self.stage else ""
        return f"{self.skill_id} \u00b7 {label} ({self.section_id or 'body'}{stage})"

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "content_revision": self.content_revision,
            "section_id": self.section_id,
            "heading": self.heading,
            "stage": self.stage,
        }


@dataclass(frozen=True)
class ChosenSection:
    """One disclosed section, whole, with exact revision identities.

    ``estimate_tokens`` is the measured cost of the *disclosed block* —
    ``"\\n### {skill_id} · {heading}\\n{text}\\n"`` — under the plan's token
    counter, so ``included_tokens`` matches the serialized prompt, not just
    the body text.
    """

    step_id: str
    skill_id: str
    skill_revision: str
    content_revision: str
    section_id: str
    heading: str
    stage: str
    text: str
    estimate_tokens: int
    required_by: tuple[str, ...] = ()

    def locator(self) -> SectionLocator:
        return SectionLocator(
            skill_id=self.skill_id,
            content_revision=self.content_revision,
            section_id=self.section_id,
            heading=self.heading,
            stage=self.stage,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "skill_id": self.skill_id,
            "skill_revision": self.skill_revision,
            "content_revision": self.content_revision,
            "section_id": self.section_id,
            "heading": self.heading,
            "stage": self.stage,
            "text": self.text,
            "estimate_tokens": self.estimate_tokens,
            "required_by": list(self.required_by),
        }


@dataclass(frozen=True)
class OmittedSection:
    """A section that was not disclosed, with its reason and locator."""

    locator: SectionLocator
    reason: str
    required_by: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "locator": self.locator.to_dict(),
            "reason": self.reason,
            "required_by": list(self.required_by),
        }


@dataclass(frozen=True)
class UnresolvedPrerequisite:
    """A required section that could not be loaded coherently."""

    skill_id: str
    section_id: str
    required_by: str
    reason: str
    locator: SectionLocator | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "section_id": self.section_id,
            "required_by": self.required_by,
            "reason": self.reason,
            "locator": self.locator.to_dict() if self.locator else None,
        }


@dataclass(frozen=True)
class WholeBodyUnit:
    """A skill without declared sections: legacy whole-body excerpt fallback."""

    step_id: str
    skill_id: str
    skill_revision: str
    content_revision: str
    content: str
    file_path: str = ""
    estimate_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "skill_id": self.skill_id,
            "skill_revision": self.skill_revision,
            "content_revision": self.content_revision,
            "content_size": len(self.content),
            "file_path": self.file_path,
            "estimate_tokens": self.estimate_tokens,
        }


@dataclass(frozen=True)
class DisclosurePlan:
    """The §10.3 instruction disclosure contract for one plan + stage fact."""

    task_revision: int
    stage_revision: int
    catalog_revision: str
    stage_source: str
    current_stage: str
    chosen: tuple[ChosenSection, ...] = ()
    whole_body: tuple[WholeBodyUnit, ...] = ()
    required_prerequisites: tuple[SectionLocator, ...] = ()
    omitted: tuple[OmittedSection, ...] = ()
    unresolved_prerequisites: tuple[UnresolvedPrerequisite, ...] = ()
    missing_context: tuple[str, ...] = ()
    task_constraints: tuple[str, ...] = ()
    included_tokens: int = 0
    token_basis: str = TOKEN_BASIS
    diagnostics: tuple[MetadataDiagnostic, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_revision": self.task_revision,
            "stage_revision": self.stage_revision,
            "catalog_revision": self.catalog_revision,
            "stage_source": self.stage_source,
            "current_stage": self.current_stage,
            "chosen": [item.to_dict() for item in self.chosen],
            "whole_body": [item.to_dict() for item in self.whole_body],
            "required_prerequisites": [item.to_dict() for item in self.required_prerequisites],
            "omitted": [item.to_dict() for item in self.omitted],
            "unresolved_prerequisites": [item.to_dict() for item in self.unresolved_prerequisites],
            "missing_context": list(self.missing_context),
            "task_constraints": list(self.task_constraints),
            "included_tokens": self.included_tokens,
            "token_basis": self.token_basis,
            "diagnostics": [item.to_dict() for item in self.diagnostics],
        }


def plan_disclosure(
    steps: Sequence[PlannedStep],
    *,
    skill_for: Callable[[str], Any | None],
    content_for: Callable[[str], str | None],
    budget: int = 1200,
    stage_state: StageState | Mapping[str, Any] | None = None,
    task_revision: int = 0,
    catalog_revision: str = "",
    constraints: Sequence[str] = (),
    token_count: Callable[[str], int] = estimate_tokens,
    token_basis: str = TOKEN_BASIS,
    whole_body_limit: int = MAX_WHOLE_BODY_UNITS,
) -> DisclosurePlan:
    """Derive the disclosure selection from the authoritative planned steps.

    Section-bearing skills disclose only the current-stage sections, their
    transitive prerequisites (whole, prerequisites first), and untagged
    general sections. Without an honest stage fact only bootstrap material is
    disclosed. Skills without declared sections remain on the legacy
    whole-body excerpt fallback, preserving pre-A3 output byte-for-byte.
    """
    state = normalize_stage_state(stage_state)
    current_stage = state.stage if state is not None else ""
    stage_source = state.source if state is not None else ""
    stage_revision = state.revision if state is not None else 0
    remaining = max(0, int(budget))

    chosen: list[ChosenSection] = []
    chosen_keys: set[tuple[str, str]] = set()
    required_prerequisites: list[SectionLocator] = []
    omitted: list[OmittedSection] = []
    unresolved: list[UnresolvedPrerequisite] = []
    missing_context: list[str] = []
    diagnostics: list[MetadataDiagnostic] = []
    whole_body: list[WholeBodyUnit] = []
    fetched: dict[str, tuple[Any, str | None]] = {}

    def locator(
        skill_id: str,
        revision: str,
        section_id: str,
        heading: str = "",
        stage: str = "",
    ) -> SectionLocator:
        return SectionLocator(
            skill_id=skill_id,
            content_revision=revision,
            section_id=section_id,
            heading=heading,
            stage=stage,
        )

    def declared_locator(skill_id: str, section: SectionMetadata, revision: str) -> SectionLocator:
        return locator(
            skill_id,
            revision,
            section.id,
            heading=section.heading,
            stage=section.stage or "",
        )

    def missing_key(skill_id: str, section: SectionMetadata, revision: str) -> str:
        return f"{skill_id}#{section.id}@{revision}"

    def block_cost(skill_id: str, heading: str, text: str) -> int:
        """Exact cost of the disclosed prompt block (heading included)."""
        return token_count(f"\n### {skill_id} \u00b7 {heading}\n{text}\n")

    for step in steps:
        skill = skill_for(step.skill_id)
        content = content_for(step.skill_id)
        fetched[step.skill_id] = (skill, content)
        if skill is None or not content:
            omitted.append(
                OmittedSection(locator=locator(step.skill_id, "", ""), reason="unavailable")
            )
            continue
        metadata: SkillMetadata | None = getattr(skill, "skill_metadata", None)
        if metadata is None or not metadata.sections:
            continue
        try:
            graph, graph_diags = build_section_graph(metadata)
        except SectionCycleError as exc:
            diagnostics.extend(exc.diagnostics)
            index = index_sections(content)
            for section in metadata.sections:
                unresolved.append(
                    UnresolvedPrerequisite(
                        skill_id=step.skill_id,
                        section_id=section.id,
                        required_by=section.id,
                        reason="cycle",
                        locator=declared_locator(step.skill_id, section, index.revision),
                    )
                )
                omitted.append(
                    OmittedSection(
                        locator=declared_locator(step.skill_id, section, index.revision),
                        reason="cycle",
                    )
                )
            continue
        diagnostics.extend(graph_diags)
        index = index_sections(content)
        bound, bind_diags = bound_sections(metadata, content)
        diagnostics.extend(bind_diags)

        def try_load(target: SectionMetadata, *, mandatory: bool) -> None:
            """Load target + prerequisites whole, or record explicit inability."""
            nonlocal remaining
            chain = [*graph.closure(target.id), target.id]
            pending = [
                section_id for section_id in chain if (step.skill_id, section_id) not in chosen_keys
            ]
            if not pending:
                return
            units: list[tuple[SectionMetadata, Any, str, int]] = []
            for section_id in pending:
                declared = bound.get_section(section_id)
                if declared is None:
                    continue
                if declared.anchor is None or index.find_by_line(declared.anchor.line) is None:
                    unresolved.append(
                        UnresolvedPrerequisite(
                            skill_id=step.skill_id,
                            section_id=declared.id,
                            required_by=target.id,
                            reason="missing-anchor",
                            locator=declared_locator(step.skill_id, declared, index.revision),
                        )
                    )
                    omitted.append(
                        OmittedSection(
                            locator=declared_locator(step.skill_id, target, index.revision),
                            reason="prerequisite-unresolved",
                        )
                    )
                    if mandatory:
                        missing_context.append(missing_key(step.skill_id, target, index.revision))
                    return
                parsed = index.find_by_line(declared.anchor.line)
                units.append(
                    (
                        declared,
                        parsed,
                        parsed.text,
                        block_cost(step.skill_id, declared.heading or parsed.heading, parsed.text),
                    )
                )
            cost = sum(unit[3] for unit in units)
            if cost > remaining:
                for declared, parsed, _text, _estimate in units:
                    unresolved.append(
                        UnresolvedPrerequisite(
                            skill_id=step.skill_id,
                            section_id=declared.id,
                            required_by=target.id,
                            reason="budget",
                            locator=declared_locator(step.skill_id, declared, index.revision),
                        )
                    )
                omitted.append(
                    OmittedSection(
                        locator=declared_locator(step.skill_id, target, index.revision),
                        reason="budget",
                        required_by=(target.id,),
                    )
                )
                if mandatory:
                    missing_context.append(missing_key(step.skill_id, target, index.revision))
                return
            for declared, parsed, text, estimate in units:
                is_prerequisite = declared.id != target.id
                entry = ChosenSection(
                    step_id=step.step_id,
                    skill_id=step.skill_id,
                    skill_revision=skill_content_revision(skill),
                    content_revision=index.revision,
                    section_id=declared.id,
                    heading=declared.heading or parsed.heading,
                    stage=declared.stage or "",
                    text=text,
                    estimate_tokens=estimate,
                    required_by=(target.id,) if is_prerequisite else (),
                )
                chosen.append(entry)
                chosen_keys.add((step.skill_id, declared.id))
                remaining -= estimate
                if is_prerequisite:
                    required_prerequisites.append(entry.locator())

        if current_stage:
            targets = [section for section in bound.sections if section.stage == current_stage]
        else:
            targets = [section for section in bound.sections if section.stage is None]
        if not current_stage and index.preamble.strip():
            preamble_text = index.preamble.rstrip()
            estimate = block_cost(step.skill_id, "Preamble", preamble_text)
            if estimate <= remaining and (step.skill_id, "preamble") not in chosen_keys:
                chosen.append(
                    ChosenSection(
                        step_id=step.step_id,
                        skill_id=step.skill_id,
                        skill_revision=skill_content_revision(skill),
                        content_revision=index.revision,
                        section_id="preamble",
                        heading="Preamble",
                        stage="",
                        text=preamble_text,
                        estimate_tokens=estimate,
                    )
                )
                chosen_keys.add((step.skill_id, "preamble"))
                remaining -= estimate
        for target in targets:
            mandatory = bool(current_stage) and target.stage == current_stage
            try_load(target, mandatory=mandatory)
        if current_stage:
            # Untagged sections are phase-neutral general context; keep them
            # after the current-stage units, budget permitting.
            for section in bound.sections:
                if section.stage is not None:
                    continue
                try_load(section, mandatory=False)
        for section in bound.sections:
            if section.stage is None:
                continue
            if current_stage and section.stage == current_stage:
                # Already recorded by the mandatory load attempt above.
                continue
            if (step.skill_id, section.id) in chosen_keys:
                continue
            omitted.append(
                OmittedSection(
                    locator=declared_locator(step.skill_id, section, index.revision),
                    reason="later-stage" if current_stage else "stage-unknown",
                )
            )

    whole_count = 0
    for step in steps:
        if whole_count >= whole_body_limit:
            break
        skill, content = fetched.get(step.skill_id, (None, None))
        if skill is None or not content:
            continue
        metadata = getattr(skill, "skill_metadata", None)
        if metadata is not None and metadata.sections:
            continue
        whole_body.append(
            WholeBodyUnit(
                step_id=step.step_id,
                skill_id=step.skill_id,
                skill_revision=skill_content_revision(skill),
                content_revision=content_fingerprint(content),
                content=content,
                file_path=str(getattr(skill, "file_path", "") or ""),
                estimate_tokens=estimate_tokens(content),
            )
        )
        whole_count += 1

    if len(diagnostics) > MAX_PLAN_DIAGNOSTICS:
        dropped = len(diagnostics) - MAX_PLAN_DIAGNOSTICS + 1
        diagnostics = diagnostics[: MAX_PLAN_DIAGNOSTICS - 1] + [
            MetadataDiagnostic(
                code=CODE_DIAGNOSTICS_TRUNCATED,
                message=(
                    f"{dropped} further diagnostic(s) dropped; "
                    f"the plan report is bounded at {MAX_PLAN_DIAGNOSTICS}"
                ),
            )
        ]
    included_tokens = sum(item.estimate_tokens for item in chosen)
    try:
        task_revision_value = max(0, int(task_revision))
    except (TypeError, ValueError):
        task_revision_value = 0
    return DisclosurePlan(
        task_revision=task_revision_value,
        stage_revision=stage_revision,
        catalog_revision=catalog_revision,
        stage_source=stage_source,
        current_stage=current_stage,
        chosen=tuple(chosen),
        whole_body=tuple(whole_body),
        required_prerequisites=tuple(required_prerequisites),
        omitted=tuple(omitted[:MAX_OMITTED]),
        unresolved_prerequisites=tuple(unresolved),
        missing_context=tuple(dict.fromkeys(missing_context)),
        task_constraints=normalize_constraints(list(constraints)),
        included_tokens=included_tokens,
        token_basis=token_basis,
        diagnostics=tuple(diagnostics),
    )
