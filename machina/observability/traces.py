"""Hierarchical run tracing — LangSmith-style run/trace hierarchy, fully in-process.

Doctrine (langsmith-observability): every turn is a root *run*; each LLM
call, tool call, delegation, and approval wait is a child run nested under
it. No external service is involved: spans live in memory for the duration
of the turn and are reclaimed via a bounded ring.

Conventions (phoenix-observability): spans carry ``kind`` / ``name`` /
``status`` plus a small attribute bag (model, token counts, outcome,
durations). Free text is redacted at record time with the existing
``machina.memory.redaction`` helpers — no new redaction logic lives here.

Observe-only contract: recording functions never gate execution logic. Sink
emission is swallowed unconditionally, every public mutator degrades to a
no-op on unexpected failure, and ``set_enabled(False)`` makes every entry
point a no-op. Tracing can never break a turn.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from machina.memory.redaction import redact_secrets, redact_value

__all__ = [
    "MAX_LEDGER_ENTRIES_PER_TURN",
    "MAX_RESERVATIONS",
    "MAX_SPANS_PER_TURN",
    "MAX_TURNS_RETAINED",
    "Span",
    "TurnTrace",
    "active_turn",
    "add_sink",
    "approval_span",
    "cancel_turn",
    "clear_sinks",
    "current_turn_id",
    "delegation_span",
    "dropped_reservations",
    "dropped_turns",
    "end_turn",
    "get_turn",
    "llm_span",
    "note_finish",
    "note_reserve",
    "remove_sink",
    "render_diagnostics",
    "reset_for_tests",
    "set_enabled",
    "set_turn_attributes",
    "start_turn",
    "tool_span",
    "tracing_enabled",
    "turn_report",
]

# ─── bounds ─────────────────────────────────────────────────────────────────

#: Max spans retained per turn (root turn span is never evicted). Overflow
#: drops the oldest non-root spans and bumps ``TurnTrace.dropped_spans``.
MAX_SPANS_PER_TURN = 500
#: Max usage-ledger entries retained per turn. Overflow drops the oldest
#: entries and bumps ``TurnTrace.ledger_dropped`` (same ring policy as spans).
MAX_LEDGER_ENTRIES_PER_TURN = 500
#: Max reservation→turn entries retained in the process map (FIFO). Overflow
#: evicts the oldest reservation; late finishes for evicted ids are ignored.
MAX_RESERVATIONS = 512
#: Max finished/unfinished turns retained in the process registry (FIFO).
MAX_TURNS_RETAINED = 128
#: Max characters kept for a redacted tool-args preview.
ARGS_PREVIEW_CHARS = 500
#: Default bound for the diagnostics renderer.
DIAGNOSTICS_MAX_CHARS = 2000

# ─── kinds / statuses ───────────────────────────────────────────────────────

TURN = "turn"
LLM = "llm"
TOOL = "tool"
DELEGATION = "delegation"
APPROVAL = "approval"

STATUS_OPEN = "open"
STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_CANCELLED = "cancelled"


@dataclass
class Span:
    """One node in a turn's run hierarchy (Phoenix-style attributes)."""

    id: str
    kind: str
    name: str
    turn_id: str
    parent_id: str | None
    status: str = STATUS_OPEN
    attributes: dict[str, Any] = field(default_factory=dict)
    started_ms: float = 0.0
    ended_ms: float | None = None
    duration_ms: float | None = None

    def set(self, **attrs: Any) -> Span:
        """Attach operational attributes (model, outcome, counts, ...).

        Stores values as-is; free-text redaction happens at the dedicated
        record paths and defensively in :func:`render_diagnostics`.
        """
        try:
            self.attributes.update(attrs)
        except Exception:
            pass
        return self


@dataclass
class TurnTrace:
    """In-memory span ring plus usage ledger for one turn."""

    turn_id: str
    session_id: str = ""
    agent_id: str = ""
    task_class: str = ""
    status: str = STATUS_OPEN
    stop_reason: str = ""
    parent_turn_id: str = ""
    spans: list[Span] = field(default_factory=list)
    dropped_spans: int = 0
    dropped_tool_spans: int = 0
    dropped_turns: int = 0
    ledger: list[dict[str, Any]] = field(default_factory=list)
    ledger_dropped: int = 0
    ended: bool = False
    _stack_depth: int = 0

    @property
    def root(self) -> Span | None:
        return self.spans[0] if self.spans else None


