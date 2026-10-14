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

Process discipline mirrors the repo's terminal-tool patterns
(``machina/tools/terminal.py``, AH-AUDIT-010/023): children run with a minimal
environment allowlist, their own process group, a kill-tree deadline, and
bounded output. The tool registry is deliberately not imported at startup so
the server stays light; status imports Machina pieces lazily inside the call.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import subprocess
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
# start. Mirrors machina/tools/terminal.py (kept local so the MCP server does
# not import the tool registry). No passthrough extension: the unused
# MACHINA_MCP_EXTRA_ENV hook was removed by the 2026-10-14 interop review —
# a new name needs a named host case and a test.
_CHILD_ENV_ALLOWLIST = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "TMPDIR",
        "TMP",
        "TEMP",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TERM",
        "VIRTUAL_ENV",
        "SYSTEMROOT",
        "WINDIR",
        "USERPROFILE",
        "COMSPEC",
        "PATHEXT",
        "APPDATA",
        "LOCALAPPDATA",
    }
)


def _sanitized_env() -> dict[str, str]:
    """Minimal child environment without secrets (never dumps the environment)."""
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        if key.upper() in _CHILD_ENV_ALLOWLIST:
            env[key] = value
    if os.name != "nt" and not env.get("PATH"):
        env["PATH"] = "/usr/bin:/bin"
    # Display-only: keep rich from hard-wrapping the reply at the pipe width
    # and strip color even when TERM claims otherwise.
    env["COLUMNS"] = "400"
    env["NO_COLOR"] = "1"
    return env


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
# Bounded child process runner (terminal.py idiom: process group + kill tree)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProcessResult:
    """Outcome of one bounded child process (stdout and stderr separately)."""

    returncode: int | None
    output: str
    stderr: str
    timed_out: bool
    truncated: bool


def _group_members(pgid: int | None, process: subprocess.Popen[str]) -> bool:
    """True while the launcher or any of its session descendants remains."""
    if os.name == "nt" or pgid is None:
        return process.poll() is None
    process.poll()
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return process.returncode is None
    return True


def _kill_tree(process: subprocess.Popen[str], pgid: int | None = None) -> None:
    """Terminate a whole process tree (AH-AUDIT-023). Best effort, never raises."""
    if os.name == "nt":
        try:
            if process.poll() is not None:
                return
        except Exception:
            return
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                timeout=10,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            try:
                process.kill()
            except OSError:
                pass
        return
    group = pgid
    if group is None:
        try:
            derived = os.getpgid(process.pid)
        except OSError:
            derived = None
        # Never killpg an inherited group this process did not create.
        group = derived if derived == process.pid else None
    if group is not None:
        import signal as _signal

        try:
            os.killpg(group, _signal.SIGKILL)
            return
        except ProcessLookupError:
            return
        except (PermissionError, OSError):
            pass
    try:
        process.kill()
    except OSError:
        pass


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


