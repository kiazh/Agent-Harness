"""Shared per-request projection/capacity gate for both ReAct loops (B2, §14).

One function — :func:`evaluate_request` — is called by ``run()`` and
``run_stream()`` after in-place stale-history condensation and before every
provider request, so the two loops cannot drift (C10):

- **Default (flag off): shadow.** The request is projected and a bounded,
  redacted report is logged under the stable logger name
  ``agent.request_projection.shadow`` (counts, refusal code, estimate, basis,
  hashes — never message bodies). Outgoing messages are byte-identical to the
  pre-B2 behavior; the legacy hard capacity guard is unchanged.
- **``request_projection_enabled`` (default False): wired.** A safe plan is
  sent — pins first, then the canonical unit order — and a structured refusal
  turns into the same capacity-failure shape the legacy guard emits
  (audit event + ``outcome="failed"`` + the refusal message and safe options)
  with no provider call. The legacy hard guard is still applied to the exact
  outgoing payload as the final enforcement.

Both modes also emit exactly one bounded ``request_projection_decision`` audit
event per evaluation (§14.7): mode, would_fit/refusal, token estimates, budget
and pass count — labels and counts only, never message bodies. A wiring
failure that degrades to the legacy path emits the same event marked
``degraded`` instead of silently disappearing.

The gate is pure with respect to the agent: ``messages`` is never mutated
(copy-on-write in the projector), no effects are replayed, and the only I/O is
read-only verified-view prefetching through ``artifact_store.lookup`` before
the pure projector runs (store-before-publish stays in the store contract;
C06/C08). Any unexpected wiring/projection failure degrades to the legacy
request plus the hard guard — it never fabricates a lossy send.

Pins (never invented, §14.3): objective from the session goal; constraints and
read-only scope from the shared deterministic task descriptor over the durable
task state plus the current request; active task class/families only when the
session actually carries task-state keys; blockers from the durable
stagnation-stop record, the only failed-attempt marker the runtime persists.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from machina.core.artifacts import artifact_store
from machina.core.config import config
from machina.core.models import AgentResponse
from machina.core.prompt import (
    PROVIDER_CONTENT_KEY,
    _effective_turn_budget,
    _request_context_tokens,
)
from machina.core.provider import audit_log
from machina.core.request_projection import (
    ProjectionInput,
    ProjectionPins,
    ProjectionRefusal,
    ProjectionResult,
    project_request,
    provider_messages_to_units,
    shadow_report,
)
from machina.core.task_descriptor import build_task_descriptor
from machina.observability.diagnostics import record_failure

__all__ = ["RequestGate", "evaluate_request"]

logger = logging.getLogger(__name__)
# Stable shadow logger (bounded + redacted: counts, codes, estimates, hashes).
_SHADOW_LOGGER = logging.getLogger("agent.request_projection.shadow")

# Every recovery disclosure tail (artifact_disclosure, reducer _disclosure, and
# the projector's own verified view) contains this phrase. The handle token is
# bounded; the store re-validates the UUID and answers unavailable otherwise.
_HANDLE_RE = re.compile(r"retained as artifact ([^\s;)\]\"']{1,128})")

# Exact legacy capacity-failure text (byte-compatible with the pre-B2 guard).
_CAPACITY_MESSAGE = (
    "Current request exceeds the model context window. "
    "Compact the context or select a model with a larger window."
)
_COUNT_BASIS = "prompt.request_context_tokens(planned request, live tool_defs)"


@dataclass
class RequestGate:
    """Outcome of one per-request projection/capacity decision.

    ``messages`` is the exact payload the loop must send: the caller's list
    object unchanged (shadow/off and degraded paths) or the projector's
    planned messages (wired path). A set ``failure`` means no provider call
    may be made; the loop returns (or yields) it verbatim.
    """

    messages: list[dict[str, Any]]
    effective_budget: int
    request_tokens: int
    failure: AgentResponse | None = None
    refusal: ProjectionRefusal | None = None
    projected: bool = False


def _wired_enabled() -> bool:
    try:
        return bool(config.get("request_projection_enabled"))
    except Exception:
        return False


def _clean_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _state_constraints(state: Mapping[str, Any]) -> tuple[str, ...]:
    raw = state.get("constraints")
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return ()
    return tuple(str(item) for item in raw if isinstance(item, str) and item.strip())


def _artifact_handles(messages: Sequence[Mapping[str, Any]]) -> dict[int, str]:
    """Map tool-message index -> retained-artifact handle from its disclosure.

    Handles are recovered from the stable disclosure tail the append seam
    writes (``retained as artifact <handle>``); the last occurrence is the
    result's own tail, so a condensed head that merely quotes another handle
    cannot win. Parsing, not state: the canonical message list is never
    annotated (byte-identity when the flag is off).
    """
    found: dict[int, str] = {}
    for index, message in enumerate(messages):
        if message.get("role") != "tool":
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        matches = _HANDLE_RE.findall(content)
        if matches:
            found[index] = matches[-1]
    return found


def _collect_pins(session: Any, user_message: str) -> ProjectionPins:
    """Pins from state the code actually keeps (never invented; §14.3)."""
    goal = _clean_text(getattr(session, "goal", None))
    raw_state = getattr(session, "state", None)
    state: Mapping[str, Any] = raw_state if isinstance(raw_state, Mapping) else {}

    descriptor = None
    try:
        descriptor = build_task_descriptor(
            str(user_message or ""), task_state=state, goal=goal or None
        )
    except Exception as error:  # deterministic helper: never break the turn
        record_failure("request_gate.pins", error)

    constraints = (
        tuple(descriptor.constraints) if descriptor is not None else _state_constraints(state)
    )
    read_only = (
        ("operation_scope=read-only",)
        if descriptor is not None and descriptor.operation_scope == "read-only"
        else ()
    )

    task_state: tuple[str, ...] = ()
    if descriptor is not None and (
        state.get("active_class") or state.get("active_families") or state.get("stack")
    ):
        lines = [f"task_class={descriptor.task_class}"]
        if descriptor.task_families:
            lines.append("task_families=" + ",".join(descriptor.task_families))
        task_state = tuple(lines)

    blockers: tuple[str, ...] = ()
    stop = state.get("stagnation_stop")
    if isinstance(stop, Mapping):
        stop_text = _clean_text(stop.get("stop_text"))
        if stop_text:
            blockers = (stop_text,)

    return ProjectionPins(
        objective=(goal,) if goal else (),
        constraints=constraints,
        read_only_scope=read_only,
        blockers=blockers,
        task_state=task_state,
    )


async def _build_input(
    messages: Sequence[Mapping[str, Any]],
    *,
    session: Any,
    provider: Any,
    tool_defs: Any,
    session_id: Any,
    agent_id: str,
    user_message: str,
) -> ProjectionInput:
    """Assemble the projector input; async only for the verified-view prefetch."""
    handles = _artifact_handles(messages)
    prefetched: dict[str, Mapping[str, Any] | None] = {}
    for handle in sorted(set(handles.values())):
        try:
            prefetched[handle] = await artifact_store.lookup(
                handle, session_id=session_id, agent_id=agent_id
            )
        except Exception as error:  # one bad handle degrades that view only
            record_failure("request_gate.lookup", error)
            prefetched[handle] = None

    protected = {
        index
        for index, message in enumerate(messages)
        if message.get(PROVIDER_CONTENT_KEY) is not None
    }
    kinds: dict[int, str] = {}
    if messages:
        first = messages[0]
        if first.get("role") in ("user", "system") and not first.get("tool_calls"):
            kinds[0] = "required"

    units = provider_messages_to_units(
        messages,
        protected_indexes=protected,
        artifact_handles=handles,
        kinds=kinds,
        native_key=PROVIDER_CONTENT_KEY,
    )
    return ProjectionInput(
        messages=units,
        pins=_collect_pins(session, user_message),
        budget_tokens=_effective_turn_budget(session, provider),
        count_tokens=lambda planned: _request_context_tokens(list(planned), tool_defs, provider),
        artifacts=lambda handle: prefetched.get(handle),
        safety_headroom_tokens=0,
        min_recent_groups=1,
        count_basis=_COUNT_BASIS,
    )


async def evaluate_request(
    messages: list[dict[str, Any]],
    *,
    session: Any,
    provider: Any,
    tool_defs: Any,
    session_id: Any,
    agent_id: str,
    user_message: str = "",
    total_tokens: int = 0,
    iterations: int = 0,
    tool_calls_made: list[dict[str, Any]] | None = None,
) -> RequestGate:
    """One projection/capacity decision for the exact next request.

    Called by both loops between stale-history condensation and the provider
    call. Pure with respect to the agent: ``messages`` is never mutated, no
    tool effect is replayed on reprojection (a read-only lookup is the only
    I/O), and the returned ``messages`` is the caller's own list on every
    non-wired path.
    """
    effective_budget = _effective_turn_budget(session, provider)

    def counter(planned: Sequence[Mapping[str, Any]]) -> int:
        return _request_context_tokens(list(planned), tool_defs, provider)

    if _wired_enabled():
        gate = await _wired_gate(
            messages,
            session=session,
            provider=provider,
            tool_defs=tool_defs,
            session_id=session_id,
            agent_id=agent_id,
            user_message=user_message,
            total_tokens=total_tokens,
            iterations=iterations,
            tool_calls_made=tool_calls_made,
            effective_budget=effective_budget,
            counter=counter,
        )
        if gate is not None:
            return gate
        # Degraded wiring: keep the legacy request; the hard guard below
        # still enforces capacity (never a silent lossy send).
    else:
        report = await _shadow_note(
            messages,
            session=session,
            provider=provider,
            tool_defs=tool_defs,
            session_id=session_id,
            agent_id=agent_id,
            user_message=user_message,
        )
        _audit_decision(
            mode="shadow",
            degraded=report is None,
            would_fit=bool(report["would_fit"]) if report is not None else None,
            refusal=(
                (report.get("refusal") or {}).get("code") if report is not None else None
            ),
            estimated_tokens=report["estimated_tokens"] if report is not None else None,
            effective_budget=effective_budget,
            request_tokens=None,
            passes=report["passes"] if report is not None else None,
            projected=False,
            session_id=session_id,
        )

    request_tokens = counter(messages)
    failure = None
    if request_tokens > effective_budget:
        failure = _guard_failure(
            request_tokens,
            effective_budget,
            session_id=session_id,
            total_tokens=total_tokens,
            iterations=iterations,
            tool_calls_made=tool_calls_made,
        )
    return RequestGate(
        messages=messages,
        effective_budget=effective_budget,
        request_tokens=request_tokens,
        failure=failure,
    )


async def _shadow_note(
    messages: Sequence[Mapping[str, Any]],
    *,
    session: Any,
    provider: Any,
    tool_defs: Any,
    session_id: Any,
    agent_id: str,
    user_message: str,
) -> dict[str, Any] | None:
    """Best-effort shadow projection: bounded log only, payload never sent.

    Returns the redacted report for the caller's audit event, or ``None`` when
    the shadow projection itself failed (the caller degrades honestly).
    """
    try:
        inp = await _build_input(
            messages,
            session=session,
            provider=provider,
            tool_defs=tool_defs,
            session_id=session_id,
            agent_id=agent_id,
            user_message=user_message,
        )
        report = shadow_report(inp, baseline=messages)
    except Exception as error:
        record_failure("request_gate.shadow", error)
        return None
    refusal = report.get("refusal") or {}
    baseline = report.get("baseline") or {}
    _SHADOW_LOGGER.info(
        "request_projection shadow would_fit=%s refusal=%s estimated_tokens=%d "
        "effective_budget_tokens=%d passes=%d retained=%d projected=%d omitted=%d "
        "pins=%d pinned_survived=%s baseline_messages=%d baseline_sha256=%.12s "
        "basis=%s input_revision=%.12s",
        report["would_fit"],
        refusal.get("code", "none"),
        report["estimated_tokens"],
        report["effective_budget_tokens"],
        report["passes"],
        len(report["retained"]),
        len(report["projected"]),
        len(report["omitted"]),
        len(report["retained_pins"]),
        report["pinned_survived"],
        baseline.get("messages", len(messages)),
        str(baseline.get("sha256", "")),
        report["basis"],
        report["input_revision"],
    )
    return report


async def _wired_gate(
    messages: list[dict[str, Any]],
    *,
    session: Any,
    provider: Any,
    tool_defs: Any,
    session_id: Any,
    agent_id: str,
    user_message: str,
    total_tokens: int,
    iterations: int,
    tool_calls_made: list[dict[str, Any]] | None,
    effective_budget: int,
    counter: Callable[[Sequence[Mapping[str, Any]]], int],
) -> RequestGate | None:
    """Project the request. ``None`` means degrade to the legacy path."""
    try:
        inp = await _build_input(
            messages,
            session=session,
            provider=provider,
            tool_defs=tool_defs,
            session_id=session_id,
            agent_id=agent_id,
            user_message=user_message,
        )
        result = project_request(inp)
    except Exception as error:
        record_failure("request_gate.wired", error)
        _audit_decision(
            mode="wired",
            degraded=True,
            would_fit=None,
            refusal=None,
            estimated_tokens=None,
            effective_budget=effective_budget,
            request_tokens=None,
            passes=None,
            projected=False,
            session_id=session_id,
        )
        return None
    _log_wired(result, baseline_count=len(messages), effective_budget=effective_budget)

    if result.refusal is not None:
        failure = _refusal_failure(
            result.refusal,
            session_id=session_id,
            total_tokens=total_tokens,
            iterations=iterations,
            tool_calls_made=tool_calls_made,
        )
        gate = RequestGate(
            messages=messages,
            effective_budget=effective_budget,
            request_tokens=result.refusal.required_tokens,
            failure=failure,
            refusal=result.refusal,
        )
        _audit_decision(
            mode="wired",
            degraded=False,
            would_fit=False,
            refusal=result.refusal.code,
            estimated_tokens=result.estimated_tokens,
            effective_budget=effective_budget,
            request_tokens=gate.request_tokens,
            passes=result.passes,
            projected=False,
            session_id=session_id,
        )
        return gate

    planned = list(result.planned_messages)
    request_tokens = counter(planned)
    failure = None
    if request_tokens > effective_budget:
        # The projector measured a fit; this is the final hard-guard
        # enforcement on the exact outgoing payload (defensive backstop).
        failure = _guard_failure(
            request_tokens,
            effective_budget,
            session_id=session_id,
            total_tokens=total_tokens,
            iterations=iterations,
            tool_calls_made=tool_calls_made,
        )
    gate = RequestGate(
        messages=planned,
        effective_budget=effective_budget,
        request_tokens=request_tokens,
        failure=failure,
        projected=True,
    )
    _audit_decision(
        mode="wired",
        degraded=False,
        would_fit=gate.failure is None,
        refusal=None,
        estimated_tokens=result.estimated_tokens,
        effective_budget=effective_budget,
        request_tokens=gate.request_tokens,
        passes=result.passes,
        projected=True,
        session_id=session_id,
    )
    return gate


def _audit_decision(
    *,
    mode: str,
    degraded: bool,
    would_fit: bool | None,
    refusal: str | None,
    estimated_tokens: int | None,
    effective_budget: int,
    request_tokens: int | None,
    passes: int | None,
    projected: bool,
    session_id: Any,
) -> None:
    """One bounded decision record per gate evaluation (§14.7).

    Labels, booleans and token counts only — never message bodies, pins, or
    refusal text. Emission is best-effort: a failed audit call must never
    change the gate's decision or the outgoing payload.
    """
    try:
        audit_log(
            "request_projection_decision",
            session_id=str(session_id),
            mode=mode,
            degraded=bool(degraded),
            would_fit=would_fit,
            refusal=refusal,
            estimated_tokens=estimated_tokens,
            effective_budget_tokens=effective_budget,
            request_tokens=request_tokens,
            passes=passes,
            projected=bool(projected),
        )
    except Exception as error:
        record_failure("request_gate.audit", error)


def _log_wired(result: ProjectionResult, *, baseline_count: int, effective_budget: int) -> None:
    """Bounded wired-path log (same redaction discipline as the shadow log)."""
    refusal = result.refusal
    logger.info(
        "request_projection wired would_fit=%s refusal=%s estimated_tokens=%d "
        "effective_budget_tokens=%d passes=%d retained=%d projected=%d omitted=%d "
        "pins=%d pinned_survived=%s baseline_messages=%d basis=%s input_revision=%.12s",
        refusal is None,
        refusal.code if refusal is not None else "none",
        result.estimated_tokens,
        effective_budget,
        result.passes,
        len(result.retained),
        len(result.projected),
        len(result.omitted),
        len(result.retained_pins),
        result.pinned_survived,
        baseline_count,
        result.basis,
        result.input_revision,
    )


def _guard_failure(
    request_tokens: int,
    effective_budget: int,
    *,
    session_id: Any,
    total_tokens: int,
    iterations: int,
    tool_calls_made: list[dict[str, Any]] | None,
) -> AgentResponse:
    """The pre-B2 hard-guard failure shape, byte-compatible with the loops."""
    logger.warning("Model context window exceeded (%d > %d)", request_tokens, effective_budget)
    audit_log(
        "agent_request_context_exceeded",
        session_id=str(session_id),
        total_tokens=total_tokens,
        request_tokens=request_tokens,
        context_window=effective_budget,
    )
    return AgentResponse(
        content=_CAPACITY_MESSAGE,
        outcome="failed",
        tool_calls=tool_calls_made if tool_calls_made is not None else [],
        tokens_used=total_tokens,
        iterations=iterations,
    )


def _refusal_failure(
    refusal: ProjectionRefusal,
    *,
    session_id: Any,
    total_tokens: int,
    iterations: int,
    tool_calls_made: list[dict[str, Any]] | None,
) -> AgentResponse:
    """Structured capacity failure: same event/outcome shape + safe options."""
    logger.warning(
        "Model context window exceeded (%d > %d); request projection refused (%s)",
        refusal.required_tokens,
        refusal.budget_tokens,
        refusal.code,
    )
    audit_log(
        "agent_request_context_exceeded",
        session_id=str(session_id),
        total_tokens=total_tokens,
        request_tokens=refusal.required_tokens,
        context_window=refusal.budget_tokens,
        refusal=refusal.code,
    )
    options = " ".join(refusal.safe_options)
    return AgentResponse(
        content=f"{refusal.message} Safe options: {options}",
        outcome="failed",
        tool_calls=tool_calls_made if tool_calls_made is not None else [],
        tokens_used=total_tokens,
        iterations=iterations,
    )
