"""Resolve the execution boundary once; backends consume broker snapshots.

Stream B seam notes: mode resolution is turn-scoped in
:mod:`machina.core.session_mode` (explicit ``turn_scope()`` or the implicit
broker-turn cache — one durable read per turn, failures never cached). The
broker-stamped snapshot consumed here is immutable for the guard+execute
window; this module never re-resolves mutable globals behind a snapshot.
Deletion test: removing this module pushes boundary logic (mode/backend/
workspace binding) into every tool backend — the seam earns its keep.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from machina.core.config import config
from machina.core.session_mode import (
    MODES,
    begin_turn_scope,
    end_turn_scope,
    get_effective_mode,
    invalidate_turn_mode,
    peek_turn_mode,
    resolve_effective_mode,
    turn_scope,
)

__all__ = [
    "ExecutionContext",
    "configured_workspace",
    "resolve_execution_context",
    "execution_context",
    "turn_scope",
    "begin_turn_scope",
    "end_turn_scope",
    "peek_turn_mode",
    "invalidate_turn_mode",
    "count_prompt_sections",
    "summarize_section_tokens",
]


SECTION_ORDER = ("system", "skills", "tools", "recent", "retrieved", "query")


def count_prompt_sections(sections: Mapping[str, str]) -> dict[str, int]:
    """Token count per prompt section (diagnostics only, never mutates input).

    Split _prepare_context assembly into (system / skills / tools schema /
    recent / retrieved / query) counts loggable per LLM call. Read-only: the
    assembled prompt text is unchanged by counting it.
    """
    from machina.core.assembler import get_token_count

    return {name: get_token_count(sections[name]) for name in sections}


def summarize_section_tokens(counts: Mapping[str, int]) -> str:
    """One-line `name=n` summary in SECTION_ORDER, then stragglers, plus total."""
    parts = [f"{name}={counts[name]}" for name in SECTION_ORDER if name in counts]
    parts += [f"{name}={counts[name]}" for name in counts if name not in SECTION_ORDER]
    parts.append(f"prompt={sum(counts.values())}")
    return "prompt_sections " + " ".join(parts)


@dataclass(frozen=True)
class ExecutionContext:
    mode: str
    backend: str
    workspace: Path


def configured_workspace() -> Path:
    return Path(config.get("workspace_root") or config.get("machina_home") or Path.cwd()).resolve()


def _context(mode: str, tool: str, workspace: Path, backend: str | None = None) -> ExecutionContext:
    if mode not in MODES:
        raise ValueError("invalid execution mode")
    if backend is None:
        backend = (
            "sandbox"
            if tool == "terminal"
            and (mode == "sandbox" or config.get("terminal_sandbox") == "docker")
            else "host"
        )
    if backend not in ("host", "sandbox"):
        raise ValueError("invalid execution backend")
    return ExecutionContext(mode, backend, workspace)


async def resolve_execution_context(session_id: str | None, tool: str = "") -> ExecutionContext:
    return _context(await resolve_effective_mode(session_id), tool, configured_workspace())


def execution_context(
    tool: str = "", *, workspace_override: Path | None = None
) -> ExecutionContext:
    from machina.permissions.broker import get_execution_context

    snapshot = get_execution_context()
    if snapshot:
        root = (
            Path(snapshot["workspace_root"]).resolve()
            if snapshot.get("workspace_root")
            else configured_workspace()
        )
        return _context(str(snapshot["mode"]).lower(), tool, root, snapshot.get("backend"))
    from machina.tools.agents import current_session_id

    sid = current_session_id.get()
    if sid:
        from machina.db.connection import db

        if db.connected:
            raise RuntimeError("session execution requires a broker-approved context")
    root = (
        workspace_override.resolve() if workspace_override is not None else configured_workspace()
    )
    return _context(get_effective_mode(str(sid) if sid else None), tool, root)
