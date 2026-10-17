"""LSP subprocess transport and lifecycle for the integrated workspace (plan §15).

Scope (T7): trusted profile resolution, Content-Length framing, the
initialize/capabilities/initialized handshake, request/response correlation
with cancellation, ordered document-sync writes, bounded stderr retention,
observed process termination, and a per-workspace manager with a bounded
restart policy. This module is self-contained (stdlib plus
``machina.core.child_process``) and owns no UI/RPC surface.

The application's own gateway JSON-RPC is newline-delimited; LSP framing here
is a separate Content-Length implementation and must not be conflated with it.

Security posture:
- argv spawn without a shell; the child environment is the
  ``child_process.sanitized_env`` allowlist (no gateway tokens, no app
  secrets) plus profile-declared ``env_extra`` values;
- one process session per server so the whole process group can be torn down;
- malformed server output fails only that connection and never raises into
  callers;
- server-initiated requests always receive an answer (handler or structured
  refusal), so a server can never hang waiting on this client.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from machina.core.child_process import group_members, kill_tree, sanitized_env

__all__ = [
    "LSP_CONTENT_MODIFIED",
    "LSP_REQUEST_CANCELLED",
    "LSP_REQUEST_FAILED",
    "LSP_SERVER_CANCELLED",
    "STATE_DISABLED",
    "STATE_FAILED",
    "STATE_INITIALIZING",
    "STATE_MISSING",
    "STATE_READY",
    "STATE_STARTING",
    "STATE_STOPPED",
    "STATE_STOPPING",
    "STOP_OUTCOME_CLEANUP_PENDING",
    "STOP_OUTCOME_STOPPED",
    "LspConnection",
    "LspError",
    "LspFraming",
    "LspManager",
    "LspPendingLimitError",
    "LspProfile",
    "LspProtocolError",
    "LspRequestError",
    "LspTimeoutError",
    "LspUnavailableError",
    "resolve_profile",
]

logger = logging.getLogger(__name__)

# --- protocol and lifecycle constants -------------------------------------

STATE_DISABLED = "disabled"
STATE_MISSING = "missing"
STATE_STOPPED = "stopped"
STATE_STARTING = "starting"
STATE_INITIALIZING = "initializing"
STATE_READY = "ready"
STATE_FAILED = "failed"
STATE_STOPPING = "stopping"

STOP_OUTCOME_STOPPED = "stopped"
STOP_OUTCOME_CLEANUP_PENDING = "cleanup_pending"

# JSON-RPC 2.0 reserved codes used by the default server-request refusals.
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INTERNAL_ERROR = -32603

# LSP 3.x error codes, preserved verbatim on LspRequestError.code.
LSP_REQUEST_CANCELLED = -32800
LSP_CONTENT_MODIFIED = -32801
LSP_SERVER_CANCELLED = -32802
LSP_REQUEST_FAILED = -32803

DEFAULT_HEADER_LIMIT = 16 * 1024
DEFAULT_BODY_LIMIT = 8 * 1024 * 1024
DEFAULT_STDERR_LIMIT = 256 * 1024
DEFAULT_PENDING_LIMIT = 32
DEFAULT_OUTBOX_LIMIT = 256


class LspError(Exception):
    """Base class for typed LSP transport failures."""


class LspProtocolError(LspError):
    """Framing or protocol violation from a language server."""


class LspUnavailableError(LspError):
    """The connection cannot serve a request in its current state."""


class LspPendingLimitError(LspError):
    """Structured rejection when the bounded pending-request table is full."""

    def __init__(self, limit: int) -> None:
        super().__init__(f"pending request limit reached ({limit})")
        self.limit = limit


class LspRequestError(LspError):
    """A JSON-RPC error response, a cancellation, or a malformed response.

    ``code`` is the server's JSON-RPC/LSP code verbatim (``None`` when the
    failure is local, e.g. a client-side timeout); ``data`` is passed through
    untouched.
    """

    def __init__(
        self,
        message: str,
        *,
        code: int | None = None,
        data: Any = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.data = data

    @property
    def cancelled(self) -> bool:
        return self.code == LSP_REQUEST_CANCELLED or self.code == LSP_SERVER_CANCELLED

    @property
    def content_modified(self) -> bool:
        return self.code == LSP_CONTENT_MODIFIED


class LspTimeoutError(LspRequestError):
    """A request exceeded its bounded wait; the late response is dropped."""


# --- profiles ---------------------------------------------------------------

_KNOWN_PROFILES: dict[str, tuple[str, str]] = {
    # language id -> (profile id, executable name)
    "python": ("pyright", "pyright-langserver"),
    "typescript": ("typescript-language-server", "typescript-language-server"),
    "javascript": ("typescript-language-server", "typescript-language-server"),
    "typescriptreact": ("typescript-language-server", "typescript-language-server"),
    "javascriptreact": ("typescript-language-server", "typescript-language-server"),
}

# Dev fallbacks provisioned by the T0 tooling commit. They are used only when
# the executable is not explicitly configured and not on PATH; nothing is ever
# installed automatically, and a repository-adjacent binary is a trusted-by-
# configuration dev convenience, not an implicitly trusted one.
_DEV_FALLBACKS: dict[str, str] = {
    "pyright-langserver": "ui/node_modules/.bin/pyright-langserver",
    "typescript-language-server": "ui/node_modules/.bin/typescript-language-server",
}


@dataclass(frozen=True)
class LspProfile:
    """A configured, trusted language-server launch profile."""

    language_id: str
    profile_id: str
    executable: str
    args: Sequence[str] = ()
    env_extra: Mapping[str, str] = field(default_factory=dict)


def _repo_root() -> Path:
    """Repository root discovered from this file's path (``machina/workspace/``)."""
    return Path(__file__).resolve().parents[2]


def _is_executable_file(path: Path) -> bool:
    try:
        return path.is_file() and os.access(path, os.X_OK)
    except OSError:
        return False


def resolve_profile(language: str, explicit_path: str | None = None) -> LspProfile | None:
    """Resolve a launch profile for ``language``; ``None`` means honest missing.

    Order: an explicit path (when it names an executable file) wins; then the
    configured executable name on ``PATH``; then the repo-local dev fallback
    under ``ui/node_modules/.bin``. Nothing is ever installed or executed from
    a shell string.
    """
    known = _KNOWN_PROFILES.get(language.lower())
    if known is None:
        return None
    profile_id, executable_name = known
    executable: str | None = None
    if explicit_path:
        candidate = Path(explicit_path).expanduser()
        if _is_executable_file(candidate):
            executable = str(candidate.resolve())
    if executable is None:
        found = shutil.which(executable_name)
        if found:
            executable = found
    if executable is None:
        fallback = _DEV_FALLBACKS.get(executable_name)
        if fallback:
            candidate = _repo_root() / fallback
            if _is_executable_file(candidate):
                executable = str(candidate)
    if executable is None:
        return None
    return LspProfile(
        language_id=language.lower(),
        profile_id=profile_id,
        executable=executable,
        args=("--stdio",),
    )


