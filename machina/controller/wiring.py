"""S3 wiring-wave helpers: advisory turn hooks owned by the controller package.

Thin, never-raising shims used ONLY from ``machina/core/agent.py`` hook
points (each hook is delimited BEGIN/END there for independent revert).
Nothing here changes prompts, delegation, ranking, or loop bounds:

- :func:`log_section_tokens` — diagnostics-only per-section token log via
  the existing prompt-section helpers (never mutates input; performs no
  JSON dump / tokenization unless DEBUG is enabled for this logger).
- :func:`record_triage` — controller triage classification recorded on the
  agent (``agent._last_triage``), never enforced; any import failure is
  swallowed so turns always proceed.
- :func:`log_complexity` — complexity fan-out recommendation logged only;
  delegation behavior is untouched.
- :func:`target_bytes` / :func:`check_stagnation` / :func:`stagnation_content`
  — central per-tool (target, new_bytes) mapping + stagnation fold over
  ``machina/core/progress.py``. Stagnation is not an iteration cap: it fires
  only when K consecutive rounds add no new coverage AND no artifact bytes.
- :func:`trace_tool_call` / :func:`record_approval_pause` — S3.3 observe-only
  trace producers (wave-2 ``machina/observability/traces.py`` spans had no
  production callers). A tracing failure degrades to a no-op and never gates
  execution: the tool still runs and the turn still completes.
- :func:`stagnation_evidence` / :func:`stagnation_state_fields` /
  :func:`record_stagnation_state` — the forced write behind
  :func:`stagnation_content`'s claim: a short evidence summary (tool counts,
  no LLM call) persisted as session state. Session state is durable and is
  NOT part of ``context.get_recent_context``, so the stop marker can never
  re-enter the next turn's recent-context window (which cannot exclude
  chunk types today).

Mapping rule (single source of truth, see :func:`target_bytes` and
:data:`_TARGET_KEYS`):
only RETRIEVAL-family tools are stagnation-eligible (target=joined
identifying args in ``_TARGET_KEYS`` — e.g. web_extract→url,
search_files→pattern|path, web_search→query — bytes=0 always; reads add no
artifact bytes, so progress comes only from new-target coverage). Distinct
URLs/patterns/queries are therefore NEW coverage and can never trip the
stagnation stop. Every other tool (writes, file reads, terminal, delegate,
skill bodies, unknown shapes) defaults conservative non-stagnant (bytes=1):
re-execution there is often legitimate (verify-after-write, polling,
retries) and the agent loop must stay UNBOUNDED — see
tests/test_comprehensive.py::TestReActAgent::test_agent_unbounded_tool_calls
(12 identical read_file rounds must run to completion). Unknown shapes can
therefore never force a stop.

Module-level imports are stdlib-only (no ``machina`` edges); every
``machina`` import below is deferred inside functions so this module can
never create an import cycle and never breaks turns.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from typing import Any

logger = logging.getLogger(__name__)

_ELIGIBLE = frozenset(
    {
        "session_recall",
        "session_recall_window",
        "recall",
        "search_documents",
        "search_files",
        "skill_list",
        "web_search",
        "web_extract",
        "web_research",
        "harness_status",
        "list_agents",
    }
)

# Per-tool target extraction (single source of truth). Distinct calls MUST
# yield distinct targets or legitimate multi-item loops read as stagnant:
# web_extract on distinct URLs or search_files for distinct patterns is NEW
# coverage every round (wave-2 audit regression: a generic key list lacked
# "url" and let "path" shadow "pattern", stopping real research at ~4 calls).
# Keys listed for a tool are joined (e.g. "pattern|path"), so a difference in
# ANY listed key counts as new coverage. Tools with no identifying args
# present fall back to the tool name: repeated identical status reads
# (harness_status/list_agents) are genuinely stagnant.
_TARGET_KEYS: dict[str, tuple[str, ...]] = {
    "session_recall": ("query",),
    "recall": ("query",),
    "search_documents": ("query",),
    "search_files": ("pattern", "path"),
    "skill_list": ("query",),
    "web_search": ("query",),
    "web_extract": ("url",),
    "web_research": ("query", "url"),
}


def log_section_tokens(messages: Any, tools: Any) -> None:
    """Log per-section token counts; read-only, never mutates input.

    No-op unless DEBUG is enabled for this logger: the JSON dump plus
    tokenization of messages+tools previously ran eagerly on EVERY LLM call.
    """
    try:
        if not logger.isEnabledFor(logging.DEBUG):
            return
        from machina.core.prompt import prompt_section_tokens

        sections: dict[str, str] = {
            "recent": json.dumps(messages, ensure_ascii=False, default=str),
            "tools": json.dumps(tools, ensure_ascii=False, default=str),
        }
        logger.debug("s3 sections %s", prompt_section_tokens(sections))
    except Exception:
        pass


def record_triage(agent: Any, user_message: str) -> None:
    """Record controller triage label on the agent; never enforced, never raises."""
    try:
        from machina.controller.triage import resolve_task

        agent._last_triage = resolve_task(user_message)
        logger.debug("s3 triage task=%s", agent._last_triage)
    except Exception:
        pass


def log_complexity(agent: Any, user_message: str) -> None:
    """Log complexity fan-out recommendation; advisory only, never raises."""
    try:
        from machina.controller.complexity import score_complexity

        out = score_complexity(user_message)
        agent._last_complexity = out
        logger.debug("s3 complexity level=%s score=%s", out.get("level"), out.get("score"))
    except Exception:
        pass


# ─── S3.3 observe-only trace producers ──────────────────────────────────────


class _ToolTrace:
    """Handle for one traced tool call; labels the spans at finish time."""

    def __init__(self, tool: Any = None, delegation: Any = None) -> None:
        self.tool = tool
        self.delegation = delegation

    def finish(self, outcome: str = "completed") -> None:
        """Attach the truthful outcome/status; a tracing failure is a no-op."""
        try:
            if self.tool is not None:
                self.tool.set(outcome=str(outcome or "completed"))
            if self.delegation is not None:
                self.delegation.set(status=str(outcome or "completed"))
        except Exception:
            pass


def _enter_trace_span(stack: ExitStack, manager: Any) -> Any:
    """Enter one observe-only span; failure degrades to a missing span."""
    try:
        return stack.enter_context(manager)
    except Exception:
        return None


def _label_delegation_child(item: Any, span: Any) -> None:
    """Attach the child session id when a delegated pause surfaced one."""
    try:
        if span is None or not isinstance(item, dict):
            return
        pauses = item.get("_needs_approvals") or [item.get("_needs_approval")]
        child = next(
            (
                str(p.get("session_id"))
                for p in pauses
                if isinstance(p, dict) and p.get("session_id")
            ),
            "",
        )
        if child:
            span.name = child
            span.attributes["child_session_id"] = child
    except Exception:
        pass


@contextmanager
def trace_tool_call(item: Any) -> Iterator[_ToolTrace]:
    """Child spans for one tool item: a tool span per call plus, for
    ``delegate`` entries, a delegation span. Observe-only: any tracing
    failure degrades to a no-op and never gates execution. The caller labels
    the outcome via ``handle.finish(outcome)`` after execution."""
    handle = _ToolTrace()
    stack: ExitStack | None = None
    try:
        from machina.observability.traces import delegation_span, tool_span

        name = str((item or {}).get("name") or "unknown") if isinstance(item, dict) else "unknown"
        args = item.get("args") if isinstance(item, dict) else {}
        stack = ExitStack()
        handle.tool = _enter_trace_span(stack, tool_span(name, args))
        if name == "delegate":
            agent_name = str(args.get("agent") or "") if isinstance(args, dict) else ""
            handle.delegation = _enter_trace_span(
                stack, delegation_span("", child_agent=agent_name)
            )
    except Exception:
        stack = None
    if stack is None:
        yield handle
        return
    try:
        yield handle
    except BaseException:
        try:
            stack.__exit__(*sys.exc_info())
        except Exception:
            pass
        raise
    else:
        _label_delegation_child(item, handle.delegation)
        try:
            stack.__exit__(None, None, None)
        except Exception:
            pass


def record_approval_pause(approval: Any) -> None:
    """Observe-only approval span for one needs-approval pause; never raises."""
    try:
        from machina.observability.traces import approval_span

        if not isinstance(approval, dict):
            return
        with approval_span(str(approval.get("request_id", ""))) as span:
            if span is not None:
                span.set(
                    operation=str(approval.get("operation", "")),
                    target=str(approval.get("target", "")),
                    outcome="paused",
                )
    except Exception:
        pass


# ─── stagnation mapping + forced-write evidence ─────────────────────────────


def target_bytes(tool: Any, args: Any, preview: Any, outcome: Any) -> tuple[str, int]:
    """Central per-tool (target, new_bytes) extraction (see module mapping rule)."""
    try:
        name = str(tool or "unknown")
        argv = args if isinstance(args, dict) else {}
        if name not in _ELIGIBLE:
            return (name, 1)
        if name == "session_recall_window":
            return (f"{argv.get('session_id', '')}:{argv.get('chunk_id', '')}" or name, 0)
        keys = _TARGET_KEYS.get(name, ())
        parts = [str(argv[k]) for k in keys if argv.get(k)]
        return ("|".join(parts)[:500] if parts else name, 0)
    except Exception:
        return (str(tool or "unknown"), 1)


def check_stagnation(history: Any, batch: Any) -> tuple[Any, str | None]:
    """Fold one tool batch into progress history; return (history, stop|None)."""
    try:
        from machina.core.progress import is_stagnant, record, stop_reason

        hist = list(history or [])
        for entry in batch or []:
            if isinstance(entry, dict):
                t, b = target_bytes(
                    entry.get("tool"),
                    entry.get("args"),
                    entry.get("result_preview"),
                    entry.get("outcome"),
                )
                hist = record(hist, str(entry.get("tool") or "unknown"), t, b)
        return (hist, stop_reason(hist) if is_stagnant(hist) else None)
    except Exception:
        return (history, None)


def stagnation_evidence(tool_calls: Any) -> dict[str, int]:
    """Counts by tool from ``tool_calls_made`` (the forced-write evidence)."""
    counts: dict[str, int] = {}
    try:
        for entry in tool_calls or []:
            if isinstance(entry, dict):
                name = str(entry.get("tool") or "unknown")
                counts[name] = counts.get(name, 0) + 1
    except Exception:
        pass
    return dict(sorted(counts.items()))


def stagnation_state_fields(stop: str, text: str, tool_calls: Any) -> dict[str, Any]:
    """msgpack-safe session-state payload for one stagnation stop."""
    entries = list(tool_calls or [])
    return {
        "stagnation_stop": {
            "reason": str(stop),
            "stop_text": str(text),
            "tool_calls": len(entries),
            "evidence": stagnation_evidence(entries),
        }
    }


async def record_stagnation_state(
    session_manager: Any, session_id: Any, stop: str, text: str, tool_calls: Any
) -> None:
    """Forced write (no LLM call): persist the stop reason + evidence as state.

    The marker must not be written as a context chunk: chunks re-enter the
    next turn's recent-context window and ``get_recent_context`` cannot
    exclude chunk types. Session state is durable and never re-injected.
    Never raises; a failed write leaves the turn's outcome unchanged.
    """
    try:
        await session_manager.update_state_fields(
            session_id, stagnation_state_fields(stop, text, tool_calls)
        )
    except Exception:
        logger.debug("stagnation state write failed", exc_info=True)


def stagnation_content(stop: str, n: int, tool_calls: Any = None) -> str:
    """Truthful stop text for a stagnation halt (no success claim, no cap language)."""
    evidence = stagnation_evidence(tool_calls)
    summary = ", ".join(f"{name}×{count}" for name, count in evidence.items())
    return (
        f"{stop}\n\nStopped after {n} tool calls: no new file coverage and no artifact "
        f"bytes for K consecutive rounds. Evidence summary recorded as session state "
        f"({summary or 'no tool evidence'}); no further model call was made."
    )
