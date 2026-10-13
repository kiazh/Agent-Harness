"""S3 wiring-wave helpers: advisory turn hooks owned by the controller package.

Thin, never-raising shims used ONLY from ``machina/core/agent.py`` hook
points (each hook is delimited BEGIN/END there for independent revert).
Nothing here changes prompts, delegation, ranking, or loop bounds:

- :func:`log_section_tokens` — diagnostics-only per-section token log via
  the existing prompt-section helpers (never mutates input).
- :func:`record_triage` — controller triage classification recorded on the
  agent (``agent._last_triage``), never enforced; any import failure is
  swallowed so turns always proceed.
- :func:`log_complexity` — complexity fan-out recommendation logged only;
  delegation behavior is untouched.
- :func:`target_bytes` / :func:`check_stagnation` / :func:`stagnation_content`
  — central per-tool (target, new_bytes) mapping + stagnation fold over
  ``machina/core/progress.py``. Stagnation is not an iteration cap: it fires
  only when K consecutive rounds add no new coverage AND no artifact bytes.

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
    """Log per-section token counts; read-only, never mutates input."""
    try:
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


def stagnation_content(stop: str, n: int) -> str:
    """Truthful stop text for a stagnation halt (no success claim, no cap language)."""
    return f"{stop}\n\nStopped after {n} tool calls: no new file coverage and no artifact bytes for K consecutive rounds. Evidence above is preserved; forced synthesize/write attempt recorded."