# ─── process state ──────────────────────────────────────────────────────────

_enabled: bool = True
_TURNS: dict[str, TurnTrace] = {}
#: reservation id (str) -> {turn_id, span_id, reserved_tokens, model}
_RESERVATIONS: dict[str, dict[str, Any]] = {}
#: Reservation entries evicted by the FIFO cap (late finishes are dropped).
_reservations_dropped: int = 0
#: Turn entries evicted by the registry cap (their reports become unavailable;
#: an evicted turn's retained parent carries the per-turn ``dropped_turns``
#: marker, and every eviction is announced on the sink channel).
_turns_dropped: int = 0
_sinks: list[Callable[[str, dict[str, Any]], None]] = []

_active_turn: ContextVar[str | None] = ContextVar("machina_trace_active_turn", default=None)
_turn_stack: ContextVar[tuple[str, ...]] = ContextVar("machina_trace_turn_stack", default=())
_span_stack: ContextVar[tuple[tuple[str, str], ...]] = ContextVar(
    "machina_trace_span_stack", default=()
)


def tracing_enabled() -> bool:
    return _enabled


def set_enabled(flag: bool) -> None:
    global _enabled
    _enabled = bool(flag)


def add_sink(fn: Callable[[str, dict[str, Any]], None]) -> None:
    _sinks.append(fn)


def remove_sink(fn: Callable[[str, dict[str, Any]], None]) -> None:
    try:
        _sinks.remove(fn)
    except ValueError:
        pass


def clear_sinks() -> None:
    del _sinks[:]


def reset_for_tests() -> None:
    """Clear registry, reservations, sinks, and context isolation state."""
    global _enabled, _reservations_dropped, _turns_dropped
    _enabled = True
    _reservations_dropped = 0
    _turns_dropped = 0
    _TURNS.clear()
    _RESERVATIONS.clear()
    del _sinks[:]
    _active_turn.set(None)
    _turn_stack.set(())
    _span_stack.set(())


def dropped_reservations() -> int:
    """Count reservation entries evicted by the FIFO cap in this process."""
    return _reservations_dropped


def dropped_turns() -> int:
    """Count turns evicted by the registry cap in this process.

    An evicted turn's report is gone; the event is marked here globally and on
    the evicted turn's retained parent (``TurnTrace.dropped_turns``), so tool
    and delegation totals from before the eviction are never silently lost
    without a marker.
    """
    return _turns_dropped


def _emit(event: str, payload: dict[str, Any]) -> None:
    """Deliver a trace event to every sink; a failing sink never propagates."""
    for sink in list(_sinks):
        try:
            sink(event, payload)
        except Exception:
            continue


def _now_ms() -> float:
    return time.monotonic() * 1000.0


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


# ─── span storage ───────────────────────────────────────────────────────────

# Span kinds whose exit auto-fills waited_ms from the measured duration.
_TIMED_KINDS = frozenset({APPROVAL})


def _parent_for(turn: TurnTrace) -> str | None:
    """Nearest open-stack span belonging to *turn*, else the turn root."""
    for tid, sid in reversed(_span_stack.get()):
        if tid == turn.turn_id:
            return sid
    root = turn.root
    return root.id if root is not None else None


