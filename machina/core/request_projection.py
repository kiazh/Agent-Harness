"""Shared budgeted request projector — shadow mode (spec §14, §17-B1).

One deterministic algorithm both provider-call loops will share (B2 wires it;
B1 ships the projector only). It plans the *model-visible* view of a request:
pinned content, round-grouped history, optional retrieved material, and
verified artifact views — never deletion of durable history (§14.1).

Safety invariants
-----------------

- **Pins are never truncated** (§14.3, C03/C12). Text pins are exact strings;
  provider-required blocks and ``protected`` units (native/signed provider
  blocks) are never dropped or replaced (C05). If pinned content alone cannot
  fit, the result is a structured refusal — never a lossy send.
- **Store-before-publish** (C06). An older bulky observation is replaced by a
  recoverable view *only* when the injected artifact lookup reports
  ``available`` and ``complete`` for its handle. Otherwise the original is
  kept, or the round is omitted explicitly — never rewritten into a lossy view
  that claims recovery it cannot back (§14.6).
- **Round atomicity** (§14.4, C04). Each assistant tool-call batch is grouped
  with all of its result messages and selected as one unit: no torn batches,
  ids/order/arguments preserved.

Injected seams (the module is a leaf: stdlib imports only, no provider, DB,
config, or agent imports)
----------------------------------------------------------------------------------------------

- ``count_tokens(messages)`` — provider-aware accounting (B2 passes the same
  callable the capacity guard uses); the projector measures the *planned*
  canonical message list through it.
- ``artifacts(handle)`` — returns ``{available, complete, handle, revision,
  summary_view}`` or nothing; errors degrade to "unavailable".
- ``ProjectionInput`` is frozen and never mutated (copy-on-write; C16
  pass-through re-renders the canonical shape).

Recent-set policy (§14.4)
-------------------------

``min_recent_groups`` (>= 1) is a floor, not a target: the newest M coherent
history groups plus every pinned/protected/required unit must survive. The
**token budget wins** above that floor: older groups are first given verified
views and then, if still over, omitted oldest-first. Floor protection is
"present", not "untruncated": when the floor itself is over budget, its
eligible tool results are reduced to verified views (C02 deterministic
reduction); if no verified view can shrink the floor, the projector refuses
(C02/C05/C12). The minimum viable protected group is the newest single
coherent group (assistant batch + its results); with no groups, the pins are
the floor.

Deterministic fit passes (§14.5, bounded)
-----------------------------------------

1. Measure the full original plan — if it fits, return it verbatim (C16).
2. Measure the pins + protected/required + newest-M floor (originals).
3. If the floor is over, measure the floor with eligible verified views.
4. Measure the floor plus every optional unit (views where eligible).
5. Find the largest fitting optional prefix by binary search over the
   deterministic priority order: history groups newest-first, then retrieved
   material by ascending explicit ``priority`` (ties by input order).
6. Return the best fitting plan or a structured refusal diagnosed against
   pins / protected blocks / required content / floor.

Every pass is one injected ``count_tokens`` call; identical candidates are
measured once. ``MAX_FIT_PASSES = 24`` hard-caps a call (worst case is
``ceil(log2(n)) + 8`` measures for ``n`` optional units; a defensive
``pass_limit_exceeded`` refusal replaces any overrun). No randomness, no
wall-clock, stable unit ids throughout; ``input_revision`` hashes the
canonical input snapshot for B2's shadow comparison.

View rendering
--------------

A verified view is ``summary_view`` plus a deterministic locator tail that
starts with ``[machina view: reduced by request_projection``. That tail also
matches ``prompt._REDUCED_VIEW_TAIL_RE``, so a later stale-history
condensation pass leaves the curated view intact (B2 seam note).
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

__all__ = [
    "MAX_FIT_PASSES",
    "MIN_VIEWABLE_CHARS",
    "OmittedUnit",
    "ProjectedUnit",
    "ProjectionInput",
    "ProjectionMessage",
    "ProjectionPins",
    "ProjectionRefusal",
    "ProjectionResult",
    "RetainedPin",
    "UnitDecision",
    "project_request",
    "provider_messages_to_units",
    "shadow_report",
]

# Hard cap on count_tokens invocations per project_request call (§14.5 step 9:
# stop after a bounded number of passes; no retry storm, C15). Worst case is
# ceil(log2(n)) + 8 for n optional units; the cap is a defensive backstop.
MAX_FIT_PASSES = 24

# Only bulky text observations are eligible for verified-view replacement;
# tiny results are cheaper to keep than to externalize.
MIN_VIEWABLE_CHARS = 512

_ROLES = frozenset({"system", "user", "assistant", "tool"})
_KINDS = frozenset({"history", "required", "retrieved"})
_PIN_KINDS = (
    "objective",
    "constraints",
    "read_only_scope",
    "core_instructions",
    "blockers",
    "task_state",
)
# Pins that frame a request and therefore render ahead of the current turn's
# instruction (§14.3). Every other pin kind (read-only scope, blockers, task
# state) is a derived runtime annotation: it renders *after* the instruction
# so projection can never demote or drop the turn's instruction text.
_GOVERNING_PIN_KINDS = frozenset({"objective", "constraints", "core_instructions"})
_RETRIEVED_REASON = "retrieved_over_budget"
_OLDER_GROUP_REASON = "older_group_over_budget"
_REFUSAL_REASON = "capacity_refusal"
_SAFE_OPTIONS = (
    "Choose a route or model with a larger context window and retry the turn.",
    "Start a new session or narrower task so older rounds can be released.",
    "Reduce pinned/required content explicitly (operator decision, never automatic truncation).",
    "Retry once bulky results are stored as complete artifacts (B0 store-before-publish).",
)
_REFUSAL_MESSAGES = {
    "pins_exceed_capacity": "Pinned content alone exceeds the effective request budget.",
    "protected_exceed_capacity": "Provider-required/protected blocks exceed the effective budget.",
    "required_exceed_capacity": "Required current-stage content exceeds the effective budget.",
    "floor_exceeded_capacity": (
        "The minimum viable plan (pins plus the latest coherent group) is too large."
    ),
    "capacity_exceeded": "No fitting plan was found within the bounded projection passes.",
    "pass_limit_exceeded": "Projection stopped at the bounded pass limit without a confirmed fit.",
}


@dataclass(frozen=True)
class RetainedPin:
    """One exact pinned text that survived into the planned request."""

    kind: str
    text: str


@dataclass(frozen=True)
class ProjectionPins:
    """Exact pinned content and provider-required block ids (§14.3).

    Text fields are never truncated or edited; ``provider_required_blocks``
    lists message unit ids that must survive (they act as ``protected``).
    """

    objective: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    read_only_scope: tuple[str, ...] = ()
    core_instructions: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    task_state: tuple[str, ...] = ()
    provider_required_blocks: tuple[str, ...] = ()

    def items(self) -> tuple[RetainedPin, ...]:
        """Pinned texts in the fixed kind order the planner renders them."""
        items: list[RetainedPin] = []
        for kind in _PIN_KINDS:
            for text in getattr(self, kind):
                items.append(RetainedPin(kind=kind, text=str(text)))
        return tuple(items)


@dataclass(frozen=True)
class ProjectionMessage:
    """One canonical, provider-ready message with a stable unit id.

    ``content`` is usually a string; non-text content (for example image
    parts) is opaque to the projector: it is carried and counted verbatim and
    is never view-replaced. ``protected`` units (native/signed provider
    blocks) are never dropped or replaced (C05). ``kind`` is ``history``
    (default; participates in round grouping), ``required`` (current-stage
    content; always survives or the projection refuses) or ``retrieved``
    (optional material ordered by ascending ``priority``, ties by input
    order). Tool calls are treated as opaque but exact: arguments, ids and
    ordering are preserved byte-for-byte.
    """

    unit_id: str
    role: str
    content: Any = ""
    tool_calls: tuple[Mapping[str, Any], ...] = ()
    tool_call_id: str | None = None
    protected: bool = False
    kind: str = "history"
    artifact_handle: str | None = None
    priority: int = 0
    native_blocks: Any = None
    native_key: str | None = None
    # The original canonical provider mapping captured (deep-copied) at unit
    # construction (MCH-004). ``None`` for hand-built units, which keeps the
    # field-subset reconstruction shape. Unmodified units re-render ``raw``
    # verbatim on the outgoing request so extra provider fields (``name``,
    # caller metadata, ...) never vanish; reconstructed units (view
    # replacement) merge the extra original fields back.
    raw: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class ProjectionInput:
    """Everything the projector needs; every external seam is injected."""

    messages: tuple[ProjectionMessage, ...] = ()
    pins: ProjectionPins = ProjectionPins()
    budget_tokens: int = 0
    count_tokens: Callable[[Sequence[Mapping[str, Any]]], int] | None = None
    artifacts: Callable[[str], Mapping[str, Any] | None] | None = None
    safety_headroom_tokens: int = 0
    min_recent_groups: int = 1
    count_basis: str = "injected count_tokens(planned_messages)"


@dataclass(frozen=True)
class ProjectedUnit:
    """A unit re-rendered as a verified recoverable view (store-before-publish)."""

    unit_id: str
    original_chars: int
    view: str
    artifact_handle: str
    artifact_revision: str | None
    completeness: str
    method: str = "artifact_view"


@dataclass(frozen=True)
class OmittedUnit:
    """A unit left out of the plan, with the deterministic reason."""

    unit_id: str
    reason: str


@dataclass(frozen=True)
class UnitDecision:
    """Per-unit outcome for shadow comparison and B2 logging."""

    unit_id: str
    action: str  # "retained" | "projected" | "omitted"
    reason: str


@dataclass(frozen=True)
class ProjectionRefusal:
    """Structured capacity failure: no request may be sent for this plan."""

    code: str
    message: str
    required_tokens: int
    budget_tokens: int
    safe_options: tuple[str, ...]


@dataclass(frozen=True)
class ProjectionResult:
    """The planned projection (or a structured refusal)."""

    retained: tuple[str, ...]
    projected: tuple[ProjectedUnit, ...]
    omitted: tuple[OmittedUnit, ...]
    retained_pins: tuple[RetainedPin, ...]
    planned_messages: tuple[Mapping[str, Any], ...]
    pinned_survived: bool
    estimated_tokens: int
    basis: str
    passes: int
    refusal: ProjectionRefusal | None
    input_revision: str
    decisions: tuple[UnitDecision, ...] = ()


# --------------------------------------------------------------------------- #
# input validation and revision identity
# --------------------------------------------------------------------------- #


def _validate(inp: ProjectionInput) -> None:
    if not callable(inp.count_tokens):
        raise ValueError("ProjectionInput.count_tokens must be a callable")
    if isinstance(inp.budget_tokens, bool) or not isinstance(inp.budget_tokens, int):
        raise ValueError("budget_tokens must be an int")
    if inp.budget_tokens < 0:
        raise ValueError("budget_tokens must be >= 0")
    headroom = inp.safety_headroom_tokens
    if isinstance(headroom, bool) or not isinstance(headroom, int) or headroom < 0:
        raise ValueError("safety_headroom_tokens must be an int >= 0")
    if isinstance(inp.min_recent_groups, bool) or not isinstance(inp.min_recent_groups, int):
        raise ValueError("min_recent_groups must be an int >= 1")
    if inp.min_recent_groups < 1:
        raise ValueError("min_recent_groups must be >= 1 (the latest group is always protected)")
    if inp.artifacts is not None and not callable(inp.artifacts):
        raise ValueError("artifacts must be a callable or None")

    seen: set[str] = set()
    for unit in inp.messages:
        unit_id = unit.unit_id
        if not isinstance(unit_id, str) or not unit_id:
            raise ValueError("every unit_id must be a non-empty string")
        if unit_id in seen:
            raise ValueError(f"duplicate unit_id: {unit_id}")
        seen.add(unit_id)
        if unit.role not in _ROLES:
            raise ValueError(f"unit {unit_id}: role must be one of {sorted(_ROLES)}")
        if unit.kind not in _KINDS:
            raise ValueError(f"unit {unit_id}: kind must be one of {sorted(_KINDS)}")
        if unit.kind != "history" and (unit.tool_calls or unit.tool_call_id is not None):
            raise ValueError(f"unit {unit_id}: kind={unit.kind} units are standalone content")
        if unit.role == "tool":
            if unit.kind != "history":
                raise ValueError(f"unit {unit_id}: tool results are history observations")
            if not isinstance(unit.tool_call_id, str) or not unit.tool_call_id:
                raise ValueError(f"unit {unit_id}: tool results require a tool_call_id")
        if unit.role == "assistant" and unit.tool_calls and unit.kind != "history":
            raise ValueError(f"unit {unit_id}: tool-call batches are history rounds")
        if unit.native_blocks is not None:
            if not isinstance(unit.native_key, str) or not unit.native_key:
                raise ValueError(f"unit {unit_id}: native_blocks require a native_key")
        if unit.artifact_handle is not None and (
            not isinstance(unit.artifact_handle, str) or not unit.artifact_handle
        ):
            raise ValueError(f"unit {unit_id}: artifact_handle must be a non-empty string")
        if isinstance(unit.priority, bool) or not isinstance(unit.priority, int):
            raise ValueError(f"unit {unit_id}: priority must be an int")
        call_ids = []
        for tool_call in unit.tool_calls:
            if not isinstance(tool_call, Mapping):
                raise ValueError(f"unit {unit_id}: tool calls must be mappings")
            call_id = tool_call.get("id")
            if not isinstance(call_id, str) or not call_id:
                raise ValueError(f"unit {unit_id}: every tool call needs a string id")
            call_ids.append(call_id)
        if len(call_ids) != len(set(call_ids)):
            raise ValueError(f"unit {unit_id}: duplicate tool-call ids in one batch")
        if unit.raw is not None and not isinstance(unit.raw, Mapping):
            raise ValueError(f"unit {unit_id}: raw must be a mapping or None")

    for block_id in inp.pins.provider_required_blocks:
        if not isinstance(block_id, str) or block_id not in seen:
            raise ValueError(f"provider_required_blocks references unknown unit: {block_id}")


def _canonical_content(content: Any) -> Any:
    return content if isinstance(content, str) else repr(content)


def _input_revision(inp: ProjectionInput) -> str:
    """Deterministic content revision of the canonical snapshot.

    Opaque ``native_blocks`` are excluded: they are protected and never
    modified, so they cannot change the projection decisions.
    """
    payload = {
        "pins": {kind: [str(text) for text in getattr(inp.pins, kind)] for kind in _PIN_KINDS},
        "provider_required_blocks": list(inp.pins.provider_required_blocks),
        "messages": [
            {
                "unit_id": unit.unit_id,
                "role": unit.role,
                "content": _canonical_content(unit.content),
                "tool_calls": [
                    {str(key): _canonical_content(value) for key, value in dict(tool_call).items()}
                    for tool_call in unit.tool_calls
                ],
                "tool_call_id": unit.tool_call_id,
                "protected": bool(unit.protected),
                "kind": unit.kind,
                "artifact_handle": unit.artifact_handle,
                "priority": int(unit.priority),
            }
            for unit in inp.messages
        ],
        "budget_tokens": int(inp.budget_tokens),
        "safety_headroom_tokens": int(inp.safety_headroom_tokens),
        "min_recent_groups": int(inp.min_recent_groups),
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=repr)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# bounded measurement
# --------------------------------------------------------------------------- #


class _PassLimitExceeded(Exception):
    """Internal: the bounded pass count was reached (C15 backstop)."""


class _Counter:
    """Bounded wrapper over the injected provider-aware token counter."""

    def __init__(self, count_tokens: Callable[..., int], limit: int) -> None:
        self._count_tokens = count_tokens
        self.limit = limit
        self.passes = 0
        self.last_tokens = 0

    def __call__(self, messages: Sequence[Mapping[str, Any]]) -> int:
        if self.passes >= self.limit:
            raise _PassLimitExceeded(f"bounded pass limit {self.limit} reached")
        self.passes += 1
        value = self._count_tokens(tuple(messages))
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("count_tokens must return an int")
        self.last_tokens = int(value)
        return int(value)


# --------------------------------------------------------------------------- #
# round grouping (§14.4)
# --------------------------------------------------------------------------- #


def _group_history(messages: Sequence[ProjectionMessage]) -> list[list[ProjectionMessage]]:
    """Group history messages: assistant tool-call batches with their results.

    Deterministic and structural: each assistant message carrying tool calls
    starts a round; every immediately following contiguous history tool
    message joins it, and each result must reference one of that batch's call
    ids (ordering and pairing stay exactly as produced by the agent loop,
    C04). All other history messages are singleton groups. Missing results are
    never fabricated and never inferred as success.
    """
    groups: list[list[ProjectionMessage]] = []
    index = 0
    while index < len(messages):
        unit = messages[index]
        if unit.kind != "history":
            index += 1
            continue
        if unit.role == "assistant" and unit.tool_calls:
            call_ids = {tc.get("id") for tc in unit.tool_calls}
            group = [unit]
            cursor = index + 1
            while (
                cursor < len(messages)
                and messages[cursor].kind == "history"
                and messages[cursor].role == "tool"
            ):
                result = messages[cursor]
                if result.tool_call_id not in call_ids:
                    raise ValueError(
                        f"tool result {result.unit_id} does not match any call id of {unit.unit_id}"
                    )
                group.append(result)
                cursor += 1
            groups.append(group)
            index = cursor
        else:
            groups.append([unit])
            index += 1
    return groups


@dataclass(frozen=True)
class _Element:
    """One selectable optional item: a whole round group or one retrieved unit."""

    key: str
    unit_ids: tuple[str, ...]
    kind: str  # "group" | "retrieved"


# --------------------------------------------------------------------------- #
# rendering and verified views
# --------------------------------------------------------------------------- #


class _Artifacts:
    """Memoized, fail-safe view resolver over the injected artifact lookup."""

    def __init__(self, lookup: Callable[[str], Mapping[str, Any] | None] | None) -> None:
        self._lookup = lookup
        self._by_handle: dict[str, tuple[str, str | None] | None] = {}

    def view(self, handle: str) -> tuple[str, str | None] | None:
        """``(summary_view, revision)`` only for available+complete handles."""
        if handle in self._by_handle:
            return self._by_handle[handle]
        found: tuple[str, str | None] | None = None
        if self._lookup is not None:
            raw: Mapping[str, Any] | None = None
            try:
                raw = self._lookup(handle)
            except Exception:
                raw = None  # lookup failure degrades to unavailable (C06)
            if (
                isinstance(raw, Mapping)
                and raw.get("available") is True
                and raw.get("complete") is True
                and raw.get("handle") == handle
            ):
                summary = raw.get("summary_view")
                if isinstance(summary, str) and summary:
                    revision = raw.get("revision")
                    found = (summary, revision if isinstance(revision, str) else None)
        self._by_handle[handle] = found
        return found


def _view_text(summary: str, original_chars: int, handle: str, revision: str | None) -> str:
    revision_part = f", revision {revision}" if revision else ""
    return (
        f"{summary}\n[machina view: reduced by request_projection; original "
        f"{original_chars} chars retained as artifact {handle} "
        f"(complete{revision_part}); artifact_read pages the exact original]"
    )


def _view_eligible(unit: ProjectionMessage) -> bool:
    """Verified-view replacement is only for bulky unprotected tool results."""
    if unit.protected or unit.kind != "history" or unit.role != "tool":
        return False
    if not isinstance(unit.artifact_handle, str) or not unit.artifact_handle:
        return False
    if not isinstance(unit.content, str) or len(unit.content) < MIN_VIEWABLE_CHARS:
        return False
    return "\n[machina view:" not in unit.content


#: Fields projection explicitly renders from the unit when it must
#: reconstruct (MCH-004); every other key of ``unit.raw`` is merged back so
#: extra provider fields never vanish, while content-bearing fields owned by
#: projection (``content`` and the view/role) are never resurrected.
_FIXED_UNIT_FIELDS = frozenset({"role", "content", "tool_calls", "tool_call_id"})


def _render_unit(
    unit: ProjectionMessage,
    native_cache: Mapping[str, Any],
    *,
    text: Any = None,
) -> dict[str, Any]:
    raw = unit.raw
    if raw is not None:
        if text is None and not unit.tool_calls and unit.native_blocks is None:
            # Unmodified unit (MCH-004): the captured original mapping is the
            # canonical payload — replay it verbatim instead of reconstructing
            # a fixed-field subset. The copy keeps the plan isolated from the
            # caller's storage (C16: never alias inputs).
            return copy.deepcopy(dict(raw))
        base = _reconstructed_unit(unit, native_cache, text=text)
        # View replacement or a native/tool rewrite: the owned fields were
        # rebuilt; merge any further original fields so nothing silently
        # vanishes. Owned fields win (setdefault), extra fields follow.
        for key, value in raw.items():
            if key not in _FIXED_UNIT_FIELDS:
                base.setdefault(key, copy.deepcopy(value))
        return base
    return _reconstructed_unit(unit, native_cache, text=text)


def _reconstructed_unit(
    unit: ProjectionMessage,
    native_cache: Mapping[str, Any],
    *,
    text: Any = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "role": unit.role,
        "content": unit.content if text is None else text,
    }
    if unit.tool_calls:
        message["tool_calls"] = [copy.deepcopy(dict(tool_call)) for tool_call in unit.tool_calls]
    if unit.tool_call_id is not None:
        message["tool_call_id"] = unit.tool_call_id
    if unit.native_blocks is not None and unit.native_key:
        message[unit.native_key] = native_cache[unit.unit_id]
    return message


def _compose(
    messages: Sequence[ProjectionMessage],
    pin_items: Sequence[RetainedPin],
    included: Collection[str],
    viewed: Mapping[str, str],
    native_cache: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Render the plan: governing pins, the instruction, derived pins, units.

    Governing pins (objective, constraints, core instructions) frame the
    request and render first. The current turn's instruction is the first
    included ``required`` unit (the assembled user prompt); it renders ahead of
    the derived scope/blockers/task-state pins so projection can never demote
    or drop instruction text (§14.3). When no required unit is in the plan,
    derived pins keep their canonical place directly after the governing pins
    (pin-only and floor measurements are unchanged).
    """
    governing = [
        {"role": "system", "content": pin.text}
        for pin in pin_items
        if pin.kind in _GOVERNING_PIN_KINDS
    ]
    derived = [
        {"role": "system", "content": pin.text}
        for pin in pin_items
        if pin.kind not in _GOVERNING_PIN_KINDS
    ]
    instruction_id = next(
        (unit.unit_id for unit in messages if unit.kind == "required" and unit.unit_id in included),
        None,
    )
    plan = list(governing)
    if instruction_id is None:
        plan.extend(derived)
        for unit in messages:
            if unit.unit_id in included:
                plan.append(_render_unit(unit, native_cache, text=viewed.get(unit.unit_id)))
        return plan
    for unit in messages:
        if unit.unit_id not in included:
            continue
        plan.append(_render_unit(unit, native_cache, text=viewed.get(unit.unit_id)))
        if unit.unit_id == instruction_id:
            plan.extend(derived)
    return plan


