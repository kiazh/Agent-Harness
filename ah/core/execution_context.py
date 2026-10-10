"""Resolve the execution boundary once; backends consume broker snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ah.core.config import config
from ah.core.session_mode import MODES, get_effective_mode, resolve_effective_mode


@dataclass(frozen=True)
class ExecutionContext:
    mode: str
    backend: str
    workspace: Path


def configured_workspace() -> Path:
    return Path(
        config.get("workspace_root") or config.get("agent_harness_home") or Path.cwd()
    ).resolve()


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
    from ah.permissions.broker import get_execution_context

    snapshot = get_execution_context()
    if snapshot:
        root = (
            Path(snapshot["workspace_root"]).resolve()
            if snapshot.get("workspace_root")
            else configured_workspace()
        )
        return _context(str(snapshot["mode"]).lower(), tool, root, snapshot.get("backend"))
    from ah.tools.agents import current_session_id

    sid = current_session_id.get()
    if sid:
        from ah.db.connection import db

        if db.connected:
            raise RuntimeError("session execution requires a broker-approved context")
    root = (
        workspace_override.resolve() if workspace_override is not None else configured_workspace()
    )
    return _context(get_effective_mode(str(sid) if sid else None), tool, root)