def _append_span(
    turn: TurnTrace,
    *,
    kind: str,
    name: str,
    attributes: dict[str, Any] | None = None,
    parent_id: str | None = None,
) -> Span:
    if parent_id is None:
        parent_id = _parent_for(turn)
    span = Span(
        id=_new_id(),
        kind=kind,
        name=name,
        turn_id=turn.turn_id,
        parent_id=parent_id,
        attributes=dict(attributes or {}),
        started_ms=_now_ms(),
    )
    turn.spans.append(span)
    if len(turn.spans) > MAX_SPANS_PER_TURN:
        # Bounded ring: evict oldest non-root span, keep dropped-count markers.
        # Tool spans additionally bump their own delta so `turn_report` can
        # flag that `tool_calls.total` no longer equals the real total.
        evicted = turn.spans.pop(1)
        turn.dropped_spans += 1
        if evicted.kind == TOOL:
            turn.dropped_tool_spans += 1
        root = turn.root
        if root is not None:
            root.attributes["dropped_spans"] = turn.dropped_spans
            if turn.dropped_tool_spans:
                root.attributes["dropped_tool_spans"] = turn.dropped_tool_spans
    return span


def _close_span(turn: TurnTrace, span: Span, status: str | None = None) -> None:
    if span.ended_ms is not None:
        return
    if status is not None:
        span.status = status
    elif span.status == STATUS_OPEN:
        span.status = STATUS_OK
    span.ended_ms = _now_ms()
    span.duration_ms = max(0.0, span.ended_ms - span.started_ms)
    if span.kind in _TIMED_KINDS and "waited_ms" not in span.attributes:
        span.attributes["waited_ms"] = span.duration_ms


def _close_open_spans(turn: TurnTrace, status: str) -> None:
    for span in turn.spans:
        if span.ended_ms is None:
            _close_span(turn, span, status if status != STATUS_OK else STATUS_OK)


# ─── turn lifecycle ─────────────────────────────────────────────────────────


def _evict_turns() -> None:
    """Bounded FIFO eviction with markers (R4-064).

    Finished turns are evicted first so an in-flight turn cannot be dropped
    while any closed turn could be; only when every retained turn is in flight
    is the oldest force-evicted, and that case is marked (global counter +
    ``turn_evicted`` sink event, plus the victim's retained parent when there
    is one). Every eviction is marked.
    """
    global _turns_dropped
    while len(_TURNS) > MAX_TURNS_RETAINED:
        victim_id = next((tid for tid, turn in _TURNS.items() if turn.ended), None)
        if victim_id is None:
            victim_id = next(iter(_TURNS))
        victim = _TURNS.pop(victim_id)
        _turns_dropped += 1
        parent = _TURNS.get(victim.parent_turn_id) if victim.parent_turn_id else None
        if parent is not None:
            parent.dropped_turns += 1
            root = parent.root
            if root is not None:
                root.attributes["dropped_turns"] = parent.dropped_turns
        _emit(
            "turn_evicted",
            {
                "turn_id": victim_id,
                "ended": victim.ended,
                "parent_turn_id": victim.parent_turn_id,
            },
        )


def start_turn(
    turn_id: str,
    *,
    session_id: str = "",
    agent_id: str = "",
    task_class: str = "",
) -> TurnTrace | None:
    """Open a turn (root run). Nests inside any currently active turn."""
    if not _enabled:
        return None
    try:
        tid = str(turn_id)
        prev = _active_turn.get()
        if prev is not None:
            _turn_stack.set(_turn_stack.get() + (prev,))
        turn = TurnTrace(
            turn_id=tid,
            session_id=str(session_id or ""),
            agent_id=str(agent_id or ""),
            task_class=str(task_class or ""),
            parent_turn_id=str(prev) if prev is not None else "",
            _stack_depth=len(_span_stack.get()),
        )
        root = Span(
            id=_new_id(),
            kind=TURN,
            name=f"turn:{tid}",
            turn_id=tid,
            parent_id=None,
            attributes={
                "session_id": turn.session_id,
                "agent_id": turn.agent_id,
                "task_class": turn.task_class,
            },
            started_ms=_now_ms(),
        )
        turn.spans.append(root)
        _TURNS[tid] = turn
        _evict_turns()
        _active_turn.set(tid)
        _emit("turn_started", {"turn_id": tid, "session_id": turn.session_id})
        return turn
    except Exception:
        return None