def _run_process(cmd: list[str], *, timeout: float, cwd: Path | None = None) -> ProcessResult:
    """Run one child in its own process group with a hard kill-tree deadline.

    ``cwd=None`` (default) runs the child in the host launch directory
    captured at call time (see ``_default_child_cwd``); pass ``cwd`` to pin a
    different working directory. stdout (the reply) and stderr (audit/log
    chatter) are read separately, each capped. Prolific stdout stops the
    child; stderr past its cap is drained and discarded so the pipe cannot
    deadlock. On deadline expiry the whole group is killed; this function
    never waits past ``timeout`` plus bounded teardown.
    """
    child_cwd = cwd if cwd is not None else _default_child_cwd()
    popen_kwargs: dict[str, Any] = {}
    if os.name == "nt":
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        popen_kwargs["start_new_session"] = True
    with subprocess.Popen(
        cmd,
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(child_cwd),
        env=_sanitized_env(),
        **popen_kwargs,
    ) as process:
        # R07: retain the group id independently of the launcher so an
        # already-exited leader cannot hide surviving descendants.
        pgid: int | None = None if os.name == "nt" else process.pid
        expired = threading.Event()
        exceeded = threading.Event()

        def stop_on_timeout() -> None:
            if not _group_members(pgid, process):
                return
            expired.set()
            _kill_tree(process, pgid)

        def read_stream(stream: Any, parts: list[str], is_stdout: bool, cap: int) -> None:
            total = 0
            while True:
                chunk = stream.read(8192)
                if not chunk:
                    return
                room = cap - total
                if room <= 0:
                    if is_stdout:
                        exceeded.set()
                        return
                    continue  # stderr past the cap: keep draining, discard
                kept = chunk[:room]
                parts.append(kept)
                total += len(kept)
                if is_stdout and len(chunk) > room:
                    exceeded.set()
                    return

        timer = threading.Timer(timeout, stop_on_timeout)
        timer.daemon = True
        timer.start()
        try:
            if process.stdout is None or process.stderr is None:
                raise RuntimeError("process pipes unavailable")
            output_parts: list[str] = []
            error_parts: list[str] = []
            readers = [
                threading.Thread(
                    target=read_stream,
                    args=(process.stdout, output_parts, True, MAX_OUTPUT_CHARS),
                    daemon=True,
                ),
                threading.Thread(
                    target=read_stream,
                    args=(process.stderr, error_parts, False, MAX_STDERR_CHARS),
                    daemon=True,
                ),
            ]
            for reader in readers:
                reader.start()
            for reader in readers:
                reader.join(timeout=timeout + 5)
            if exceeded.is_set():
                # Prolific stdout: stop the whole group.
                _kill_tree(process, pgid)
            # Completion semantics follow terminal.py: the launcher may exit
            # while descendants keep running, so wait for the whole group —
            # bounded by the deadline timer, which kills what is still alive.
            while not expired.is_set() and _group_members(pgid, process):
                time.sleep(0.02)
            if (expired.is_set() or exceeded.is_set()) and _group_members(pgid, process):
                _kill_tree(process, pgid)
            process.wait()
        finally:
            timer.cancel()
        return ProcessResult(
            returncode=process.returncode,
            output="".join(output_parts),
            stderr="".join(error_parts),
            timed_out=expired.is_set(),
            truncated=exceeded.is_set(),
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
    def value(key: str, default: object = "unknown") -> object:
        found = summary.get(key, default)
        return default if found is None else found

    tools = summary.get("tools") or []
    retrieval = f"memory retrieval: {value('memoryRetrieval')}"
    if summary.get("memoryRetrievalReason"):
        retrieval += f" ({summary['memoryRetrievalReason']})"
    lines = [
        "Machina status",
        f"postgres: {value('postgres')}",
        f"sessions: {value('sessions', 0)} | chunks: {value('contextChunks', 0)}"
        f" | memories: {value('memories', 0)} (pending: {value('pendingMemories', 0)})",
        f"tools: {len(tools)} registered",
        f"model: {value('model')} (provider: {value('provider')})",
        f"mode: {value('mode')} | backend: {value('backend')}"
        f" | workspace: {value('workspace', '') or '(unset)'}",
        f"chat provider: {value('chatProvider')}",
        retrieval,
        f"memory extraction: {value('memoryExtraction')}",
        f"document rag: {value('documentRag')}",
        f"reranker: {value('reranker')}",
        f"auto compaction: {value('autoCompaction')}",
        f"sandbox: {value('sandbox')}",
        f"jobs awaiting approval: {value('jobsAwaitingApproval', 0)}",
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


async def machina_chat(prompt: str, session_id: str | None = None, mode: str | None = None) -> str:
    """Run one headless Machina turn and return a JSON result.

    Spawns `machina chat` with the repo venv interpreter, bounded by a timeout
    and a kill-tree deadline. Text only: attachments/images are refused in
    phase 1. Returns JSON text with status completed | paused_for_approval |
    failed | timeout | invalid_request, plus session_id, the reply, and — for
    an approval pause — the pending request ids. Never hangs on exit 3.
    """
    invalid = _validate_chat_args(prompt, session_id, mode)
    if invalid:
        return _json_text({"status": "invalid_request", "error": invalid})
    cmd = _chat_command(prompt, session_id, mode)
    result = await asyncio.to_thread(_run_process, cmd, timeout=DEFAULT_CHAT_TIMEOUT_SECONDS)
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