# --------------------------------------------------------------------------- #
# the fit algorithm (§14.5)
# --------------------------------------------------------------------------- #


def project_request(inp: ProjectionInput) -> ProjectionResult:
    """Plan the request projection. Pure: inputs are never mutated (C16)."""
    _validate(inp)
    counter = _Counter(inp.count_tokens, MAX_FIT_PASSES)
    revision = _input_revision(inp)
    budget = max(0, int(inp.budget_tokens) - int(inp.safety_headroom_tokens))

    messages = tuple(inp.messages)
    native_cache = {
        unit.unit_id: copy.deepcopy(unit.native_blocks)
        for unit in messages
        if unit.native_blocks is not None
    }
    pin_items = inp.pins.items()

    pinned_ids = {unit.unit_id for unit in messages if unit.protected}
    pinned_ids |= set(inp.pins.provider_required_blocks)
    required_ids = {unit.unit_id for unit in messages if unit.kind == "required"}
    floor_extra = pinned_ids | required_ids

    artifacts = _Artifacts(inp.artifacts)
    view_texts: dict[str, tuple[str, str | None]] = {}
    for unit in messages:
        if _view_eligible(unit):
            found = artifacts.view(str(unit.artifact_handle))
            if found is not None:
                text = _view_text(
                    found[0], len(str(unit.content)), str(unit.artifact_handle), found[1]
                )
                if len(text) < len(str(unit.content)):
                    view_texts[unit.unit_id] = (text, found[1])

    history_groups = _group_history(messages)
    newest_group_ids = {unit.unit_id for unit in history_groups[-1]} if history_groups else set()
    floor_groups: list[list[ProjectionMessage]] = []
    optional_groups: list[list[ProjectionMessage]] = []
    for group in reversed(history_groups):
        if len(floor_groups) < inp.min_recent_groups or any(
            unit.unit_id in floor_extra for unit in group
        ):
            floor_groups.append(group)
        else:
            optional_groups.append(group)

    floor_ids = {unit.unit_id for group in floor_groups for unit in group}
    floor_ids |= required_ids
    floor_ids |= pinned_ids

    positions = {unit.unit_id: index for index, unit in enumerate(messages)}
    retrieved = sorted(
        (unit for unit in messages if unit.kind == "retrieved" and unit.unit_id not in floor_ids),
        key=lambda unit: (int(unit.priority), positions[unit.unit_id]),
    )
    elements: list[_Element] = [
        _Element(
            key=f"group:{group[0].unit_id}",
            unit_ids=tuple(unit.unit_id for unit in group),
            kind="group",
        )
        for group in optional_groups
    ]
    elements.extend(
        _Element(key=f"retrieved:{unit.unit_id}", unit_ids=(unit.unit_id,), kind="retrieved")
        for unit in retrieved
    )

    def plan_for(family: str, prefix: int) -> dict[str, Any]:
        included = set(floor_ids)
        for element in elements[:prefix]:
            included.update(element.unit_ids)
        viewed: dict[str, str] = {}
        for unit_id in included:
            if unit_id not in view_texts:
                continue
            if family == "reduced_floor":
                # The floor itself is over budget: reduce every eligible unit.
                viewed[unit_id] = view_texts[unit_id][0]
            elif family == "reduced" and unit_id not in floor_ids:
                # The floor fits as-is; optional older material uses views.
                viewed[unit_id] = view_texts[unit_id][0]
            # family "original": nothing is replaced (C16 verbatim pass-through).
        return {
            "included": included,
            "viewed": viewed,
            "revisions": {unit_id: view_texts[unit_id][1] for unit_id in viewed},
            "plan": _compose(messages, pin_items, included, viewed, native_cache),
        }

    cache: dict[tuple[str, int], tuple[bool, int, dict[str, Any]]] = {}

    def evaluate(family: str, prefix: int) -> tuple[bool, int, dict[str, Any]]:
        key = (family, prefix)
        if key not in cache:
            candidate = plan_for(family, prefix)
            tokens = counter(candidate["plan"])
            cache[key] = (tokens <= budget, tokens, candidate)
        return cache[key]

    total = len(elements)
    try:
        fits, tokens, candidate = evaluate("original", total)  # §14.5 step 1
        if fits:  # C16: no projection needed; the plan is the canonical input
            return _success(
                inp,
                counter,
                revision,
                messages,
                pin_items,
                pinned_ids,
                required_ids,
                newest_group_ids,
                floor_ids,
                candidate,
                tokens,
            )

        floor_fits, floor_tokens, floor_candidate = evaluate("original", 0)
        floor_measured = floor_tokens
        if not floor_fits:
            reduced_fits, reduced_tokens, reduced_candidate = evaluate("reduced_floor", 0)
            floor_measured = min(floor_measured, reduced_tokens)
            if not reduced_fits:
                return _refusal(
                    inp,
                    counter,
                    revision,
                    messages,
                    pin_items,
                    native_cache,
                    budget,
                    floor_measured,
                    pinned_ids,
                    required_ids,
                )
            base_family, base_candidate, base_tokens = (
                "reduced_floor",
                reduced_candidate,
                reduced_tokens,
            )
        else:
            # The floor fits as-is: optional units may still use verified views.
            base_family, base_candidate, base_tokens = "reduced", floor_candidate, floor_tokens
            cache[(base_family, 0)] = (floor_fits, floor_tokens, floor_candidate)

        if total == 0:
            return _success(
                inp,
                counter,
                revision,
                messages,
                pin_items,
                pinned_ids,
                required_ids,
                newest_group_ids,
                floor_ids,
                base_candidate,
                base_tokens,
            )

        full_fits, full_tokens, full_candidate = evaluate(base_family, total)
        if full_fits:
            return _success(
                inp,
                counter,
                revision,
                messages,
                pin_items,
                pinned_ids,
                required_ids,
                newest_group_ids,
                floor_ids,
                full_candidate,
                full_tokens,
            )

        # §14.5 step 8: largest fitting prefix in deterministic priority order.
        low, high = 0, total  # predicate(0) True (base), predicate(total) False
        while high - low > 1:
            middle = (low + high) // 2
            middle_fits, _, _ = evaluate(base_family, middle)
            if middle_fits:
                low = middle
            else:
                high = middle
        final_fits, final_tokens, final_candidate = evaluate(base_family, low)
        if not final_fits:  # defensive: a non-monotone counter cannot force a lossy send
            return _refusal(
                inp,
                counter,
                revision,
                messages,
                pin_items,
                native_cache,
                budget,
                final_tokens,
                pinned_ids,
                required_ids,
            )
        return _success(
            inp,
            counter,
            revision,
            messages,
            pin_items,
            pinned_ids,
            required_ids,
            newest_group_ids,
            floor_ids,
            final_candidate,
            final_tokens,
        )
    except _PassLimitExceeded:
        return _refusal(
            inp,
            counter,
            revision,
            messages,
            pin_items,
            native_cache,
            budget,
            counter.last_tokens,
            pinned_ids,
            required_ids,
        )