def set_turn_attributes(
    turn_id: str | None = None,
    *,
    agent_id: str | None = None,
    task_class: str | None = None,
    session_id: str | None = None,
) -> None:
    """Enrich a turn (e.g. agent id once the session row is loaded)."""
    if not _enabled:
        return
    try:
        tid = str(turn_id) if turn_id is not None else _active_turn.get()
        turn = _TURNS.get(tid) if tid else None
        if turn is None:
            return
        if agent_id is not None:
            turn.agent_id = str(agent_id)
        if task_class is not None:
            turn.task_class = str(task_class)
        if session_id is not None:
            turn.session_id = str(session_id)
        root = turn.root
        if root is not None:
            root.attributes.update(
                {
                    "session_id": turn.session_id,
                    "agent_id": turn.agent_id,
                    "task_class": turn.task_class,
                }
            )
    except Exception:
        pass


def _restore_parent(tid: str) -> None:
    """Pop context back to the parent turn; drop stray stack references."""
    try:
        stack = _turn_stack.get()
        if _active_turn.get() == tid:
            parent = stack[-1] if stack else None
            _turn_stack.set(stack[:-1] if stack else ())
            _active_turn.set(parent)
        elif tid in stack:
            _turn_stack.set(tuple(t for t in stack if t != tid))
    except Exception:
        pass


def end_turn(
    turn_id: str | None = None,
    *,
    status: str = STATUS_OK,
    stop_reason: str = "",
) -> None:
    """Close a turn and its open spans. Idempotent; never raises."""
    if not _enabled:
        return
    try:
        tid = str(turn_id) if turn_id is not None else _active_turn.get()
        if not tid:
            return
        turn = _TURNS.get(tid)
        if turn is None:
            _restore_parent(tid)
            return
        if not turn.ended:
            turn.status = status
            turn.stop_reason = str(stop_reason or "")
            _close_open_spans(turn, status)
            turn.ended = True
            root = turn.root
            if root is not None:
                root.attributes["stop_reason"] = turn.stop_reason
            # Sweep reservations still owned by this turn so a late finish
            # cannot attribute to a closed ring.
            for key in [k for k, v in _RESERVATIONS.items() if v.get("turn_id") == tid]:
                _RESERVATIONS.pop(key, None)
            try:
                _span_stack.set(tuple(entry for entry in _span_stack.get() if entry[0] != tid))
            except Exception:
                pass
            _emit(
                "turn_ended",
                {"turn_id": tid, "status": turn.status, "stop_reason": turn.stop_reason},
            )
        _restore_parent(tid)
    except Exception:
        pass


def cancel_turn(turn_id: str | None = None) -> None:
    """Close a turn mid-span with the cancelled marker. Idempotent."""
    if not _enabled:
        return
    try:
        tid = str(turn_id) if turn_id is not None else _active_turn.get()
        if not tid:
            return
        turn = _TURNS.get(tid)
        if turn is None or turn.ended:
            return
        end_turn(tid, status=STATUS_CANCELLED, stop_reason="cancelled")
    except Exception:
        pass


def current_turn_id() -> str | None:
    return _active_turn.get()


def active_turn() -> TurnTrace | None:
    tid = _active_turn.get()
    return _TURNS.get(tid) if tid else None


def get_turn(turn_id: str) -> TurnTrace | None:
    try:
        return _TURNS.get(str(turn_id))
    except Exception:
        return None


# ─── child spans ────────────────────────────────────────────────────────────


@contextmanager
def _child_span(kind: str, name: str, attributes: dict[str, Any]) -> Iterator[Span | None]:
    if not _enabled:
        yield None
        return
    turn: TurnTrace | None = None
    span: Span | None = None
    try:
        tid = _active_turn.get()
        turn = _TURNS.get(tid) if tid else None
        if turn is not None and not turn.ended:
            span = _append_span(turn, kind=kind, name=name, attributes=attributes)
    except Exception:
        turn = None
        span = None
    if turn is None or span is None:
        # No-turn path yields OUTSIDE the guarded region: a body exception
        # thrown back into the generator must propagate as itself instead of
        # being caught here and answered with a second yield (which surfaces
        # as "RuntimeError: generator didn't stop after throw()").
        yield None
        return
    token = _span_stack.set(_span_stack.get() + ((turn.turn_id, span.id),))
    try:
        _emit("span_started", {"turn_id": turn.turn_id, "span_id": span.id, "kind": kind})
        yield span
    except BaseException:
        try:
            _close_span(turn, span, STATUS_ERROR)
            _emit(
                "span_ended",
                {"turn_id": turn.turn_id, "span_id": span.id, "status": span.status},
            )
        except Exception:
            pass
        raise
    else:
        try:
            _close_span(turn, span)
            _emit(
                "span_ended",
                {"turn_id": turn.turn_id, "span_id": span.id, "status": span.status},
            )
        except Exception:
            pass
    finally:
        try:
            _span_stack.reset(token)
        except Exception:
            pass


