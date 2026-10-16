"""Observed activation events: typed, bounded, redacted telemetry (spec §13).

Seven closed event names describe what was OBSERVED:

* ``candidate_found`` — a selection pass saw this skill as a candidate;
* ``selected_for_plan`` — a candidate entered a plan, carrying structured
  selection reasons (reuse the A2b explanations: named ranking components and
  applicability reasons — everything surfaced is derived from the same values
  that drove the decision, never hidden chain-of-thought);
* ``instruction_section_disclosed`` — instruction text was served (ACTIVITY);
* ``supporting_resource_disclosed`` — a supporting file/bundle resource was
  served;
* ``stage_requested_or_entered`` — an explicit stage fact was requested or
  entered (source: user/model/observed);
* ``associated_tool_outcome_observed`` — a tool outcome was observed next to a
  skill step (the observation is recorded; the outcome string is data);
* ``verification_evidence_recorded`` — verification evidence with a task/step
  id and a source revision link.

Semantics, pinned by tests (``tests/test_skills_events.py``):

* **Disclosure is ACTIVITY, never success.** Every record is an observation.
  :meth:`ObservedEventLog.counts` counts retained observations per event name;
  no counter is ever labeled success, and a disclosure, stage entry, or tool
  observation proves only that the event happened — never that the task
  worked or that the skill caused anything.
* **``not_observed`` semantics.** When the honest support for an event is
  missing, the attempt records ``observed=False`` (``status ==
  NOT_OBSERVED``) with a bounded reason instead of being dropped or upgraded:
  :meth:`ObservedEventLog.record_verification_evidence` requires a task/step
  id AND a source revision link, otherwise it records ``not_observed``.
  Absence of an event is likewise never evidence of failure.
* **Bounded.** A ring buffer keeps at most ``MAX_OBSERVED_EVENTS``
  observations, oldest dropped first. No watchers, no daemon threads, no
  file/DB writes — the durable channel stays the legacy telemetry counters.
* **Redacted.** Text fields are truncated (``MAX_TEXT_CHARS`` /
  ``MAX_IDENTIFIER_CHARS``) and whitespace/control characters are collapsed.
  Full instruction text, prompts, source code, and file contents are never
  accepted: there is deliberately no content parameter and no content field on
  :class:`ObservedEvent`. Callers pass bounded identifiers and structured
  reasons only.

Migration (legacy counters stay authoritative; ``machina/skills/learning.py``
is untouched): the durable channel remains ``SkillRegistry.record_use`` /
``record_view`` (``usage_count`` / ``view_count`` / ``last_activity_at`` in
``telemetry.json``). This module is the ephemeral observation API *alongside*
them — record a typed observation when one occurs, and keep calling
``record_use`` when instructions actually enter the model input. Gateway
browse/preview (administrative reads) belongs to ``record_view``: a human
preview is not an activation and must not record
``instruction_section_disclosed``.

This module is stdlib-only and imports nothing from ``machina``.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

__all__ = [
    "EVENT_CANDIDATE_FOUND",
    "EVENT_INSTRUCTION_SECTION_DISCLOSED",
    "EVENT_NAMES",
    "EVENT_SELECTED_FOR_PLAN",
    "EVENT_STAGE_REQUESTED_OR_ENTERED",
    "EVENT_SUPPORTING_RESOURCE_DISCLOSED",
    "EVENT_TOOL_OUTCOME_OBSERVED",
    "EVENT_VERIFICATION_EVIDENCE_RECORDED",
    "MAX_IDENTIFIER_CHARS",
    "MAX_OBSERVED_EVENTS",
    "MAX_REASONS",
    "MAX_TEXT_CHARS",
    "NOT_OBSERVED",
    "OBSERVED",
    "ObservedEvent",
    "ObservedEventLog",
    "clear",
    "counts",
    "record",
    "record_candidate_found",
    "record_instruction_section_disclosed",
    "record_not_observed",
    "record_selected_for_plan",
    "record_stage_requested_or_entered",
    "record_supporting_resource_disclosed",
    "record_tool_outcome_observed",
    "record_verification_evidence",
    "snapshot",
]

#: Ring-buffer bound: one process never grows past this many observations.
MAX_OBSERVED_EVENTS = 256
#: Bound for free-text fields (detail, reasons, outcome).
MAX_TEXT_CHARS = 160
#: Bound for identifier fields (skill/section/step/task ids, revisions, ...).
MAX_IDENTIFIER_CHARS = 80
#: Bound for the number of structured reasons kept per event.
MAX_REASONS = 4

OBSERVED = "observed"
NOT_OBSERVED = "not_observed"

EVENT_CANDIDATE_FOUND = "candidate_found"
EVENT_SELECTED_FOR_PLAN = "selected_for_plan"
EVENT_INSTRUCTION_SECTION_DISCLOSED = "instruction_section_disclosed"
EVENT_SUPPORTING_RESOURCE_DISCLOSED = "supporting_resource_disclosed"
EVENT_STAGE_REQUESTED_OR_ENTERED = "stage_requested_or_entered"
EVENT_TOOL_OUTCOME_OBSERVED = "associated_tool_outcome_observed"
EVENT_VERIFICATION_EVIDENCE_RECORDED = "verification_evidence_recorded"

#: The closed §13 vocabulary, in declared order. Extend only with tests.
EVENT_NAMES: tuple[str, ...] = (
    EVENT_CANDIDATE_FOUND,
    EVENT_SELECTED_FOR_PLAN,
    EVENT_INSTRUCTION_SECTION_DISCLOSED,
    EVENT_SUPPORTING_RESOURCE_DISCLOSED,
    EVENT_STAGE_REQUESTED_OR_ENTERED,
    EVENT_TOOL_OUTCOME_OBSERVED,
    EVENT_VERIFICATION_EVIDENCE_RECORDED,
)


def _text(value: Any, *, limit: int) -> str:
    """Collapse whitespace/control characters, then hard-truncate."""
    if not isinstance(value, str) or not value:
        return ""
    return " ".join(value.split())[:limit]


def _identifier(value: Any) -> str:
    return _text(value, limit=MAX_IDENTIFIER_CHARS)


def _reasons(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        value = (value,)
    if not isinstance(value, Iterable):
        return ()
    cleaned = [item for entry in value if (item := _text(entry, limit=MAX_TEXT_CHARS))]
    return tuple(cleaned[:MAX_REASONS])


@dataclass(frozen=True)
class ObservedEvent:
    """One bounded, redacted observation. Frozen; never carries content."""

    seq: int
    at: str
    name: str
    observed: bool = True
    skill_id: str = ""
    section_id: str = ""
    step_id: str = ""
    task_id: str = ""
    stage: str = ""
    source: str = ""
    content_revision: str = ""
    source_revision: str = ""
    resource_id: str = ""
    tool_id: str = ""
    outcome: str = ""
    reasons: tuple[str, ...] = ()
    detail: str = ""

    @property
    def status(self) -> str:
        return OBSERVED if self.observed else NOT_OBSERVED

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "at": self.at,
            "name": self.name,
            "status": self.status,
            "observed": self.observed,
            "skill_id": self.skill_id,
            "section_id": self.section_id,
            "step_id": self.step_id,
            "task_id": self.task_id,
            "stage": self.stage,
            "source": self.source,
            "content_revision": self.content_revision,
            "source_revision": self.source_revision,
            "resource_id": self.resource_id,
            "tool_id": self.tool_id,
            "outcome": self.outcome,
            "reasons": list(self.reasons),
            "detail": self.detail,
        }


class ObservedEventLog:
    """Bounded, thread-safe ring buffer of observed activation events."""

    def __init__(self, *, max_entries: int = MAX_OBSERVED_EVENTS) -> None:
        self._max_entries = max(1, int(max_entries))
        self._events: deque[ObservedEvent] = deque(maxlen=self._max_entries)
        self._lock = threading.Lock()
        self._seq = 0

    @property
    def max_entries(self) -> int:
        return self._max_entries

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)

    def record(
        self,
        name: str,
        *,
        observed: bool = True,
        skill_id: Any = "",
        section_id: Any = "",
        step_id: Any = "",
        task_id: Any = "",
        stage: Any = "",
        source: Any = "",
        content_revision: Any = "",
        source_revision: Any = "",
        resource_id: Any = "",
        tool_id: Any = "",
        outcome: Any = "",
        reasons: Any = (),
        detail: Any = "",
    ) -> ObservedEvent:
        """Record one observation under a closed-vocabulary event *name*."""
        if name not in EVENT_NAMES:
            expected = ", ".join(EVENT_NAMES)
            raise ValueError(f"unknown observed event name {name!r}; expected one of {expected}")
        with self._lock:
            self._seq += 1
            event = ObservedEvent(
                seq=self._seq,
                at=datetime.now(UTC).isoformat(),
                name=name,
                observed=bool(observed),
                skill_id=_identifier(skill_id),
                section_id=_identifier(section_id),
                step_id=_identifier(step_id),
                task_id=_identifier(task_id),
                stage=_identifier(stage),
                source=_identifier(source),
                content_revision=_identifier(content_revision),
                source_revision=_identifier(source_revision),
                resource_id=_identifier(resource_id),
                tool_id=_identifier(tool_id),
                outcome=_text(outcome, limit=MAX_TEXT_CHARS),
                reasons=_reasons(reasons),
                detail=_text(detail, limit=MAX_TEXT_CHARS),
            )
            self._events.append(event)
        return event

    def record_not_observed(self, name: str, *, reason: Any, **fields: Any) -> ObservedEvent:
        """Record an explicit ``not_observed`` attempt with a bounded reason.

        The attempt is kept (never silently dropped) and can never be upgraded
        to an observation: ``observed=False`` and the ``not_observed:`` marker
        survives even when the caller supplied its own reasons.
        """
        marker = _text(f"not_observed: {reason}", limit=MAX_TEXT_CHARS) or NOT_OBSERVED
        prefix = _reasons(fields.pop("reasons", ()))
        return self.record(
            name,
            observed=False,
            reasons=(marker, *prefix[: MAX_REASONS - 1]),
            **fields,
        )

    # ─── typed convenience recorders ───────────────────────────────────────

    def record_candidate_found(
        self, skill_id: Any = "", *, reasons: Any = (), **fields: Any
    ) -> ObservedEvent:
        return self.record(EVENT_CANDIDATE_FOUND, skill_id=skill_id, reasons=reasons, **fields)

    def record_selected_for_plan(
        self, skill_id: Any = "", *, step_id: Any = "", reasons: Any = (), **fields: Any
    ) -> ObservedEvent:
        return self.record(
            EVENT_SELECTED_FOR_PLAN, skill_id=skill_id, step_id=step_id, reasons=reasons, **fields
        )

    def record_instruction_section_disclosed(
        self,
        skill_id: Any = "",
        section_id: Any = "",
        *,
        step_id: Any = "",
        stage: Any = "",
        content_revision: Any = "",
        **fields: Any,
    ) -> ObservedEvent:
        return self.record(
            EVENT_INSTRUCTION_SECTION_DISCLOSED,
            skill_id=skill_id,
            section_id=section_id,
            step_id=step_id,
            stage=stage,
            content_revision=content_revision,
            **fields,
        )

    def record_supporting_resource_disclosed(
        self,
        skill_id: Any = "",
        resource_id: Any = "",
        *,
        content_revision: Any = "",
        **fields: Any,
    ) -> ObservedEvent:
        return self.record(
            EVENT_SUPPORTING_RESOURCE_DISCLOSED,
            skill_id=skill_id,
            resource_id=resource_id,
            content_revision=content_revision,
            **fields,
        )

    def record_stage_requested_or_entered(
        self, stage: Any, *, source: Any = "user", **fields: Any
    ) -> ObservedEvent:
        return self.record(EVENT_STAGE_REQUESTED_OR_ENTERED, stage=stage, source=source, **fields)

    def record_tool_outcome_observed(
        self, *, outcome: Any, skill_id: Any = "", tool_id: Any = "", **fields: Any
    ) -> ObservedEvent:
        return self.record(
            EVENT_TOOL_OUTCOME_OBSERVED,
            outcome=outcome,
            skill_id=skill_id,
            tool_id=tool_id,
            **fields,
        )

    def record_verification_evidence(
        self,
        *,
        task_id: Any = "",
        step_id: Any = "",
        source_revision: Any = "",
        **fields: Any,
    ) -> ObservedEvent:
        """Verification evidence needs a task/step id AND a revision link.

        Without both, the attempt records ``not_observed`` — never silently
        dropped and never upgraded to an observation.
        """
        if not (task_id or step_id):
            return self.record_not_observed(
                EVENT_VERIFICATION_EVIDENCE_RECORDED,
                reason="a task id or step id is required",
                **fields,
            )
        if not source_revision:
            return self.record_not_observed(
                EVENT_VERIFICATION_EVIDENCE_RECORDED,
                reason="a source revision link is required",
                task_id=task_id,
                step_id=step_id,
                **fields,
            )
        return self.record(
            EVENT_VERIFICATION_EVIDENCE_RECORDED,
            task_id=task_id,
            step_id=step_id,
            source_revision=source_revision,
            **fields,
        )

    # ─── reads ─────────────────────────────────────────────────────────────

    def snapshot(self) -> list[ObservedEvent]:
        """Retained observations, oldest first (list copy; events immutable)."""
        with self._lock:
            return list(self._events)

    def counts(self) -> dict[str, int]:
        """Retained observations per event name (activity counts, no success)."""
        result = dict.fromkeys(EVENT_NAMES, 0)
        with self._lock:
            for event in self._events:
                result[event.name] += 1
        return result

    def clear(self) -> None:
        with self._lock:
            self._events.clear()


#: Process-level log used by the module-level API (bounded, ephemeral).
_DEFAULT_LOG = ObservedEventLog()


def record(name: str, **fields: Any) -> ObservedEvent:
    return _DEFAULT_LOG.record(name, **fields)


def record_not_observed(name: str, *, reason: Any, **fields: Any) -> ObservedEvent:
    return _DEFAULT_LOG.record_not_observed(name, reason=reason, **fields)


def record_candidate_found(
    skill_id: Any = "", *, reasons: Any = (), **fields: Any
) -> ObservedEvent:
    return _DEFAULT_LOG.record_candidate_found(skill_id, reasons=reasons, **fields)


def record_selected_for_plan(
    skill_id: Any = "", *, step_id: Any = "", reasons: Any = (), **fields: Any
) -> ObservedEvent:
    return _DEFAULT_LOG.record_selected_for_plan(
        skill_id, step_id=step_id, reasons=reasons, **fields
    )


def record_instruction_section_disclosed(
    skill_id: Any = "",
    section_id: Any = "",
    *,
    step_id: Any = "",
    stage: Any = "",
    content_revision: Any = "",
    **fields: Any,
) -> ObservedEvent:
    return _DEFAULT_LOG.record_instruction_section_disclosed(
        skill_id,
        section_id,
        step_id=step_id,
        stage=stage,
        content_revision=content_revision,
        **fields,
    )


def record_supporting_resource_disclosed(
    skill_id: Any = "",
    resource_id: Any = "",
    *,
    content_revision: Any = "",
    **fields: Any,
) -> ObservedEvent:
    return _DEFAULT_LOG.record_supporting_resource_disclosed(
        skill_id, resource_id, content_revision=content_revision, **fields
    )


def record_stage_requested_or_entered(
    stage: Any, *, source: Any = "user", **fields: Any
) -> ObservedEvent:
    return _DEFAULT_LOG.record_stage_requested_or_entered(stage, source=source, **fields)


def record_tool_outcome_observed(
    *, outcome: Any, skill_id: Any = "", tool_id: Any = "", **fields: Any
) -> ObservedEvent:
    return _DEFAULT_LOG.record_tool_outcome_observed(
        outcome=outcome, skill_id=skill_id, tool_id=tool_id, **fields
    )


def record_verification_evidence(
    *, task_id: Any = "", step_id: Any = "", source_revision: Any = "", **fields: Any
) -> ObservedEvent:
    return _DEFAULT_LOG.record_verification_evidence(
        task_id=task_id, step_id=step_id, source_revision=source_revision, **fields
    )


def snapshot() -> list[ObservedEvent]:
    return _DEFAULT_LOG.snapshot()


def counts() -> dict[str, int]:
    return _DEFAULT_LOG.counts()


def clear() -> None:
    _DEFAULT_LOG.clear()