def _retained_reason(
    unit: ProjectionMessage,
    pinned_ids: Collection[str],
    required_ids: Collection[str],
    newest_group_ids: Collection[str],
    floor_ids: Collection[str],
) -> str:
    if unit.unit_id in pinned_ids:
        return "protected"
    if unit.unit_id in required_ids:
        return "required"
    if unit.unit_id in newest_group_ids:
        return "latest_group"
    if unit.unit_id in floor_ids:
        return "recent_group"
    return "optional_fit"


def _success(
    inp: ProjectionInput,
    counter: _Counter,
    revision: str,
    messages: Sequence[ProjectionMessage],
    pin_items: Sequence[RetainedPin],
    pinned_ids: Collection[str],
    required_ids: Collection[str],
    newest_group_ids: Collection[str],
    floor_ids: Collection[str],
    candidate: Mapping[str, Any],
    tokens: int,
) -> ProjectionResult:
    included = set(candidate["included"])
    viewed: Mapping[str, str] = candidate["viewed"]
    revisions: Mapping[str, str | None] = candidate.get("revisions", {})

    retained: list[str] = []
    projected: list[ProjectedUnit] = []
    omitted: list[OmittedUnit] = []
    decisions: list[UnitDecision] = []
    for unit in messages:
        if unit.unit_id in viewed:
            projected.append(
                ProjectedUnit(
                    unit_id=unit.unit_id,
                    original_chars=len(str(unit.content)),
                    view=viewed[unit.unit_id],
                    artifact_handle=str(unit.artifact_handle),
                    artifact_revision=revisions.get(unit.unit_id),
                    completeness="complete",
                )
            )
            decisions.append(UnitDecision(unit.unit_id, "projected", "artifact_view"))
        elif unit.unit_id in included:
            retained.append(unit.unit_id)
            decisions.append(
                UnitDecision(
                    unit.unit_id,
                    "retained",
                    _retained_reason(unit, pinned_ids, required_ids, newest_group_ids, floor_ids),
                )
            )
        else:
            omission_reason = _RETRIEVED_REASON if unit.kind == "retrieved" else _OLDER_GROUP_REASON
            omitted.append(OmittedUnit(unit.unit_id, omission_reason))
            decisions.append(UnitDecision(unit.unit_id, "omitted", omission_reason))

    pinned_survived = all(unit_id in included for unit_id in {*pinned_ids, *required_ids})
    return ProjectionResult(
        retained=tuple(retained),
        projected=tuple(projected),
        omitted=tuple(omitted),
        retained_pins=tuple(pin_items),
        planned_messages=tuple(candidate["plan"]),
        pinned_survived=bool(pinned_survived),
        estimated_tokens=int(tokens),
        basis=inp.count_basis,
        passes=counter.passes,
        refusal=None,
        input_revision=revision,
        decisions=tuple(decisions),
    )