def _args_preview(args: Any) -> str:
    """Redacted args preview using only the existing redaction helpers."""
    try:
        safe = redact_value(args)
        text = redact_secrets(str(safe)).text
    except Exception:
        text = "[unpreviewable]"
    return text[:ARGS_PREVIEW_CHARS]


@contextmanager
def llm_span(model: str = "", *, cached: bool = False) -> Iterator[Span | None]:
    """Child run for one LLM call (usage hook fills token counts)."""
    with _child_span(
        LLM, str(model or "unknown"), {"model": str(model or "unknown"), "cached": bool(cached)}
    ) as span:
        yield span  # type: ignore[misc]


@contextmanager
def tool_span(name: str, args: Any = None) -> Iterator[Span | None]:
    """Child run for one tool call; args stored as a redacted preview only."""
    with _child_span(
        TOOL,
        str(name),
        {"tool": str(name), "args_preview": _args_preview(args if args is not None else {})},
    ) as span:
        yield span  # type: ignore[misc]


@contextmanager
def delegation_span(child_session_id: str = "", *, child_agent: str = "") -> Iterator[Span | None]:
    """Child run for one delegation to a child session."""
    with _child_span(
        DELEGATION,
        str(child_session_id or "unknown"),
        {"child_session_id": str(child_session_id or ""), "child_agent": str(child_agent or "")},
    ) as span:
        yield span  # type: ignore[misc]


@contextmanager
def approval_span(request_id: str) -> Iterator[Span | None]:
    """Child run for one human-approval wait (waited_ms auto-filled on exit)."""
    with _child_span(APPROVAL, str(request_id), {"request_id": str(request_id)}) as span:
        yield span  # type: ignore[misc]


# ─── usage attribution ──────────────────────────────────────────────────────


def _is_nonneg_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def note_reserve(
    reservation_id: Any,
    *,
    model: str = "",
    reserved_tokens: int = 0,
    provider: str = "",
) -> str | None:
    """Record a usage reservation as an open LLM child span on the active turn.

    Returns the span id, or None when tracing is off / no turn is active.
    Never raises; never affects accounting.
    """
    global _reservations_dropped
    if not _enabled:
        return None
    try:
        tid = _active_turn.get()
        turn = _TURNS.get(tid) if tid else None
        if turn is None or turn.ended:
            return None
        try:
            reserved = int(reserved_tokens)
        except (TypeError, ValueError):
            reserved = 0
        span = _append_span(
            turn,
            kind=LLM,
            name=str(model or "unknown"),
            attributes={
                "model": str(model or "unknown"),
                "provider": str(provider or ""),
                "reserved_tokens": max(0, reserved),
            },
        )
        _RESERVATIONS[str(reservation_id)] = {
            "turn_id": turn.turn_id,
            "span_id": span.id,
            "reserved_tokens": max(0, reserved),
            "model": str(model or "unknown"),
        }
        # Bounded FIFO: a long-running process must not accumulate reservation
        # entries forever. Late finishes for evicted ids are ignored (return
        # False), which is the documented degrade for lost companions.
        while len(_RESERVATIONS) > MAX_RESERVATIONS:
            _RESERVATIONS.pop(next(iter(_RESERVATIONS)))
            _reservations_dropped += 1
        _emit(
            "span_started",
            {"turn_id": turn.turn_id, "span_id": span.id, "kind": LLM},
        )
        return span.id
    except Exception:
        return None