# --- framing ----------------------------------------------------------------

_HEADER_LINE_RE = re.compile(rb"^[A-Za-z0-9-]+:[ \t]*[\x20-\x7e]*$")


class LspFraming:
    """Pure Content-Length framing: ``encode`` plus an incremental ``feed``.

    Bounds are constructor-configurable; overruns, duplicate/negative/missing
    ``Content-Length`` headers, header-line garbage and truncated frames raise
    :class:`LspProtocolError`. Once a stream has failed the instance stays
    failed, so a torn connection can never silently resynchronise.
    """

    def __init__(
        self,
        *,
        max_header: int = DEFAULT_HEADER_LIMIT,
        max_body: int = DEFAULT_BODY_LIMIT,
    ) -> None:
        self.max_header = max_header
        self.max_body = max_body
        self._buffer = bytearray()
        self._body_remaining: int | None = None
        self._failed: str | None = None

    @staticmethod
    def encode(message: Mapping[str, Any]) -> bytes:
        """ASCII headers, CRLF separator, UTF-8 JSON body (byte Content-Length).

        ``ensure_ascii=True`` keeps the JSON body pure ASCII: a lone surrogate
        in server-supplied data (e.g. an echoed request method) is escaped
        instead of making ``encode`` raise and leaving a server request
        unanswered.
        """
        body = json.dumps(message, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        return b"Content-Length: %d\r\n\r\n%s" % (len(body), body)

    def feed(self, chunk: bytes) -> list[dict[str, Any]]:
        """Consume one read; return every complete message it completed.

        Handles arbitrary chunk boundaries and several messages per read.
        """
        if self._failed is not None:
            raise LspProtocolError(self._failed)
        self._buffer.extend(chunk)
        frames: list[dict[str, Any]] = []
        try:
            while True:
                if self._body_remaining is not None:
                    if len(self._buffer) < self._body_remaining:
                        break
                    body = bytes(self._buffer[: self._body_remaining])
                    del self._buffer[: self._body_remaining]
                    self._body_remaining = None
                    frames.append(self._decode_body(body))
                    continue
                end = self._buffer.find(b"\r\n\r\n")
                if end == -1:
                    if not self._buffer:
                        break
                    self._validate_pending_header()
                    if len(self._buffer) >= self.max_header + 4:
                        raise LspProtocolError(
                            f"header exceeds {self.max_header} bytes without a terminator"
                        )
                    break
                if end > self.max_header:
                    raise LspProtocolError(f"header exceeds {self.max_header} bytes")
                header = bytes(self._buffer[:end])
                del self._buffer[: end + 4]
                self._body_remaining = self._parse_header(header)
        except LspProtocolError as exc:
            self._failed = str(exc)
            raise
        return frames

    def eof(self) -> None:
        """Signal end of stream; raises when a frame was left truncated."""
        if self._failed is not None:
            raise LspProtocolError(self._failed)
        if self._body_remaining is not None:
            raise LspProtocolError("stream ended mid-body")
        if self._buffer:
            raise LspProtocolError("stream ended mid-header")

    def _validate_pending_header(self) -> None:
        data = self._buffer
        first = data[0]
        if not (0x41 <= first <= 0x5A or 0x61 <= first <= 0x7A):
            raise LspProtocolError("garbage before an LSP header")
        position = 0
        while True:
            line_end = data.find(b"\r\n", position)
            if line_end == -1:
                return
            if not _HEADER_LINE_RE.match(bytes(data[position:line_end])):
                raise LspProtocolError("invalid LSP header line")
            position = line_end + 2

    def _parse_header(self, header: bytes) -> int:
        if not header:
            raise LspProtocolError("empty header block")
        content_length: int | None = None
        for raw_line in header.split(b"\r\n"):
            if not raw_line:
                continue
            if not _HEADER_LINE_RE.match(raw_line):
                raise LspProtocolError("invalid LSP header line")
            name, _, value = raw_line.decode("ascii").partition(":")
            if name.strip().lower() != "content-length":
                continue
            if content_length is not None:
                raise LspProtocolError("duplicate Content-Length header")
            value = value.strip()
            if not value.isdigit():
                raise LspProtocolError("invalid Content-Length value")
            content_length = int(value)
        if content_length is None:
            raise LspProtocolError("missing Content-Length header")
        if content_length > self.max_body:
            raise LspProtocolError(
                f"Content-Length {content_length} exceeds the {self.max_body}-byte body limit"
            )
        return content_length

    def _decode_body(self, body: bytes) -> dict[str, Any]:
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LspProtocolError(f"invalid message body: {exc}") from None
        if not isinstance(value, dict):
            raise LspProtocolError("message body must be a JSON object")
        return value


# --- log sanitizing ---------------------------------------------------------

_UNSAFE_LOG_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"  # CSI sequences
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC sequences
    r"|\x1b[@-Z\\-_]"  # other escapes
    r"|[\x00-\x08\x0b-\x1f\x7f-\x9f]"  # control chars except \n and \t
)


def _sanitize_log(text: str) -> str:
    """Strip terminal escapes and control characters from server logs."""
    return _UNSAFE_LOG_RE.sub("", text)


def _consume_future_exception(future: asyncio.Future[Any]) -> None:
    """Mark a future's exception retrieved; awaiting it still raises."""
    if future.cancelled():
        return
    with contextlib.suppress(BaseException):
        future.exception()


class _AsyncProcessHandle:
    """Duck-type an asyncio child for ``child_process`` group helpers.

    ``kill_tree``/``group_members`` are typed against ``subprocess.Popen``;
    this adapter exposes the three attributes they touch (``pid``, ``poll``,
    ``kill``) for an ``asyncio.subprocess.Process``.
    """

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process

    @property
    def pid(self) -> int:
        return self._process.pid

    def poll(self) -> int | None:
        return self._process.returncode

    def kill(self) -> None:
        with contextlib.suppress(ProcessLookupError, OSError):
            self._process.kill()


# --- connection -------------------------------------------------------------

NotificationCallback = Callable[[str, Any], None]
StateCallback = Callable[["LspConnection", str], None]
ServerRequestHandler = Callable[[str, Any], Any]
ReadyCallback = Callable[["LspConnection"], Any]