def _refusal(
    inp: ProjectionInput,
    counter: _Counter,
    revision: str,
    messages: Sequence[ProjectionMessage],
    pin_items: Sequence[RetainedPin],
    native_cache: Mapping[str, Any],
    budget: int,
    floor_measured: int,
    pinned_ids: Collection[str],
    required_ids: Collection[str],
) -> ProjectionResult:
    """Structured capacity failure (§14.3, §14.5 step 10, C02/C05/C12)."""
    code = "floor_exceeded_capacity"
    required = int(floor_measured)
    try:
        code, required = _diagnose(
            counter,
            messages,
            pin_items,
            native_cache,
            budget,
            floor_measured,
            pinned_ids,
            required_ids,
        )
    except _PassLimitExceeded:
        code = "pass_limit_exceeded"
        required = int(counter.last_tokens)
    omitted = tuple(OmittedUnit(unit.unit_id, _REFUSAL_REASON) for unit in messages)
    decisions = tuple(UnitDecision(unit.unit_id, "omitted", _REFUSAL_REASON) for unit in messages)
    return ProjectionResult(
        retained=(),
        projected=(),
        omitted=omitted,
        retained_pins=(),
        planned_messages=(),
        pinned_survived=False,
        estimated_tokens=required,
        basis=inp.count_basis,
        passes=counter.passes,
        refusal=ProjectionRefusal(
            code=code,
            message=_REFUSAL_MESSAGES.get(code, _REFUSAL_MESSAGES["capacity_exceeded"]),
            required_tokens=required,
            budget_tokens=int(budget),
            safe_options=_SAFE_OPTIONS,
        ),
        input_revision=revision,
        decisions=decisions,
    )