def note_finish(
    reservation_id: Any,
    *,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    accounted_tokens: int | None = None,
    failed: bool = False,
    model: str | None = None,
) -> bool:
    """Close the reservation's LLM span and append exact ledger figures.

    Callers pass the *same* figures the usage row persists (``None`` prompt /
    completion when unknown, ``accounted_tokens`` as computed by
    ``UsageStore.finish``). When ``accounted_tokens`` is None (failed calls),
    the stored reservation is used — mirroring the SQL
    ``COALESCE(accounted, reserved_tokens)`` fallback. Always attributes to
    the turn that owned the reservation, so nested turns never corrupt parent
    accounting. Returns False when there is nothing to attribute.
    """
    if not _enabled:
        return False
    try:
        entry = _RESERVATIONS.pop(str(reservation_id), None)
        if entry is None:
            return False
        turn = _TURNS.get(entry["turn_id"])
        if turn is None:
            return False
        known = not failed and _is_nonneg_int(prompt_tokens) and _is_nonneg_int(completion_tokens)
        accounted = accounted_tokens if _is_nonneg_int(accounted_tokens) else None
        if accounted is None and not known:
            # Mirror COALESCE($5, reserved_tokens): failed/unknown calls keep
            # the conservative reservation.
            reserved = entry.get("reserved_tokens", 0)
            accounted = reserved if _is_nonneg_int(reserved) else 0
        turn.ledger.append(
            {
                "prompt_tokens": prompt_tokens if known else None,
                "completion_tokens": completion_tokens if known else None,
                "accounted_tokens": accounted if _is_nonneg_int(accounted) else 0,
                "known": known,
                "failed": bool(failed),
                "model": str(model or entry.get("model") or "unknown"),
            }
        )
        if len(turn.ledger) > MAX_LEDGER_ENTRIES_PER_TURN:
            # Same bounded-ring policy as spans: drop the oldest entry and
            # leave a marker on the root span and the report.
            del turn.ledger[0]
            turn.ledger_dropped += 1
            root = turn.root
            if root is not None:
                root.attributes["ledger_dropped"] = turn.ledger_dropped
        span = next((s for s in turn.spans if s.id == entry["span_id"]), None)
        if span is not None:
            span.attributes.update(
                {
                    "input_tokens": prompt_tokens if known else None,
                    "output_tokens": completion_tokens if known else None,
                    "accounted_tokens": accounted if _is_nonneg_int(accounted) else 0,
                    "failed": bool(failed),
                }
            )
            if model:
                span.attributes["model"] = str(model)
            _close_span(turn, span, STATUS_ERROR if failed else None)
            _emit(
                "span_ended",
                {"turn_id": turn.turn_id, "span_id": span.id, "status": span.status},
            )
        return True
    except Exception:
        return False


# ─── reconciliation + diagnostics ───────────────────────────────────────────


def _outcome_of(span: Span, key: str) -> str:
    value = span.attributes.get(key, None) or span.status
    text = str(value).strip().lower()
    if text in ("ok", "success", "completed", "complete"):
        return STATUS_OK
    if text in ("error", "failed", "failure"):
        return STATUS_ERROR
    if text in ("cancelled", "canceled", "timeout"):
        return STATUS_CANCELLED
    return text or "unknown"


