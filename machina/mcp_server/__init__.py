"""Machina MCP server shim — phase 1 interop surface (stdio transport).

Exposes exactly three tools to MCP hosts (OpenCode, Codex, Claude Desktop):

``machina_status()``
    Read-only status text from the same in-process data path as
    ``machina status`` (``machina.services.status_summary``).

``machina_chat(prompt, session_id=None, mode=None)``
    One headless ``machina chat`` turn in a bounded child process. Text only
    (attachments/images are unsupported in phase 1). A turn that pauses for
    approval (CLI exit 3) returns a structured ``paused_for_approval`` result
    instead of hanging.

``machina_session_create(title=None)``
    Create an empty session row through ``session_manager`` and return its id.

Process discipline shares the repo's terminal-tool leaf mechanics
(``machina/core/child_process.py``, AH-AUDIT-010/023): children run with a
minimal environment allowlist, their own process group, a kill-tree
deadline, and bounded output — the dual-stream bounded runner itself lives
in that leaf (round-5 review F2) and this shim adapts it to
``ProcessResult``. The tool registry is deliberately not imported
at startup so the server stays light; status imports Machina pieces lazily
inside the call.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import sys
import threading
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from machina import __version__

logger = logging.getLogger("machina.mcp_server")

# Editable-checkout root. NOT the child cwd anymore (interop review N2): in a
# wheel install this resolves to site-packages, and children used to run with
# the wrong workspace root. It now documents the checkout layout only; the
# `-m machina` entry runs under the server's own interpreter, so import
# resolution comes from the installed/editable distribution, not from here.

__all__ = [
    "ProcessResult",
    "build_server",
    "machina_chat",
    "machina_session_create",
    "machina_status",
    "main",
]

# ---------------------------------------------------------------------------
# Bounds (every tool output is capped)
# ---------------------------------------------------------------------------

MAX_PROMPT_CHARS = 8_000
MAX_REPLY_CHARS = 16_000
MAX_ERROR_CHARS = 2_000
MAX_STATUS_CHARS = 4_000
# Subprocess read caps; stdout matches machina/tools/terminal.py MAX_OUTPUT_CHARS.
MAX_OUTPUT_CHARS = 64_000
# stderr (audit/log chatter) is kept only for diagnostics and never merged into
# the reply; it is drained past the cap so a chatty child cannot deadlock.
MAX_STDERR_CHARS = 8_000
DEFAULT_CHAT_TIMEOUT_SECONDS = 300.0
VALID_MODES = ("ask", "workspace", "sandbox", "full")

# R5-A (finding 1): tool-supplied modes are capped by a server-side ceiling so
# an MCP caller can never select broader host authority than the operator
# configured. Rank is ascending authority; MCP_MAX_MODE can only tighten below
# the operator's own execution_mode, never escalate above it.
_MODE_RANK = {"ask": 0, "workspace": 1, "sandbox": 2, "full": 3}
# Bounded wait for the child tree to retire after a cancelled request signals
# the stop handle (R5-A finding 15); run_bounded kills promptly on the event,
# so this budget only covers teardown, never a full turn.
CANCEL_JOIN_SECONDS = 10.0


def _operator_mode() -> str:
    """The operator's configured execution mode (the ceiling's default)."""
    try:
        from machina.core.config import config

        mode = str(config.get("execution_mode") or "ask").strip().lower()
    except Exception:
        return "ask"
    return mode if mode in _MODE_RANK else "ask"


def _mcp_mode_ceiling() -> str:
    """Maximum mode a tool argument may request (R5-A finding 1).

    Default is the operator's own execution_mode. ``MCP_MAX_MODE`` (server
    environment, set by the human operator at launch) may only tighten the
    ceiling, never raise it above the operator mode; an invalid value fails
    closed to ``ask`` and is logged.
    """
    operator = _operator_mode()
    raw = os.environ.get("MCP_MAX_MODE", "").strip().lower()
    if not raw:
        return operator
    if raw not in _MODE_RANK:
        logger.warning("invalid MCP_MAX_MODE %r; failing closed to 'ask'", raw)
        return "ask"
    if _MODE_RANK[raw] > _MODE_RANK[operator]:
        return operator
    return raw


SESSION_ID_PATTERN = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_SESSION_LINE_RE = re.compile(
    rf"^Session ID:\s*(?P<session_id>{SESSION_ID_PATTERN.pattern})\s*$", re.MULTILINE
)
_PENDING_REQUEST_RE = re.compile(
    r"^\s*request\s+(?P<request_id>[A-Za-z0-9_-]+)(?:\s+\((?P<detail>[^)]*)\))?\s*$"
)
# CLI chatter around the reply (see machina/cli/__init__.py chat path).
_NOISE_LINE_PREFIXES = (
    "New session:",
    "Resuming session:",
    "Continuing session:",
    "Session ID:",
    "Model:",
    "Mode:",
    "Iterations:",
)

# AH-AUDIT-010: children inherit only the platform variables they need to
# start; the allowlist, sanitizer, and process-group helpers are shared with
# machina/tools/terminal.py via machina/core/child_process.py (stdlib-only).
# The binding is function-level because the shim must not pull the Machina
# runtime at startup. No passthrough extension here: the unused
# MACHINA_MCP_EXTRA_ENV hook was removed by the 2026-10-14 interop review —
# a new name needs a named host case and a test.


def _child_process():
    """Lazily bound stdlib-only child-process mechanics (shim stays light)."""
    from machina.core import child_process

    return child_process


def _sanitized_env() -> dict[str, str]:
    """Minimal child environment without secrets (never dumps the environment)."""
    env = _child_process().sanitized_env(None)
    # Display-only: keep rich from hard-wrapping the reply at the pipe width
    # and strip color even when TERM claims otherwise.
    env["COLUMNS"] = "400"
    env["NO_COLOR"] = "1"
    return env


def _child_database_env() -> dict[str, str]:
    """Environment override pinning children to the parent's database (R4-081).

    The in-process tools (``machina_status``, ``machina_session_create``)
    connect through the shared ``machina.db.connection.db`` singleton, whose
    DSN the parent already resolved from config/``DATABASE_URL``/
    ``MACHINA_ENV_FILE``. Children run with the minimal allowlist above, so
    without this explicit handoff they would re-resolve those sources on their
    own and could observe a different database than the same server's tools.
    The resolved DSN travels only in the child environment; it is never logged
    or returned. No resolved DSN (test/degraded runtimes) adds no override.
    """
    from machina.db.connection import db

    dsn = str(db.dsn or "")
    return {"DATABASE_URL": dsn} if dsn else {}


def _bounded(text: str, limit: int) -> str:
    """Cap a string at ``limit`` characters, marker included."""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len("\n[truncated]"))] + "\n[truncated]"


def _redacted_tail(text: str, limit: int) -> str:
    """Bounded tail of diagnostic text with secret-looking strings redacted."""
    tail = text[-limit:] if len(text) > limit else text
    try:
        from machina.memory.redaction import redact_secrets

        tail = redact_secrets(tail).text
    except Exception:
        pass
    return tail.strip()


# ---------------------------------------------------------------------------
# Bounded child process runner (thin adapter over child_process.run_bounded)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProcessResult:
    """Outcome of one bounded child process (stdout and stderr separately)."""

    returncode: int | None
    output: str
    stderr: str
    timed_out: bool
    truncated: bool


# The bounded runner — twin capped readers, retained-pgid group wait, timer
# and kill-tree teardown, typed spawn-failure capture — lives once in
# machina/core/child_process.py (round-5 review F2); _run_process binds the
# leaf per call via _child_process() and maps its result onto ProcessResult.


def _default_child_cwd() -> Path:
    """Directory the MCP host launched this server in, resolved per call.

    N2 (interop review): ``REPO_ROOT`` defaulted children to the checkout
    root, which is only correct for editable installs — in a wheel it is
    site-packages, so ``machina chat`` ran with the wrong workspace root. The
    launch directory is what a user running ``machina chat`` in the serving
    project would get. An explicit ``workspace_root``/``machina_home`` config
    still wins inside the child (``machina.core.execution_context`` prefers
    them over the cwd), so the shim does not import config to second-guess it.
    """
    return Path(os.getcwd())


def _run_process(
    cmd: list[str],
    *,
    timeout: float,
    cwd: Path | None = None,
    env_extra: dict[str, str] | None = None,
    cancel_event: threading.Event | None = None,
) -> ProcessResult:
    """Run one child through the shared bounded runner (round-5 review F2).

    ``cwd=None`` (default) runs the child in the host launch directory
    captured at call time (see ``_default_child_cwd``); pass ``cwd`` to pin a
    different working directory. ``env_extra`` merges caller-supplied,
    explicitly resolved values (e.g. the parent's database DSN) onto the
    sanitized allowlist — the only channel for values the child may not
    re-derive. stdout (the reply) and stderr (audit/log chatter) are read
    separately, each capped. Prolific stdout stops the child; stderr past its
    cap is drained and discarded so the pipe cannot deadlock. On deadline
    expiry the whole group is killed; this function never waits past
    ``timeout`` plus bounded teardown. A set ``cancel_event`` kills the tree
    too (R5-A finding 15: the async handler's stop handle). Spawn failures
    come back as a typed result (``stderr``), never raised.
    """
    child_cwd = cwd if cwd is not None else _default_child_cwd()
    env = _sanitized_env()
    if env_extra:
        env.update(env_extra)
    result = _child_process().run_bounded(
        cmd,
        env=env,
        timeout=timeout,
        cwd=child_cwd,
        max_stdout=MAX_OUTPUT_CHARS,
        max_stderr=MAX_STDERR_CHARS,
        cancel_event=cancel_event,
    )
    return ProcessResult(
        returncode=result.returncode,
        output=result.stdout,
        stderr=result.stderr,
        timed_out=result.timed_out,
        truncated=result.truncated,
    )


# ---------------------------------------------------------------------------
# In-process database access (serialized; never leaves a pool open)
# ---------------------------------------------------------------------------

_DB_SEQUENCE_LOCK: asyncio.Lock | None = None


def _db_lock() -> asyncio.Lock:
    """One lazy lock serializing connect/close of the shared db singleton."""
    global _DB_SEQUENCE_LOCK
    if _DB_SEQUENCE_LOCK is None:
        _DB_SEQUENCE_LOCK = asyncio.Lock()
    return _DB_SEQUENCE_LOCK


@contextlib.asynccontextmanager
async def _db_section() -> AsyncIterator[Any]:
    """Connected ``db`` singleton for the duration of one tool call."""
    async with _db_lock():
        from machina.db.connection import db

        await db.connect()
        try:
            yield db
        finally:
            await db.close()


async def _read_status_summary() -> dict[str, Any]:
    """Read-only status payload (same services path as `machina status`)."""
    async with _db_section():
        from machina import services

        return await services.status_summary()


async def _create_session_row(title: str | None) -> tuple[str, str | None]:
    """Create an empty session; returns (session_id, resolved title)."""
    async with _db_section() as db:
        try:
            await db.initialize_runtime_schema()
        except Exception:
            # N5: `machina init` creates the deployment schema, but the
            # additive feature migrations (growth/delegation/image_context/
            # soulspec) are re-applied by every transport entry point (CLI
            # chat, gateway, API runtime, growth CLI) so an upgraded
            # installation gains them before use. Best effort here too, never
            # fatal — the same hygiene as `machina chat`.
            pass
        from machina.core.session import session_manager

        session = await session_manager.create(title=title)
        return str(session.id), session.title


# ---------------------------------------------------------------------------
# machina status
# ---------------------------------------------------------------------------


def _format_status(summary: dict[str, Any]) -> str:
    """Compact status text; the shared subset comes from ``services``.

    ``services.format_status_lines`` is the one source of truth for the
    rows the CLI also renders; it is imported lazily so the shim does not
    pull the agent runtime at startup. This surface keeps its terse
    lowercase styling and its own count/model/permission extras.
    """
    from machina import services

    shared = {label.lower(): value for label, value in services.format_status_lines(summary)}

    def value(key: str, default: object = "unknown") -> object:
        found = summary.get(key, default)
        return default if found is None else found

    tools = summary.get("tools") or []
    lines = [
        "Machina status",
        f"postgres: {value('postgres')}",
        f"sessions: {value('sessions', 0)} | chunks: {value('contextChunks', 0)}"
        f" | memories: {value('memories', 0)} (pending: {value('pendingMemories', 0)})",
        f"tools: {len(tools)} registered",
        f"model: {value('model')} (provider: {value('provider')})",
        f"mode: {shared['mode']} | backend: {shared['backend']}"
        f" | workspace: {shared['workspace'] or '(unset)'}",
        f"chat provider: {shared['chat provider']}",
        f"memory retrieval: {shared['memory retrieval']}",
        f"memory extraction: {shared['memory extraction']}",
        f"document rag: {shared['document rag']}",
        f"reranker: {shared['reranker']}",
        f"automatic compaction: {shared['automatic compaction']}",
        f"sandbox: {shared['sandbox']}",
        f"jobs awaiting approval: {shared['jobs awaiting approval']}",
        f"permission durability: {value('permissionDurability')}",
    ]
    return "\n".join(str(line) for line in lines)


async def machina_status() -> str:
    """Read-only Machina status: database, counts, mode and capability health."""
    try:
        summary = await _read_status_summary()
    except Exception as exc:
        return f"Machina status unavailable ({type(exc).__name__})"
    return _bounded(_format_status(summary), MAX_STATUS_CHARS)


# ---------------------------------------------------------------------------
# machina chat
# ---------------------------------------------------------------------------


def _validate_chat_args(prompt: str, session_id: str | None, mode: str | None) -> str:
    """Return a user-facing error, or an empty string when the call is valid."""
    if not isinstance(prompt, str) or not prompt.strip():
        return "prompt must be a non-empty string"
    if len(prompt) > MAX_PROMPT_CHARS:
        return (
            f"prompt exceeds {MAX_PROMPT_CHARS} characters; send a shorter text prompt "
            "(attachments/images are not supported in phase 1)"
        )
    if session_id is not None:
        if not isinstance(session_id, str):
            return "session_id must be a UUID string or null"
        try:
            uuid.UUID(session_id.strip())
        except (ValueError, AttributeError, TypeError):
            return f"session_id is not a valid UUID: {session_id!r}"
    if mode is not None:
        if not isinstance(mode, str) or mode.strip().lower() not in VALID_MODES:
            return f"mode must be one of {', '.join(VALID_MODES)} or null"
    return ""


def _chat_command(prompt: str, session_id: str | None, mode: str | None) -> list[str]:
    # Same interpreter that runs this server; `-m machina` is the CLI entry.
    # N1: `--` terminates option parsing so a dash-leading prompt ("-i",
    # "--help") is delivered as the chat message instead of being consumed by
    # click (`-i` opened the interactive UI; `--help` printed usage text and
    # exited 0). Every flag stays before the terminator; the prompt is the
    # only positional after it.
    cmd = [sys.executable, "-m", "machina", "chat", "-q"]
    if session_id:
        cmd += ["--session", session_id.strip()]
    if mode:
        cmd += ["--mode", mode.strip().lower()]
    cmd += ["--", prompt]
    return cmd


def _parse_session_id(output: str) -> str | None:
    match = None
    for match in _SESSION_LINE_RE.finditer(output):  # noqa: B007 - keep the last
        pass
    if match is not None:
        return match.group("session_id")
    found = SESSION_ID_PATTERN.findall(output)
    return found[-1] if found else None


def _extract_reply(output: str) -> str:
    kept = [
        line for line in output.splitlines() if not line.strip().startswith(_NOISE_LINE_PREFIXES)
    ]
    return "\n".join(kept).strip()


# CLI pause trailers that must never appear in a paused reply: the ✗
# diagnostic and `request <id>` lines printed by the CLI, the actionable
# suggestion line, and the canonical paused AgentResponse content echo
# ("Waiting for approval.", see core/agent.py) printed once when the stream
# did not already show it. N4: the suggestion line used to leak through.
_PAUSE_NOISE_LINES = frozenset(
    {
        "Resolve the request, then resume the session.",
        "Waiting for approval.",
    }
)


def _strip_pause_lines(text: str) -> str:
    kept = [
        line
        for line in text.splitlines()
        if not line.strip().startswith("✗")
        and not line.strip().startswith("request ")
        and line.strip() not in _PAUSE_NOISE_LINES
    ]
    return "\n".join(kept).strip()


def _parse_pending_approvals(output: str) -> list[dict[str, str]]:
    approvals: list[dict[str, str]] = []
    for line in output.splitlines():
        match = _PENDING_REQUEST_RE.match(line)
        if match:
            item = {"request_id": match.group("request_id")}
            if match.group("detail"):
                item["detail"] = match.group("detail")
            approvals.append(item)
    return approvals


def _json_text(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


async def _join_worker(worker: threading.Thread) -> None:
    """Bounded wait for a chat worker thread to retire (R5-A finding 15)."""
    deadline = time.monotonic() + CANCEL_JOIN_SECONDS
    while worker.is_alive() and time.monotonic() < deadline:
        await asyncio.sleep(0.05)


async def machina_chat(prompt: str, session_id: str | None = None, mode: str | None = None) -> str:
    """Run one headless Machina turn and return a JSON result.

    Spawns `machina chat` with the repo venv interpreter, bounded by a timeout
    and a kill-tree deadline, and hands it the parent-resolved database DSN
    explicitly (R4-081) so both observe the same database. Text only:
    attachments/images are refused in phase 1. Returns JSON text with status
    completed | paused_for_approval | failed | timeout | invalid_request, plus
    session_id, the reply, and — for an approval pause — the pending request
    ids. Never hangs on exit 3.

    R5-A finding 1: a tool-supplied ``mode`` above the server ceiling
    (``_mcp_mode_ceiling``) is refused with a typed error and never spawns,
    so tool arguments cannot mint authority the operator did not configure.
    S2 (security review): an omitted ``mode`` is clamped to the ceiling when
    the ceiling was tightened (``MCP_MAX_MODE``) below the operator mode, so
    the child can never run above the declared ceiling by simply leaving the
    parameter out.
    R5-A finding 15: the child runs on a worker thread bound to a stop
    handle; cancelling this coroutine sets the handle (the runner kills the
    process tree promptly) and the handler joins the worker with a bounded
    wait before the cancellation propagates — no orphaned agent keeps
    modifying files or spending budget after the request is gone.
    """
    invalid = _validate_chat_args(prompt, session_id, mode)
    if invalid:
        return _json_text({"status": "invalid_request", "error": invalid})
    requested = mode.strip().lower() if isinstance(mode, str) else ""
    ceiling = _mcp_mode_ceiling()
    if requested:
        if _MODE_RANK[requested] > _MODE_RANK[ceiling]:
            logger.warning("mcp mode ceiling refused mode=%s ceiling=%s", requested, ceiling)
            return _json_text(
                {
                    "status": "invalid_request",
                    "code": "mode_ceiling_exceeded",
                    "ceiling": ceiling,
                    "error": (
                        f"mode {requested!r} exceeds the server mode ceiling "
                        f"{ceiling!r}; the child was not started and no grant "
                        "was minted"
                    ),
                }
            )
        effective_mode: str | None = mode
    elif _MODE_RANK[ceiling] < _MODE_RANK[_operator_mode()]:
        # S2 (security review): the caller omitted `mode`, so the child would
        # otherwise inherit the operator's configured execution_mode — which can
        # sit ABOVE the declared ceiling when MCP_MAX_MODE tightens below it.
        # Spawn with the ceiling explicit (fail-closed clamp). When the ceiling
        # equals the operator mode the child's own default already matches, so
        # no flag is added and the omitted-mode behavior is unchanged.
        effective_mode = ceiling
    else:
        effective_mode = None
    cmd = _chat_command(prompt, session_id, effective_mode)
    try:
        # R4-081: resolve the parent's own DSN; the child must never guess.
        child_env = _child_database_env()
        child_env["MACHINA_EXECUTION_MODE"] = requested or ceiling
        child_env["MACHINA_MCP_MODE_CEILING"] = ceiling
    except Exception as exc:
        # Fail closed with the class name only (resolution errors never echo
        # values) instead of spawning a child that would pick its own database.
        return _json_text(
            {
                "status": "failed",
                "error": _bounded(f"database resolution failed: {type(exc).__name__}", 600),
            }
        )
    result_box: dict[str, Any] = {}
    cancel_event = threading.Event()

    def _invoke() -> None:
        try:
            result_box["result"] = _run_process(
                cmd,
                timeout=DEFAULT_CHAT_TIMEOUT_SECONDS,
                env_extra=child_env,
                cancel_event=cancel_event,
            )
        except BaseException as exc:  # never lost: re-raised on the loop thread
            result_box["error"] = exc

    worker = threading.Thread(target=_invoke, daemon=True)
    worker.start()
    try:
        while worker.is_alive():
            await asyncio.sleep(0.02)
    except BaseException:
        # Handler cancellation (or loop shutdown): retire the process tree
        # first — cancelling the await alone never stops the worker or the
        # child (finding 15). The runner observes the event promptly and
        # kills the group; the join is bounded and shielded so a second
        # cancel cannot strand the tree, then the original exception
        # propagates so the request still reports cancelled.
        cancel_event.set()
        with contextlib.suppress(BaseException):
            await asyncio.shield(_join_worker(worker))
        raise
    if "error" in result_box:
        raise result_box["error"]
    result = result_box["result"]
    parsed_session = _parse_session_id(result.output)
    reply = _bounded(_extract_reply(result.output), MAX_REPLY_CHARS)
    if result.timed_out:
        return _json_text(
            {
                "status": "timeout",
                "session_id": parsed_session,
                "reply": reply,
                "error": (
                    f"no completion within {int(DEFAULT_CHAT_TIMEOUT_SECONDS)}s; "
                    "the child process tree was killed"
                ),
            }
        )
    if result.returncode == 0:
        return _json_text(
            {
                "status": "completed",
                "session_id": parsed_session,
                "reply": reply,
                "truncated": result.truncated,
            }
        )
    if result.returncode == 3:
        return _json_text(
            {
                "status": "paused_for_approval",
                "session_id": parsed_session,
                "reply": _bounded(_strip_pause_lines(reply), MAX_REPLY_CHARS),
                "pending_approvals": _parse_pending_approvals(result.output),
                "notice": (
                    "Turn queued for approval: resolve the request in Machina, "
                    "then resume the session to continue."
                ),
            }
        )
    if "mode_ceiling_exceeded" in result.output or "mode_ceiling_exceeded" in result.stderr:
        return _json_text(
            {"status": "invalid_request", "code": "mode_ceiling_exceeded", "ceiling": ceiling}
        )
    diagnostic = _redacted_tail(result.output, MAX_ERROR_CHARS)
    if not diagnostic.strip():
        diagnostic = _redacted_tail(result.stderr, MAX_ERROR_CHARS)
    return _json_text(
        {
            "status": "failed",
            "session_id": parsed_session,
            "reply": reply,
            "exit_code": result.returncode,
            "error": _bounded(diagnostic, MAX_ERROR_CHARS),
        }
    )


# ---------------------------------------------------------------------------
# machina session create
# ---------------------------------------------------------------------------


async def machina_session_create(title: str | None = None) -> str:
    """Create an empty Machina session row and return its id as JSON text."""
    if title is not None and not isinstance(title, str):
        return _json_text({"status": "invalid_request", "error": "title must be a string or null"})
    try:
        session_id, resolved_title = await _create_session_row(title)
    except Exception as exc:
        return _json_text(
            {
                "status": "error",
                "error": _bounded(_redacted_tail(f"{type(exc).__name__}: {exc}", 600), 600),
            }
        )
    return _json_text({"status": "created", "session_id": session_id, "title": resolved_title})


# ---------------------------------------------------------------------------
# Server assembly (stdio)
# ---------------------------------------------------------------------------


def build_server() -> MCPServer:
    """Assemble the stdio server with exactly the three phase-1 tools."""
    server = MCPServer(
        name="machina",
        title="Machina",
        description=(
            "Machina agent framework shim (phase 1): read-only status, one-shot chat turns, "
            "session creation."
        ),
        instructions=(
            "Machina is a self-hosted agent framework backed by PostgreSQL. Use "
            "machina_status for a read-only health/count summary. Use machina_chat to run "
            "one headless turn and receive {status, session_id, reply} JSON; it is text-only "
            "(attachments/images are unsupported in phase 1), and a turn that pauses for "
            "approval returns status paused_for_approval instead of hanging. Use "
            "machina_session_create only when an empty session is needed before chatting."
        ),
        version=__version__,
    )
    server.add_tool(machina_status, annotations=ToolAnnotations(read_only_hint=True))
    server.add_tool(machina_chat)
    server.add_tool(machina_session_create)
    return server


def main() -> None:
    """Entry point: serve the three tools over stdio."""
    build_server().run(transport="stdio")