def _diagnose(
    counter: _Counter,
    messages: Sequence[ProjectionMessage],
    pin_items: Sequence[RetainedPin],
    native_cache: Mapping[str, Any],
    budget: int,
    floor_measured: int,
    pinned_ids: Collection[str],
    required_ids: Collection[str],
) -> tuple[str, int]:
    """Distinguish pins / protected / required / floor violations (bounded).

    Only the smallest plans that can pinpoint the over-budget layer are
    measured: pins alone; pins plus protected blocks; pins plus protected plus
    required content. The floor case reports the smallest floor measured by
    the caller (the request was still over before this diagnosis ran).
    """
    cache: dict[frozenset[str], int] = {}

    def measure(ids: Collection[str]) -> int:
        key = frozenset(ids)
        if key not in cache:
            cache[key] = counter(_compose(messages, pin_items, key, {}, native_cache))
        return cache[key]

    pins_only = measure(())
    if pins_only > budget:
        return "pins_exceed_capacity", pins_only
    if pinned_ids:
        with_protected = measure(pinned_ids)
        if with_protected > budget:
            return "protected_exceed_capacity", with_protected
    if required_ids:
        with_required = measure(set(pinned_ids) | set(required_ids))
        if with_required > budget:
            return "required_exceed_capacity", with_required
    return "floor_exceeded_capacity", int(floor_measured)