class LspConnection:
    """One language-server process, its framing, and its lifecycle.

    States: ``disabled|missing|stopped -> starting -> initializing -> ready``,
    failure -> ``failed``, ``ready -> stopping -> stopped``. ``ready`` is only
    entered after a successful ``initialize`` response and the ``initialized``
    notification; a successful spawn alone never announces readiness.
    """

    def __init__(
        self,
        profile: LspProfile | None,
        root: Path | str,
        *,
        language: str = "",
        enabled: bool = True,
        pending_limit: int = DEFAULT_PENDING_LIMIT,
        outbox_limit: int = DEFAULT_OUTBOX_LIMIT,
        stderr_limit: int = DEFAULT_STDERR_LIMIT,
        max_header: int = DEFAULT_HEADER_LIMIT,
        max_body: int = DEFAULT_BODY_LIMIT,
        initialize_timeout: float = 15.0,
        request_timeout: float = 20.0,
        shutdown_timeout: float = 5.0,
        terminate_timeout: float = 5.0,
        term_timeout: float = 2.0,
        kill_timeout: float = 2.0,
        group_reap_grace: float = 2.0,
        env_passthrough_var: str | None = None,
        client_name: str = "machina-workspace",
    ) -> None:
        self.profile = profile
        self.root = Path(root).resolve()
        self._language = language or (profile.language_id if profile else "")
        self._enabled = enabled
        self._pending_limit = max(1, pending_limit)
        self._outbox_limit = max(1, outbox_limit)
        self._stderr_limit = max(1, stderr_limit)
        self._max_header = max_header
        self._max_body = max_body
        self._initialize_timeout = initialize_timeout
        self._request_timeout = request_timeout
        self._shutdown_timeout = shutdown_timeout
        self._terminate_timeout = terminate_timeout
        self._term_timeout = term_timeout
        self._kill_timeout = kill_timeout
        self._group_reap_grace = group_reap_grace
        self._env_passthrough_var = env_passthrough_var
        self._client_name = client_name

        self.state = (
            STATE_DISABLED if not enabled else (STATE_MISSING if profile is None else STATE_STOPPED)
        )
        self.generation = 0
        self.error = (
            "" if profile is not None or not enabled else "no language server configured or found"
        )
        self.capabilities: dict[str, Any] = {}
        self.server_info: dict[str, Any] = {}
        self.cleanup_pending = False
        self.last_returncode: int | None = None
        self.last_failure: LspError | None = None

        self._process: asyncio.subprocess.Process | None = None
        self._pgid: int | None = None
        self._stdin: asyncio.StreamWriter | None = None
        self._stdout: asyncio.StreamReader | None = None
        self._stderr: asyncio.StreamReader | None = None
        self._framing = LspFraming(max_header=max_header, max_body=max_body)

        self._tasks: set[asyncio.Task[Any]] = set()
        self._writer_task: asyncio.Task[Any] | None = None
        self._reader_task: asyncio.Task[Any] | None = None
        self._stderr_task: asyncio.Task[Any] | None = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._request_id_counter = 0
        self._outbox: deque[tuple[bytes, asyncio.Future[None]]] = deque()
        self._write_event = asyncio.Event()
        self._ready_event = asyncio.Event()
        self._stop_lock = asyncio.Lock()
        self._closing = False

        self._stderr_ring = bytearray()
        self._stderr_event = asyncio.Event()

        self._state_callbacks: list[StateCallback] = []
        self._notification_callbacks: list[NotificationCallback] = []
        self._server_request_handler: ServerRequestHandler | None = None

    # -- introspection -------------------------------------------------------

    @property
    def profile_id(self) -> str:
        return self.profile.profile_id if self.profile is not None else f"missing-{self._language}"

    @property
    def language_id(self) -> str:
        return self.profile.language_id if self.profile is not None else self._language

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def stderr_size(self) -> int:
        """Bytes currently retained by the bounded stderr ring."""
        return len(self._stderr_ring)

    def stderr_text(self) -> str:
        """Sanitized stderr/log tail (terminal escapes and control chars removed)."""
        return _sanitize_log(bytes(self._stderr_ring).decode("utf-8", errors="replace"))

    async def wait_for_stderr(self, needle: str, timeout: float = 5.0) -> bool:
        """Await ``needle`` appearing in the sanitized stderr/log ring."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            if needle in self.stderr_text():
                return True
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            self._stderr_event.clear()
            if needle in self.stderr_text():
                return True
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stderr_event.wait(), remaining)

    # -- callbacks -----------------------------------------------------------

    def on_state_change(self, callback: StateCallback) -> None:
        """Register ``callback(connection, state)`` for lifecycle transitions."""
        self._state_callbacks.append(callback)

    def on_notification(self, callback: NotificationCallback) -> None:
        """Register ``callback(method, params)`` for server notifications."""
        self._notification_callbacks.append(callback)

    def on_server_request(self, handler: ServerRequestHandler | None) -> None:
        """Register ``handler(method, params) -> (result, error)``.

        The handler may be an async function. Returning ``None`` (or anything
        that is not a ``(result, error)`` pair) delegates to the structured
        default refusals. An injected handler that raises produces a JSON-RPC
        internal error instead of stalling the server.
        """
        self._server_request_handler = handler

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Spawn the server and complete the initialize handshake."""
        if not self._enabled:
            raise LspUnavailableError("language server support is disabled")
        if self.profile is None:
            self._set_state(STATE_MISSING, "no language server configured or found")
            raise LspUnavailableError(self.error or "language server is missing")
        if self.state in (STATE_STARTING, STATE_INITIALIZING):
            await self.wait_ready()
            return
        if self.state == STATE_READY:
            return
        if self.state == STATE_STOPPING:
            raise LspUnavailableError("language server is stopping; retry after stop completes")

        await self._reset_transport()
        self._closing = False
        self.cleanup_pending = False
        self.capabilities = {}
        self.server_info = {}
        self.generation += 1
        self._set_state(STATE_STARTING)
        try:
            await self._spawn()
        except LspError as exc:
            self._set_state(STATE_FAILED, str(exc))
            raise
        self._set_state(STATE_INITIALIZING)
        try:
            await self._initialize()
        except asyncio.CancelledError:
            await self._abort_start("start was cancelled")
            raise
        except LspError as exc:
            await self._abort_start(str(exc), exc)
            raise
        except Exception as exc:  # pragma: no cover - defensive containment
            await self._abort_start(f"unexpected initialisation failure: {type(exc).__name__}")
            raise LspUnavailableError(self.error) from exc
        if self.state == STATE_FAILED:
            # The reader can fail this connection between the initialize
            # response and this point (e.g. malformed output right after the
            # handshake); never promote a failed connection to ready.
            raise LspUnavailableError(self.error or "language server failed during initialization")
        self._set_state(STATE_READY)

    async def wait_ready(self, timeout: float | None = None) -> None:
        """Await the in-flight start; raises typed errors on failure/timeout."""
        if self.state == STATE_READY:
            return
        if self.state not in (STATE_STARTING, STATE_INITIALIZING):
            raise LspUnavailableError(self.error or f"language server is not ready ({self.state})")
        limit = timeout if timeout is not None else self._initialize_timeout + 5.0
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._ready_event.wait(), limit)
        if self.state != STATE_READY:
            raise LspUnavailableError(self.error or f"language server is not ready ({self.state})")

    async def stop(self) -> str:
        """Graceful shutdown then observed escalation; never claims stopped early."""
        if self.state in (STATE_DISABLED, STATE_MISSING):
            return STOP_OUTCOME_STOPPED
        if self.state == STATE_STOPPED and not self.cleanup_pending:
            return STOP_OUTCOME_STOPPED
        async with self._stop_lock:
            return await self._stop_locked()

    async def observe_termination(self, timeout: float = 5.0) -> str:
        """Re-observe a ``cleanup_pending`` process and finalize when reaped."""
        if self.state in (STATE_DISABLED, STATE_MISSING):
            return STOP_OUTCOME_STOPPED
        process = self._process
        if process is None or process.returncode is not None:
            if self.state == STATE_STOPPED and not self.cleanup_pending:
                return STOP_OUTCOME_STOPPED
            return await self._finalize_stop()
        if await self._observe(process, timeout):
            return await self._finalize_stop()
        self.cleanup_pending = True
        return STOP_OUTCOME_CLEANUP_PENDING

    async def _stop_locked(self) -> str:
        try:
            return await self._stop_locked_body()
        except asyncio.CancelledError:
            # A cancelled stop must not leave the process group behind: the
            # synchronous sweep is the last chance before the task unwinds.
            self.force_kill()
            raise
        except Exception:
            self.force_kill()
            raise

    async def _stop_locked_body(self) -> str:
        failed = self.state == STATE_FAILED
        if self.state != STATE_STOPPING:
            self._set_state(STATE_STOPPING)
        self._reject_pending("language server is stopping")
        self._drain_outbox_with_error("language server is stopping")
        process = self._process
        if process is not None and process.returncode is None:
            if not failed:
                await self._graceful_shutdown()
                if await self._observe(process, self._terminate_timeout):
                    return await self._finalize_stop()
            # A failed transport cannot complete a protocol shutdown, and an
            # unresponsive server ignored it: escalate to the group signal.
            self._signal_group(signal.SIGTERM)
            if await self._observe(process, self._term_timeout):
                return await self._finalize_stop()
            kill_tree(self._process_handle(), self._pgid)
            if self._kill_timeout > 0:
                if await self._observe(process, self._kill_timeout):
                    return await self._finalize_stop()
            elif process.returncode is not None:
                return await self._finalize_stop()
            # A signal was sent but the exit was not observed: say so honestly.
            self.cleanup_pending = True
            return STOP_OUTCOME_CLEANUP_PENDING
        return await self._finalize_stop()

    async def _graceful_shutdown(self) -> None:
        with contextlib.suppress(LspError):
            await self._request_core("shutdown", None, self._shutdown_timeout)
        with contextlib.suppress(LspError):
            await self._send({"jsonrpc": "2.0", "method": "exit"})
        self._closing = True

    async def _finalize_stop(self) -> str:
        process = self._process
        if process is not None:
            self.last_returncode = process.returncode
        # A leader that exited can still leave group descendants (wrapper
        # scripts, node helpers). Sweep the retained group, then give it a
        # bounded grace window to reap: launchers like
        # typescript-language-server exit before their tsserver child, so an
        # immediate membership check would report cleanup_pending on every
        # ordinary stop. Claiming a clean stop still requires observed
        # emptiness within the window; beyond it, stay honest.
        if self._group_has_members():
            kill_tree(self._process_handle(), self._pgid)
            if self._group_reap_grace > 0:
                deadline = time.monotonic() + self._group_reap_grace
                while self._group_has_members() and time.monotonic() < deadline:
                    await asyncio.sleep(0.05)
            if self._group_has_members():
                self.cleanup_pending = True
                self._set_state(STATE_STOPPING, "process group still has members after SIGKILL")
                return STOP_OUTCOME_CLEANUP_PENDING
        self.cleanup_pending = False
        self._closing = True
        await self._reset_transport()
        self.capabilities = {}
        self.server_info = {}
        self._set_state(STATE_STOPPED)
        return STOP_OUTCOME_STOPPED

    async def _abort_start(self, reason: str, failure: LspError | None = None) -> None:
        self.error = reason
        self.last_failure = failure if failure is not None else LspUnavailableError(reason)
        self._reject_pending(reason)
        self._drain_outbox_with_error(reason)
        await self._reset_transport()
        self._set_state(STATE_FAILED, reason)

    # -- spawning ------------------------------------------------------------

    async def _spawn(self) -> None:
        profile = self.profile
        assert profile is not None
        env = sanitized_env(self._env_passthrough_var)
        if not env.get("PATH"):
            env["PATH"] = os.defpath
        env.update({str(key): str(value) for key, value in profile.env_extra.items()})
        argv = [str(profile.executable), *profile.args]
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(self.root),
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **kwargs,
            )
        except (OSError, ValueError) as exc:
            raise LspUnavailableError(f"failed to spawn {profile.executable!r}: {exc}") from exc
        if process.stdin is None or process.stdout is None or process.stderr is None:
            with contextlib.suppress(Exception):
                process.kill()
            raise LspUnavailableError("language server pipes were not created")
        self._process = process
        self._pgid = None if os.name == "nt" else process.pid
        self._stdin, self._stdout, self._stderr = process.stdin, process.stdout, process.stderr
        self._writer_task = asyncio.create_task(
            self._writer_loop(), name=f"lsp-writer:{self.profile_id}"
        )
        self._reader_task = asyncio.create_task(
            self._reader_loop(), name=f"lsp-reader:{self.profile_id}"
        )
        self._stderr_task = asyncio.create_task(
            self._stderr_loop(), name=f"lsp-stderr:{self.profile_id}"
        )
        self._tasks.update({self._writer_task, self._reader_task, self._stderr_task})

    async def _initialize(self) -> None:
        root_uri = self.root.as_uri()
        params: dict[str, Any] = {
            "processId": os.getpid(),
            "clientInfo": {"name": self._client_name},
            "rootUri": root_uri,
            "workspaceFolders": [{"uri": root_uri, "name": self.root.name or "workspace"}],
            "capabilities": _client_capabilities(),
            "trace": "off",
        }
        result = await self._request_core("initialize", params, self._initialize_timeout)
        if not isinstance(result, dict):
            raise LspProtocolError("initialize result was not an object")
        capabilities = result.get("capabilities")
        if not isinstance(capabilities, dict):
            raise LspProtocolError("initialize result carried no capabilities object")
        self.capabilities = capabilities
        server_info = result.get("serverInfo")
        self.server_info = server_info if isinstance(server_info, dict) else {}
        await self._send({"jsonrpc": "2.0", "method": "initialized", "params": {}})

    def _save_includes_text(self) -> bool:
        sync = self.capabilities.get("textDocumentSync")
        if isinstance(sync, Mapping):
            save = sync.get("save")
            if isinstance(save, Mapping):
                return bool(save.get("includeText"))
        return False

    # -- requests ------------------------------------------------------------

    def allocate_request_id(self) -> int:
        self._request_id_counter += 1
        return self._request_id_counter

    async def request(
        self,
        method: str,
        params: Any = None,
        timeout: float | None = None,
        *,
        request_id: int | None = None,
    ) -> Any:
        """Send one request; resolves to the result or raises typed errors."""
        self._require_ready(method)
        return await self._request_core(
            method,
            params,
            self._request_timeout if timeout is None else timeout,
            request_id=request_id,
        )

    async def _request_core(
        self,
        method: str,
        params: Any,
        timeout: float,
        *,
        request_id: int | None = None,
    ) -> Any:
        if not self._transport_active():
            raise LspUnavailableError(f"language server is not running ({self.state})")
        if len(self._pending) >= self._pending_limit:
            raise LspPendingLimitError(self._pending_limit)
        if request_id is None:
            request_id = self.allocate_request_id()
        elif request_id in self._pending:
            raise ValueError(f"request id {request_id} is already pending")
        future = asyncio.get_running_loop().create_future()
        future.add_done_callback(_consume_future_exception)
        self._pending[request_id] = future
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        try:
            # The timeout covers the ordered write (which can block on a
            # language server that stops draining stdin) as well as the
            # response wait. Starting it after _send() let a stalled write
            # outlive every configured request timeout.
            async with asyncio.timeout(timeout):
                await self._send(message)
                return await future
        except TimeoutError:
            if self._pending.pop(request_id, None) is not None:
                self._send_cancel(request_id)
            raise LspTimeoutError(f"LSP request {method!r} timed out after {timeout:g}s") from None
        except asyncio.CancelledError:
            if self._pending.pop(request_id, None) is not None:
                self._send_cancel(request_id)
            raise
        except LspError:
            self._pending.pop(request_id, None)
            raise

    def cancel(self, request_id: int) -> bool:
        """Cancel a pending request: drop the waiter and send ``$/cancelRequest``.

        A late response for the cancelled id is dropped silently later.
        """
        future = self._pending.pop(request_id, None)
        if future is None:
            return False
        if not future.done():
            future.set_exception(
                LspRequestError("request cancelled by client", code=LSP_REQUEST_CANCELLED)
            )
        self._send_cancel(request_id)
        return True

    def _send_cancel(self, request_id: int) -> None:
        self._enqueue_silent(
            {"jsonrpc": "2.0", "method": "$/cancelRequest", "params": {"id": request_id}}
        )

    # -- document sync -------------------------------------------------------

    def did_open(self, uri: str, language_id: str, version: int, text: str) -> None:
        self._require_ready("textDocument/didOpen")
        self._enqueue_frame(
            {
                "jsonrpc": "2.0",
                "method": "textDocument/didOpen",
                "params": {
                    "textDocument": {
                        "uri": uri,
                        "languageId": language_id,
                        "version": version,
                        "text": text,
                    }
                },
            }
        )

    def did_change(self, uri: str, version: int, changes: Sequence[Mapping[str, Any]]) -> None:
        self._require_ready("textDocument/didChange")
        self._enqueue_frame(
            {
                "jsonrpc": "2.0",
                "method": "textDocument/didChange",
                "params": {
                    "textDocument": {"uri": uri, "version": version},
                    "contentChanges": list(changes),
                },
            }
        )

    def did_save(self, uri: str, text: str | None = None) -> None:
        """Send didSave; ``text`` is included only when the server asked for it."""
        self._require_ready("textDocument/didSave")
        params: dict[str, Any] = {"textDocument": {"uri": uri}}
        if text is not None and self._save_includes_text():
            params["text"] = text
        self._enqueue_frame({"jsonrpc": "2.0", "method": "textDocument/didSave", "params": params})

    def did_close(self, uri: str) -> None:
        self._require_ready("textDocument/didClose")
        self._enqueue_frame(
            {
                "jsonrpc": "2.0",
                "method": "textDocument/didClose",
                "params": {"textDocument": {"uri": uri}},
            }
        )

    # -- ordered write queue -------------------------------------------------

    def _require_ready(self, what: str) -> None:
        if self.state != STATE_READY:
            raise LspUnavailableError(f"cannot send {what}: language server state is {self.state}")

    def _transport_active(self) -> bool:
        return (
            self._writer_task is not None
            and not self._writer_task.done()
            and self._process is not None
            and self._process.returncode is None
        )

    def _enqueue_frame(self, message: Mapping[str, Any]) -> asyncio.Future[None]:
        """Append one frame to the single ordered writer queue.

        The returned future resolves once the frame has been written and
        drained; ``request`` awaits it so it can never overtake an earlier
        didChange.
        """
        if self._closing or not self._transport_active():
            raise LspUnavailableError(f"language server is not accepting writes ({self.state})")
        if len(self._outbox) >= self._outbox_limit:
            raise LspUnavailableError(f"language server write queue is full ({self._outbox_limit})")
        future = asyncio.get_running_loop().create_future()
        future.add_done_callback(_consume_future_exception)
        self._outbox.append((LspFraming.encode(message), future))
        self._write_event.set()
        return future

    def _enqueue_silent(self, message: Mapping[str, Any]) -> None:
        with contextlib.suppress(LspError):
            self._enqueue_frame(message)

    async def _send(self, message: Mapping[str, Any]) -> None:
        await self._enqueue_frame(message)

    async def _writer_loop(self) -> None:
        stdin = self._stdin
        assert stdin is not None
        try:
            while True:
                if self._outbox:
                    data, future = self._outbox.popleft()
                    try:
                        stdin.write(data)
                        await stdin.drain()
                    except (BrokenPipeError, ConnectionResetError, OSError) as exc:
                        if not future.done():
                            future.set_exception(
                                LspUnavailableError(f"language server stdin closed: {exc}")
                            )
                        raise
                    if not future.done():
                        future.set_result(None)
                    continue
                if self._closing:
                    return
                self._write_event.clear()
                await self._write_event.wait()
        except asyncio.CancelledError:
            self._drain_outbox_with_error("language server write queue was cancelled")
            raise
        except Exception as exc:  # pragma: no cover - defensive containment
            self._drain_outbox_with_error("language server write queue failed")
            self._fail_connection(f"write to language server failed: {type(exc).__name__}")

    # -- reader loops --------------------------------------------------------

    async def _reader_loop(self) -> None:
        stdout = self._stdout
        assert stdout is not None
        framing = self._framing
        try:
            while True:
                chunk = await stdout.read(65536)
                if not chunk:
                    break
                try:
                    frames = framing.feed(chunk)
                except LspProtocolError as exc:
                    self._fail_connection(f"malformed language server output: {exc}", exc)
                    return
                for frame in frames:
                    try:
                        self._dispatch_frame(frame)
                    except Exception as exc:  # pragma: no cover - defensive containment
                        self._fail_connection(
                            f"language server message handling failed: {type(exc).__name__}"
                        )
                        return
        except asyncio.CancelledError:
            raise
        except (OSError, ValueError) as exc:
            self._fail_connection(f"language server pipe error: {exc}")
            return
        if self._closing:
            return
        reason = "language server stdout closed"
        try:
            framing.eof()
        except LspProtocolError as exc:
            reason = f"language server exited mid-message: {exc}"
        else:
            returncode = self._process.returncode if self._process is not None else None
            reason = f"language server exited (returncode={returncode})"
        self._fail_connection(reason)

    async def _stderr_loop(self) -> None:
        stderr = self._stderr
        assert stderr is not None
        try:
            while True:
                chunk = await stderr.read(32768)
                if not chunk:
                    return
                self._append_stderr(chunk)
        except asyncio.CancelledError:
            raise
        except (OSError, ValueError):
            return

    def _append_stderr(self, chunk: bytes) -> None:
        self._stderr_ring.extend(chunk)
        overflow = len(self._stderr_ring) - self._stderr_limit
        if overflow > 0:
            del self._stderr_ring[:overflow]
        self._stderr_event.set()

    def _append_log_message(self, method: str, params: Any) -> None:
        if not isinstance(params, Mapping):
            return
        message = params.get("message")
        if not message:
            return
        self._append_stderr(f"[{method}] {message}\n".encode("utf-8", errors="replace"))

    # -- frame dispatch ------------------------------------------------------

    def _dispatch_frame(self, frame: dict[str, Any]) -> None:
        method = frame.get("method")
        if isinstance(method, str):
            if "id" in frame:
                self._schedule_server_request(frame)
            else:
                self._handle_notification(method, frame.get("params"))
            return
        if "id" in frame:
            self._handle_response(frame)
            return
        logger.debug("ignoring LSP frame without method or id: %r", sorted(frame))

    def _handle_response(self, frame: dict[str, Any]) -> None:
        request_id = frame.get("id")
        if not isinstance(request_id, int):
            return
        future = self._pending.pop(request_id, None)
        if future is None or future.done():
            # Late response after a cancellation (or a duplicate): drop it.
            return
        error = frame.get("error")
        if isinstance(error, Mapping):
            code = error.get("code")
            message = error.get("message")
            future.set_exception(
                LspRequestError(
                    str(message) if isinstance(message, str) else "language server error",
                    code=code if isinstance(code, int) else None,
                    data=error.get("data"),
                )
            )
            return
        if "result" in frame:
            future.set_result(frame["result"])
            return
        future.set_exception(LspRequestError("malformed response (no result or error)"))

    def _handle_notification(self, method: str, params: Any) -> None:
        if method in ("window/logMessage", "window/showMessage"):
            self._append_log_message(method, params)
        for callback in list(self._notification_callbacks):
            try:
                callback(method, params)
            except Exception:  # pragma: no cover - observer containment
                logger.debug("LSP notification callback failed", exc_info=True)

    def _schedule_server_request(self, frame: dict[str, Any]) -> None:
        task = asyncio.create_task(
            self._answer_server_request(frame.get("id"), str(frame["method"]), frame.get("params")),
            name=f"lsp-server-request:{self.profile_id}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _answer_server_request(self, request_id: Any, method: str, params: Any) -> None:
        result: Any = None
        error: dict[str, Any] | None = None
        handled = False
        handler = self._server_request_handler
        if handler is not None:
            try:
                outcome = handler(method, params)
                if inspect.isawaitable(outcome):
                    outcome = await outcome
                if isinstance(outcome, tuple) and len(outcome) == 2:
                    result, error = outcome
                    handled = True
            except Exception as exc:  # containment: never leave the server waiting
                error = {
                    "code": JSONRPC_INTERNAL_ERROR,
                    "message": f"server request handler failed: {type(exc).__name__}",
                }
                handled = True
        if not handled:
            if method == "workspace/applyEdit":
                result = {
                    "applied": False,
                    "failureReason": "workspace edits are not applied by this client",
                }
            elif method == "workspace/configuration":
                result = []
            elif method == "window/showMessageRequest":
                self._append_log_message(method, params)
                result = None
            else:
                error = {
                    "code": JSONRPC_METHOD_NOT_FOUND,
                    "message": f"unsupported server request: {method}",
                }
        response: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
        if error is not None:
            response["error"] = error
        else:
            response["result"] = result
        try:
            await self._send(response)
        except LspError:
            pass
        except UnicodeError:
            # Never leave a server request unanswered: fall back to a
            # plain-ASCII internal error when a supplied value cannot be
            # encoded even with ASCII escaping.
            fallback: dict[str, Any] = {
                "jsonrpc": "2.0",
                "id": request_id if isinstance(request_id, int) else None,
                "error": {
                    "code": JSONRPC_INTERNAL_ERROR,
                    "message": "server request response could not be encoded",
                },
            }
            with contextlib.suppress(LspError, UnicodeError):
                await self._send(fallback)

    # -- failure handling ----------------------------------------------------

    def _fail_connection(self, reason: str, failure: LspError | None = None) -> None:
        """Fail this connection only; caller-facing state, never a raise."""
        if self.state in (
            STATE_STOPPING,
            STATE_STOPPED,
            STATE_FAILED,
            STATE_DISABLED,
            STATE_MISSING,
        ):
            return
        self.error = reason
        self.last_failure = failure if failure is not None else LspUnavailableError(reason)
        self._reject_pending(reason)
        self._drain_outbox_with_error(reason)
        current = asyncio.current_task()
        for task in list(self._tasks):
            if task is not current and not task.done():
                task.cancel()
        self._set_state(STATE_FAILED, reason)

    def _reject_pending(self, reason: str) -> None:
        pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(LspUnavailableError(reason))

    def _drain_outbox_with_error(self, reason: str) -> None:
        outbox, self._outbox = self._outbox, deque()
        for _, future in outbox:
            if not future.done():
                future.set_exception(LspUnavailableError(reason))

    def _set_state(self, state: str, detail: str = "") -> None:
        self.state = state
        if detail:
            self.error = detail
        if state in (STATE_READY, STATE_FAILED):
            self._ready_event.set()
        elif state == STATE_STARTING:
            self._ready_event.clear()
        for callback in list(self._state_callbacks):
            try:
                callback(self, state)
            except Exception:  # pragma: no cover - observer containment
                logger.debug("LSP state callback failed", exc_info=True)

    # -- process observation -------------------------------------------------

    def _process_handle(self) -> Any:
        process = self._process
        return _AsyncProcessHandle(process) if process is not None else None

    def _group_has_members(self) -> bool:
        if os.name == "nt" or self._pgid is None or self._process is None:
            return False
        try:
            return group_members(self._pgid, self._process_handle())
        except Exception:  # pragma: no cover - defensive containment
            return False

    def force_kill(self) -> None:
        """Best-effort synchronous SIGKILL sweep for a timed-out/shutdown stop.

        Called after the bounded asynchronous stop could not observe an exit
        (or when the owning task is cancelled during shutdown). It never
        raises: a failure to signal is logged, and ``cleanup_pending`` stays
        truthful until the process is actually reaped.
        """
        process = self._process
        if process is None:
            return
        try:
            kill_tree(self._process_handle(), self._pgid)
        except Exception:
            logger.warning("language-server force-kill sweep failed", exc_info=True)
        if process.returncode is None:
            try:
                process.kill()
            except (ProcessLookupError, OSError):
                pass
            except Exception:
                logger.warning("language-server process kill failed", exc_info=True)
        try:
            self.cleanup_pending = process.returncode is None or self._group_has_members()
        except Exception:
            # Cannot observe the group: keep claiming cleanup pending rather
            # than asserting a clean stop that was not verified.
            self.cleanup_pending = True

    def _signal_group(self, sig: int) -> None:
        process = self._process
        if process is None or process.returncode is not None:
            return
        if os.name == "nt":
            with contextlib.suppress(ProcessLookupError, OSError):
                process.terminate()
            return
        if self._pgid is None:
            return
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(self._pgid, sig)

    async def _observe(self, process: asyncio.subprocess.Process, timeout: float) -> bool:
        if process.returncode is not None:
            return True
        if timeout <= 0:
            return process.returncode is not None
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(process.wait(), timeout)
        return process.returncode is not None

    async def _reset_transport(self) -> None:
        current = asyncio.current_task()
        self._closing = True
        self._reject_pending("language server transport is restarting")
        self._drain_outbox_with_error("language server transport is restarting")
        tasks = [task for task in self._tasks if task is not current and not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks = {task for task in self._tasks if task is not current and not task.done()}
        process = self._process
        if process is not None:
            if process.returncode is None:
                kill_tree(self._process_handle(), self._pgid)
                await self._observe(process, max(self._kill_timeout, 0.5))
            self.last_returncode = process.returncode
            stdin = process.stdin
            if stdin is not None:
                with contextlib.suppress(Exception):
                    if not stdin.is_closing():
                        stdin.close()
        self._process = None
        self._pgid = None
        self._stdin = None
        self._stdout = None
        self._stderr = None
        self._writer_task = None
        self._reader_task = None
        self._stderr_task = None
        self._write_event.clear()
        self._framing = LspFraming(max_header=self._max_header, max_body=self._max_body)
        self._ready_event.clear()


def _client_capabilities() -> dict[str, Any]:
    """Conservative client capabilities (plan §15).

    Deliberately absent: ``dynamicRegistration`` (anywhere),
    ``workspace.configuration``, ``workspace.executeCommand``, and any
    ``workspaceEdit`` support claim. ``applyEdit`` is ``False`` because this
    client never applies server-initiated edits (no ``on_server_request``
    handler is registered in production); ``workspace/applyEdit`` is still
    refused structurally as defense-in-depth for servers that ignore the
    advertised capability (invariant I-6: never advertise what we refuse).
    """
    return {
        "general": {"positionEncodings": ["utf-16"]},
        "workspace": {"applyEdit": False},
        "textDocument": {
            "synchronization": {"didSave": True},
            "completion": {"completionItem": {"snippetSupport": False}},
            "hover": {},
            "definition": {},
            "references": {},
            "documentSymbol": {},
            "rename": {"prepareSupport": True},
            "signatureHelp": {},
            "formatting": {},
            "codeAction": {},
            # typescript-language-server gates diagnostics delivery on this
            # capability being present (its diagnosticsSupport flag); omitting
            # it silently suppressed every diagnostic for the TS profile.
            "publishDiagnostics": {"relatedInformation": True},
        },
    }


# --- manager ----------------------------------------------------------------


class LspManager:
    """One shared connection per (workspace root, profile); never per request.

    Automatic restarts are bounded (``max_restarts`` attempts inside
    ``restart_window`` seconds); once the budget is spent the connection stays
    ``failed`` until :meth:`restart` is called explicitly. The ``on_ready``
    hook fires after every successful initialize (initial start and restarts)
    so callers can reopen their documents.
    """

    def __init__(
        self,
        root: Path | str,
        *,
        profiles: Mapping[str, LspProfile] | None = None,
        max_restarts: int = 3,
        restart_window: float = 300.0,
        restart_delay: float = 0.0,
        auto_restart: bool = True,
        **connection_kwargs: Any,
    ) -> None:
        self.root = Path(root).resolve()
        self._profiles = dict(profiles or {})
        self._max_restarts = max(0, max_restarts)
        self._restart_window = max(0.0, restart_window)
        self._restart_delay = max(0.0, restart_delay)
        self._auto_restart_enabled = auto_restart
        self._connection_kwargs = connection_kwargs
        self._connections: dict[tuple[str, str], LspConnection] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._restart_history: dict[tuple[str, str], list[float]] = {}
        self._restart_tasks: dict[tuple[str, str], asyncio.Task[Any]] = {}
        self._ready_callbacks: list[ReadyCallback] = []
        self._closed = False

    def on_ready(self, callback: ReadyCallback) -> None:
        """Register ``callback(connection)`` invoked after every initialize."""
        self._ready_callbacks.append(callback)

    def _resolve(self, language: str, explicit_path: str | None) -> LspProfile | None:
        if explicit_path:
            return resolve_profile(language, explicit_path)
        override = self._profiles.get(language)
        if override is not None:
            return override
        return resolve_profile(language)

    async def ensure_started(
        self, language: str, *, explicit_path: str | None = None
    ) -> LspConnection:
        """Return the ready connection for ``language``, starting it if needed.

        A missing server yields a connection whose state is ``missing`` (the
        honest unavailability signal); transport failures raise typed errors.
        """
        if self._closed:
            raise LspUnavailableError("workspace LSP manager is closed")
        profile = self._resolve(language, explicit_path)
        key = (str(self.root), profile.profile_id if profile is not None else f"missing:{language}")
        connection = self._connections.get(key)
        if connection is None:
            connection = LspConnection(
                profile, self.root, language=language, **self._connection_kwargs
            )
            connection.on_state_change(self._make_watcher(key))
            self._connections[key] = connection
        if connection.state == STATE_READY:
            return connection
        if connection.profile is None:
            return connection
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            if connection.state == STATE_READY:
                return connection
            if connection.state == STATE_STOPPING:
                raise LspUnavailableError("language server is stopping; retry shortly")
            if connection.state == STATE_FAILED:
                if not self._restart_allowed(key):
                    raise LspUnavailableError(
                        f"language server {profile.profile_id!r} requires manual restart: "
                        f"{connection.error}"
                    )
                self._record_restart(key)
            await connection.start()
        return connection

    def status(self) -> list[dict[str, Any]]:
        """JSON-able status per connection (matches the frozen UI contract)."""
        entries: list[dict[str, Any]] = []
        for _, connection in self._connections.items():
            entry: dict[str, Any] = {
                "profile": connection.profile_id,
                "languageId": connection.language_id,
                "state": connection.state,
                "generation": connection.generation,
            }
            if connection.capabilities:
                entry["capabilities"] = connection.capabilities
            detail = connection.error
            if connection.cleanup_pending:
                detail = "cleanup pending: process not reaped"
                if connection.error:
                    detail = f"{detail} ({connection.error})"
            if detail:
                entry["detail"] = detail
            entries.append(entry)
        return entries

    async def restart(self, profile: LspProfile | str) -> LspConnection:
        """Manual restart: stops, resets the restart budget, and re-initializes."""
        if isinstance(profile, str):
            resolved = self._resolve(profile, None)
        else:
            resolved = profile
        if resolved is None:
            raise LspUnavailableError("no language server profile to restart")
        key = (str(self.root), resolved.profile_id)
        connection = self._connections.get(key)
        if connection is None:
            raise LspUnavailableError(f"no connection for profile {resolved.profile_id!r}")
        task = self._restart_tasks.pop(key, None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await connection.stop()
        self._restart_history.pop(key, None)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            if connection.state != STATE_READY:
                await connection.start()
        return connection

    async def stop_all(self) -> None:
        """Stop every owned connection; the manager is closed afterwards."""
        self._closed = True
        for task in list(self._restart_tasks.values()):
            task.cancel()
        try:
            await asyncio.gather(
                *(connection.stop() for connection in self._connections.values()),
                return_exceptions=True,
            )
        except asyncio.CancelledError:
            # The stop itself was cancelled (shutdown/loop teardown): sweep
            # every process group synchronously before unwinding.
            self.force_kill_all()
            raise
        self._restart_tasks.clear()

    def force_kill_all(self) -> None:
        """Best-effort synchronous kill of every owned connection.

        Shutdown escalation for a stop that exceeded its bound (or whose task
        was cancelled). Synchronous by design: it must still run while the
        event loop is being torn down. Never raises.
        """
        for connection in list(self._connections.values()):
            force_kill = getattr(connection, "force_kill", None)
            if not callable(force_kill):
                continue
            try:
                force_kill()
            except Exception:
                logger.warning("language-server force kill failed", exc_info=True)
        for task in list(self._restart_tasks.values()):
            task.cancel()

    # -- restart bookkeeping -------------------------------------------------

    def _restart_allowed(self, key: tuple[str, str]) -> bool:
        if not self._auto_restart_enabled or self._max_restarts <= 0:
            return False
        now = asyncio.get_running_loop().time()
        history = [
            stamp
            for stamp in self._restart_history.get(key, [])
            if now - stamp < self._restart_window
        ]
        self._restart_history[key] = history
        return len(history) < self._max_restarts

    def _record_restart(self, key: tuple[str, str]) -> None:
        now = asyncio.get_running_loop().time()
        history = [
            stamp
            for stamp in self._restart_history.get(key, [])
            if now - stamp < self._restart_window
        ]
        history.append(now)
        self._restart_history[key] = history

    def _make_watcher(self, key: tuple[str, str]) -> StateCallback:
        def watcher(connection: LspConnection, state: str) -> None:
            if state == STATE_READY:
                self._fire_ready(connection)
            elif state == STATE_FAILED:
                self._schedule_auto_restart(key, connection)

        return watcher

    def _fire_ready(self, connection: LspConnection) -> None:
        for callback in list(self._ready_callbacks):
            try:
                outcome = callback(connection)
                if inspect.isawaitable(outcome):
                    task = asyncio.ensure_future(outcome)
                    task.add_done_callback(_consume_future_exception)
            except Exception:  # pragma: no cover - observer containment
                logger.debug("LSP ready callback failed", exc_info=True)

    def _schedule_auto_restart(self, key: tuple[str, str], connection: LspConnection) -> None:
        if self._closed or not self._auto_restart_enabled:
            return
        existing = self._restart_tasks.get(key)
        if existing is not None and not existing.done():
            return
        if not self._restart_allowed(key):
            return
        self._restart_tasks[key] = asyncio.create_task(
            self._auto_restart(key, connection), name=f"lsp-restart:{key[1]}"
        )

    async def _auto_restart(self, key: tuple[str, str], connection: LspConnection) -> None:
        try:
            if self._restart_delay > 0:
                await asyncio.sleep(self._restart_delay)
            lock = self._locks.setdefault(key, asyncio.Lock())
            async with lock:
                if self._closed or connection.state != STATE_FAILED:
                    return
                if not self._restart_allowed(key):
                    return
                self._record_restart(key)
                await connection.start()
        except (LspError, asyncio.CancelledError):
            pass
        except Exception:  # pragma: no cover - defensive containment
            logger.debug("automatic LSP restart failed", exc_info=True)