def turn_report(turn_id: str) -> dict[str, Any] | None:
    """Per-turn totals. LLM token figures come from the usage ledger, so they
    equal the persisted ``llm_usage`` figures for fixture runs driven through
    ``UsageStore.reserve`` / ``UsageStore.finish`` with this turn active."""
    try:
        turn = _TURNS.get(str(turn_id))
        if turn is None:
            return None
        ledger = turn.ledger
        by_outcome: dict[str, int] = {}
        tool_total = 0
        by_status: dict[str, int] = {}
        delegation_total = 0
        approval_count = 0
        approval_waited_ms = 0.0
        for span in turn.spans:
            if span.kind == TOOL:
                tool_total += 1
                outcome = _outcome_of(span, "outcome")
                by_outcome[outcome] = by_outcome.get(outcome, 0) + 1
            elif span.kind == DELEGATION:
                delegation_total += 1
                status = _outcome_of(span, "status")
                by_status[status] = by_status.get(status, 0) + 1
            elif span.kind == APPROVAL:
                approval_count += 1
                waited = span.attributes.get("waited_ms", span.duration_ms or 0.0)
                try:
                    approval_waited_ms += float(waited)
                except (TypeError, ValueError):
                    pass
        return {
            "turn_id": turn.turn_id,
            "session_id": turn.session_id,
            "agent_id": turn.agent_id,
            "task_class": turn.task_class,
            "status": turn.status,
            "stop_reason": turn.stop_reason,
            "llm_calls": len(ledger),
            "input_tokens": sum(e["prompt_tokens"] or 0 for e in ledger),
            "output_tokens": sum(e["completion_tokens"] or 0 for e in ledger),
            "accounted_tokens": sum(e["accounted_tokens"] or 0 for e in ledger),
            "unknown_calls": sum(1 for e in ledger if not e["known"]),
            "tool_calls": {"total": tool_total, "by_outcome": by_outcome},
            "delegations": {"total": delegation_total, "by_status": by_status},
            "approval_waits": {"count": approval_count, "waited_ms_total": approval_waited_ms},
            "spans": len(turn.spans),
            "dropped_spans": turn.dropped_spans,
            "dropped_tool_spans": turn.dropped_tool_spans,
            "dropped_turns": turn.dropped_turns,
            "ledger_dropped": turn.ledger_dropped,
        }
    except Exception:
        return None


def render_diagnostics(turn_id: str, *, max_chars: int = DIAGNOSTICS_MAX_CHARS) -> str:
    """Bounded, secret-free one-block summary for gateway/UI spend display."""
    try:
        report = turn_report(turn_id)
        if report is None:
            text = f"trace {turn_id}: no data"
        else:
            tools = report["tool_calls"]
            delegations = report["delegations"]
            approvals = report["approval_waits"]
            tool_bits = ", ".join(f"{k}={v}" for k, v in sorted(tools["by_outcome"].items()))
            del_bits = ", ".join(f"{k}={v}" for k, v in sorted(delegations["by_status"].items()))
            dropped_tools = report["dropped_tool_spans"]
            dropped_turns = report["dropped_turns"]
            # P6: the usage ledger is a capped ring too; without the marker a
            # capped ledger silently understates llm_calls/token totals.
            dropped_ledger = report["ledger_dropped"]
            text = (
                f"turn {report['turn_id']} session {report['session_id'] or '-'}"
                f" agent {report['agent_id'] or '-'}"
                f" [{report['status']}/{report['stop_reason'] or '-'}]\n"
                f"llm: {report['llm_calls']} calls,"
                f" {report['input_tokens']} in / {report['output_tokens']} out tokens"
                f" ({report['accounted_tokens']} accounted,"
                f" {report['unknown_calls']} unknown)"
                + (f" (+{dropped_ledger} dropped)" if dropped_ledger else "")
                + f"\ntools: {tools['total']}"
                + (f" ({tool_bits})" if tool_bits else "")
                + (f" (+{dropped_tools} dropped)" if dropped_tools else "")
                + f"\ndelegations: {delegations['total']}"
                + (f" ({del_bits})" if del_bits else "")
                + f"\napprovals: {approvals['count']} waits,"
                f" {approvals['waited_ms_total']:.0f} ms waited\n"
                f"spans: {report['spans']} (+{report['dropped_spans']} dropped)"
                + (f"\nchild turns evicted: {dropped_turns}" if dropped_turns else "")
            )
        try:
            text = redact_secrets(text).text
        except Exception:
            pass
        max_chars = max(0, int(max_chars))
        if len(text) > max_chars:
            marker = "…[truncated]"
            if max_chars <= len(marker):
                # Bounds below the marker length must still be honored exactly.
                text = text[:max_chars]
            else:
                text = text[: max_chars - len(marker)] + marker
        return text
    except Exception:
        return f"trace {turn_id}: unavailable"