# --------------------------------------------------------------------------- #
# shadow surface (B2 comparison seam)
# --------------------------------------------------------------------------- #


def shadow_report(
    inp: ProjectionInput,
    *,
    baseline: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Plain-data record of what the projector *would* do (never applied).

    ``baseline`` is the caller's current outgoing message list (for example
    after the legacy condensation pass); the report carries its content hash
    so B2 can compare the legacy request against the planned projection later
    without changing the live loop.
    """
    result = project_request(inp)
    report: dict[str, Any] = {
        "shadow": True,
        "input_revision": result.input_revision,
        "budget_tokens": int(inp.budget_tokens),
        "safety_headroom_tokens": int(inp.safety_headroom_tokens),
        "effective_budget_tokens": max(0, int(inp.budget_tokens) - int(inp.safety_headroom_tokens)),
        "basis": result.basis,
        "would_fit": result.refusal is None,
        "pinned_survived": result.pinned_survived,
        "estimated_tokens": result.estimated_tokens,
        "passes": result.passes,
        "retained": list(result.retained),
        "projected": [
            {
                "unit_id": unit.unit_id,
                "artifact_handle": unit.artifact_handle,
                "artifact_revision": unit.artifact_revision,
                "completeness": unit.completeness,
                "view_chars": len(unit.view),
            }
            for unit in result.projected
        ],
        "omitted": [{"unit_id": unit.unit_id, "reason": unit.reason} for unit in result.omitted],
        "decisions": [
            {"unit_id": decision.unit_id, "action": decision.action, "reason": decision.reason}
            for decision in result.decisions
        ],
        "retained_pins": [{"kind": pin.kind, "text": pin.text} for pin in result.retained_pins],
        "planned_messages": len(result.planned_messages),
        "planned_sha256": _sha256_messages(result.planned_messages),
        "refusal": None,
    }
    if result.refusal is not None:
        report["refusal"] = {
            "code": result.refusal.code,
            "message": result.refusal.message,
            "required_tokens": result.refusal.required_tokens,
            "budget_tokens": result.refusal.budget_tokens,
            "safe_options": list(result.refusal.safe_options),
        }
    if baseline is not None:
        baseline_list = list(baseline)
        report["baseline"] = {
            "messages": len(baseline_list),
            "sha256": _sha256_messages(baseline_list),
        }
    return report


def _sha256_messages(messages: Sequence[Mapping[str, Any]]) -> str:
    blob = json.dumps(list(messages), sort_keys=True, ensure_ascii=False, default=repr)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def provider_messages_to_units(
    messages: Sequence[Mapping[str, Any]],
    *,
    protected_indexes: Collection[int] = (),
    artifact_handles: Mapping[int, str] | None = None,
    kinds: Mapping[int, str] | None = None,
    priorities: Mapping[int, int] | None = None,
    native_key: str | None = None,
) -> tuple[ProjectionMessage, ...]:
    """Faithful B2 adapter from canonical provider messages to units.

    Unit ids are ``m{index}`` over the absolute message position, so
    append-only history keeps stable identities between candidate and final
    projections. Callers annotate protection, artifact handles, kinds and
    retrieval priorities by index. Each unit also captures its original
    mapping (deep-copied) as ``unit.raw``, so a fitting plan replays
    unmodified messages verbatim and nothing outside the modeled fields is
    erased (MCH-004).
    """
    protected = set(protected_indexes)
    handles = artifact_handles or {}
    kind_map = kinds or {}
    priority_map = priorities or {}
    units: list[ProjectionMessage] = []
    for index, message in enumerate(messages):
        call_payload = message.get("tool_calls") or ()
        has_native = bool(native_key) and message.get(native_key) is not None
        units.append(
            ProjectionMessage(
                unit_id=f"m{index}",
                role=str(message.get("role") or ""),
                content=message.get("content", ""),
                tool_calls=tuple(copy.deepcopy(dict(tool_call)) for tool_call in call_payload),
                tool_call_id=message.get("tool_call_id"),
                protected=index in protected,
                kind=str(kind_map.get(index, "history")),
                artifact_handle=handles.get(index),
                priority=int(priority_map.get(index, 0)),
                native_blocks=copy.deepcopy(message.get(native_key)) if has_native else None,
                native_key=str(native_key) if has_native else None,
                # Capture the whole canonical mapping verbatim (deep-copied at
                # capture time) so rendering can replay it unchanged (MCH-004).
                raw=copy.deepcopy(dict(message)),
            )
        )
    return tuple(units)
