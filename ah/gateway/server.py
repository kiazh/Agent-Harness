"""JSON-RPC gateway between the terminal UI and the agent core.

The UI (``ui/``) spawns ``python -m ah.gateway`` and exchanges one JSON object
per line over stdin/stdout (JSON-RPC 2.0). Requests get a ``result`` or
``error`` response; streamed output is pushed as notifications of the form
``{"jsonrpc": "2.0", "method": "event", "params": {"type": ..., ...}}``.

The full method and event list is in ``communications/ui-gateway-protocol.md``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import subprocess
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from ah import __version__
from ah.core.config import config
from ah.core.context import context_manager
from ah.core.models import Session
from ah.core.session import session_manager
from ah.db.connection import db
from ah.gateway import features
from ah.gateway.errors import (  # noqa: F401 — re-exported for clients and tests
    DATABASE_UNAVAILABLE,
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    NOT_FOUND,
    OPERATION_FAILED,
    PARSE_ERROR,
    SESSION_NOT_FOUND,
    TURN_IN_PROGRESS,
    UNAUTHORIZED,
    RpcError,
)
from ah.gateway.serializers import history_from_chunks, session_to_dict

__all__ = [
    "Gateway",
    "RpcError",
    "history_from_chunks",
    "session_to_dict",
]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Gateway logging — file + console
# ---------------------------------------------------------------------------
def _setup_gateway_logging() -> None:
    """Configure gateway logging to file and console."""
    log_dir = os.path.join(os.path.expanduser("~"), ".agent-harness", "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "gateway.log")

    # File handler — rotating
    from logging.handlers import RotatingFileHandler
    file_handler = RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    ))
    file_handler.setLevel(logging.DEBUG)

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s"
    ))
    console_handler.setLevel(logging.WARNING)

    # Root logger for ah.gateway
    gw_logger = logging.getLogger("ah.gateway")
    gw_logger.setLevel(logging.DEBUG)
    gw_logger.addHandler(file_handler)
    gw_logger.addHandler(console_handler)

    # Also capture ah.core logs
    core_logger = logging.getLogger("ah.core")
    core_logger.setLevel(logging.DEBUG)
    core_logger.addHandler(file_handler)

    logger.info("Gateway logging initialized: %s", log_file)


_setup_gateway_logging()

PROVIDERS = ("openrouter", "ollama")
MAX_TOOL_RESULT_CHARS = 4000

Writer = Callable[[dict[str, Any]], None]


class StreamingAgent(Protocol):
    def run_stream(self, session_id: uuid.UUID, user_message: str, verbose: bool = ...) -> Any: ...


def _git_branch() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def _default_agent_factory(model: str, provider: str) -> StreamingAgent:
    # Imported lazily: building a provider requires an API key, and importing
    # the agent pulls in the whole tool registry.
    from ah.core.agent import ReActAgent
    from ah.core.provider import get_provider

    llm = get_provider(provider=provider, model=model)
    return ReActAgent(
        provider=llm,
        max_iterations=config.get("max_iterations"),
        agent_id=config.get("agent_id"),
    )


class Gateway:
    """Dispatches JSON-RPC requests and streams agent turns as events."""

    def __init__(
        self,
        write: Writer,
        agent_factory: Callable[[str, str], StreamingAgent] = _default_agent_factory,
        owns_db: bool = True,
    ) -> None:
        self._write = write
        self._agent_factory = agent_factory
        self._owns_db = owns_db
        self.model: str = config.get("model")
        self.provider: str = config.get("provider")
        self.closing = False
        self._db_ready = False
        self._job_runner = None
        self._turns: dict[str, asyncio.Task[None]] = {}
        self._turn_locks: dict[str, asyncio.Lock] = {}
        self._turn_progress: dict[str, dict[str, int]] = {}
        self._config_lock = asyncio.Lock()
        # Token-based auth for the stdio channel. Local UIs set
        # AH_GATEWAY_TOKEN; None means "open" (stdio local use) with a warning.
        self._auth_token: str | None = os.environ.get("AH_GATEWAY_TOKEN")
        if self._auth_token is None:
            logger.warning("AH_GATEWAY_TOKEN not set — gateway accepts unauthenticated requests")
        self._methods: dict[str, Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]] = {
            "initialize": self._initialize,
            "session.create": self._session_create,
            "session.list": self._session_list,
            "session.resume": self._session_resume,
            "usage.get": self._usage_get,
            "prompt.submit": self._prompt_submit,
            "prompt.cancel": self._prompt_cancel,
            "config.set": self._config_set,
            "shutdown": self._shutdown,
        }
        features.register(self)

    def add_method(
        self, name: str, handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
    ) -> None:
        """Register an extra JSON-RPC method (used by ``ah.gateway.features``)."""
        if name in self._methods:
            logger.debug("method already registered, overwriting: %s", name)
        self._methods[name] = handler

    def turn_running(self, session_id: uuid.UUID) -> bool:
        task = self._turns.get(str(session_id))
        return task is not None and not task.done()

    # ─── dispatch ──────────────────────────────────────────────────────────
    async def handle_line(self, line: str) -> None:
        """Parse and answer one inbound JSON-RPC frame."""
        try:
            message = json.loads(line)
        except json.JSONDecodeError as e:
            logger.warning("Parse error: %s", e)
            self._send_error(None, PARSE_ERROR, f"parse error: {e}")
            return

        rid = message.get("id") if isinstance(message, dict) else None
        if (
            not isinstance(message, dict)
            or message.get("jsonrpc") != "2.0"
            or not isinstance(message.get("method"), str)
        ):
            logger.warning("Invalid request: %s", line[:200])
            self._send_error(rid, INVALID_REQUEST, "invalid request")
            return

        params = message.get("params") or {}
        if not isinstance(params, dict):
            logger.warning("Invalid params (not an object) for method %s", message.get("method"))
            self._send_error(rid, INVALID_PARAMS, "params must be an object")
            return

        # Token-based authentication: reject all methods except initialize
        # if the gateway has a token set and the caller hasn't authenticated.
        method = message["method"]
        if self._auth_token is not None and method != "initialize":
            token = params.get("token")
            if not isinstance(token, str) or not secrets.compare_digest(token, self._auth_token):
                logger.warning("Unauthorized attempt: method=%s", method)
                self._send_error(rid, UNAUTHORIZED, "unauthorized")
                return

        handler = self._methods.get(method)
        if handler is None:
            logger.warning("Unknown method: %s", method)
            self._send_error(rid, METHOD_NOT_FOUND, f"unknown method: {method}")
            return

        logger.debug("→ %s (id=%s)", method, rid)
        try:
            result = await handler(params)
        except RpcError as e:
            logger.warning("← %s error %d: %s (id=%s)", method, e.code, e.message, rid)
            self._send_error(rid, e.code, e.message)
            return
        except Exception:  # never let a handler crash the gateway
            logger.exception("Gateway method %s failed", method)
            self._send_error(rid, INTERNAL_ERROR, "internal error")
            return

        logger.debug("← %s ok (id=%s)", method, rid)
        if rid is not None:
            self._write({"jsonrpc": "2.0", "id": rid, "result": result})

    async def close(self) -> None:
        """Cancel running turns, stop the job runner, and release the database pool."""
        if self._job_runner is not None:
            await self._job_runner.stop()
            self._job_runner = None
        tasks = [t for t in self._turns.values() if not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._db_ready and self._owns_db:
            from ah.observability.audit import audit_persistence

            await audit_persistence.stop()
            await db.close()
        self._db_ready = False

    def _start_job_runner(self) -> None:
        """Run scheduled jobs in the background while the gateway is up.

        Disabled with AH_GATEWAY_NO_SCHEDULER=1 (tests drive the runner directly).
        """
        if os.environ.get("AH_GATEWAY_NO_SCHEDULER") or self._job_runner is not None:
            return
        from ah.core.scheduler import JobRunner

        self._job_runner = JobRunner()
        self._job_runner.start()

    def _send_error(self, rid: Any, code: int, message: str) -> None:
        self._write({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})

    # ─── helpers ─────────────────────────────────────────────────────────────
    def require_db(self) -> None:
        if not self._db_ready:
            raise RpcError(DATABASE_UNAVAILABLE, "database not connected; call initialize first")

    async def get_session(self, params: dict[str, Any]) -> Session:
        sid = _parse_session_id(params)
        session = await session_manager.get(sid)
        if session is None:
            raise RpcError(SESSION_NOT_FOUND, f"session {sid} not found")
        return session

    def _set_provider(self, value: Any) -> None:
        if value not in PROVIDERS:
            raise RpcError(INVALID_PARAMS, f"provider must be one of: {', '.join(PROVIDERS)}")
        self.provider = value

    def _set_model(self, value: Any) -> None:
        if not isinstance(value, str) or not value.strip():
            raise RpcError(INVALID_PARAMS, "model must be a non-empty string")
        self.model = value.strip()

    # ─── methods ────────────────────────────────────────────────────────────
    async def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        logger.info("Gateway initialize: model=%s provider=%s", self.model, self.provider)
        from ah.plugins.loader import load_plugins

        load_plugins()
        # Pick up a token set after construction (e.g. tests / late env).
        if self._auth_token is None:
            env_token = os.environ.get("AH_GATEWAY_TOKEN")
            if env_token:
                self._auth_token = env_token
        async with self._config_lock:
            if params.get("model"):
                self._set_model(params["model"])
            if params.get("provider"):
                self._set_provider(params["provider"])
        if not self._db_ready:
            try:
                await db.connect()
            except Exception as e:
                raise RpcError(DATABASE_UNAVAILABLE, f"database unavailable: {e}") from e
            self._db_ready = True
            from ah.core.usage import usage_store

            await usage_store.cleanup_orphaned_reservations()
            from ah.observability.audit import audit_persistence

            audit_persistence.start()
            self._start_job_runner()
        return {
            "version": __version__,
            "model": self.model,
            "provider": self.provider,
            # Redacted to the basename: the full working-directory path can
            # leak usernames / machine layout to unauthenticated callers.
            "cwd": os.path.basename(os.getcwd()),
            "branch": _git_branch(),
        }

    async def _session_create(self, params: dict[str, Any]) -> dict[str, Any]:
        self.require_db()
        title = str(params.get("title") or "").strip()[:80] or "New session"
        async with self._config_lock:
            model, provider = self.model, self.provider
        session = await session_manager.create(
            title=title,
            model=model,
            provider=provider,
            context_budget=config.get("context_budget"),
        )
        return {"session": session_to_dict(session)}

    async def _session_list(self, params: dict[str, Any]) -> dict[str, Any]:
        self.require_db()
        limit = params.get("limit", 20)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
            raise RpcError(INVALID_PARAMS, "limit must be an integer between 1 and 200")
        cursor = params.get("cursor")
        if cursor is None:
            offset = 0
        elif isinstance(cursor, str) and cursor.isascii() and cursor.isdigit():
            offset = int(cursor)
        else:
            raise RpcError(INVALID_PARAMS, "cursor must be a non-negative integer string")
        if offset > 100_000:
            raise RpcError(INVALID_PARAMS, "cursor offset too large (max 100000)")
        sessions = await session_manager.list_sessions(limit=limit + 1, offset=offset)
        result: dict[str, Any] = {
            "sessions": [session_to_dict(s) for s in sessions[:limit]],
        }
        if len(sessions) > limit:
            result["nextCursor"] = str(offset + limit)
        return result

    async def _session_resume(self, params: dict[str, Any]) -> dict[str, Any]:
        self.require_db()
        session = await self.get_session(params)
        chunks = await context_manager.get_chunks(session.id, limit=200)
        return {"session": session_to_dict(session), "history": history_from_chunks(chunks)}

    async def _usage_get(self, params: dict[str, Any]) -> dict[str, Any]:
        self.require_db()
        session = await self.get_session(params)
        agent = params.get("agent") or session.agent_id
        if not isinstance(agent, str) or not 1 <= len(agent) <= 100:
            raise RpcError(INVALID_PARAMS, "agent must be a non-empty string up to 100 characters")
        from ah.core.usage import usage_store

        return await usage_store.summary(session.id, agent)

    async def _prompt_submit(self, params: dict[str, Any]) -> dict[str, Any]:
        self.require_db()
        session = await self.get_session(params)
        text = params.get("text")
        if not isinstance(text, str) or not text.strip():
            raise RpcError(INVALID_PARAMS, "text must be a non-empty string")
        text = text.strip()
        if len(text) > 20_000:
            raise RpcError(INVALID_PARAMS, "text is too long (max 20000 characters)")
        key = str(session.id)
        # Per-session lock: closes the check-then-act race between the
        # running-turn check and task creation for concurrent submitters.
        # setdefault is synchronous, so both racers share the same lock.
        lock = self._turn_locks.setdefault(key, asyncio.Lock())
        async with lock:
            running = self._turns.get(key)
            if running is not None and not running.done():
                raise RpcError(TURN_IN_PROGRESS, "a turn is already running for this session")
            turn_id = uuid.uuid4().hex[:12]
            logger.info("Turn %s started for session %s", turn_id, session.id)
            self._turns[key] = asyncio.create_task(
                self._run_turn_with_timeout(session.id, turn_id, text)
            )
            return {"turnId": turn_id}

    async def _prompt_cancel(self, params: dict[str, Any]) -> dict[str, Any]:
        key = str(_parse_session_id(params))
        task = self._turns.get(key)
        if task is None or task.done():
            return {"cancelled": False}
        task.cancel()
        await asyncio.sleep(0)  # let the cancellation deliver before reporting
        try:
            # Best-effort join so the turn's cleanup runs before we answer.
            # shield() keeps a cancelled turn from cancelling this handler.
            await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
        except (asyncio.CancelledError, TimeoutError, Exception):
            pass
        # Entry removal is left to _run_turn's finally block, which deletes
        # only if the stored task is still the current one (identity check).
        return {"cancelled": True}

    async def _config_set(self, params: dict[str, Any]) -> dict[str, Any]:
        """Set a setting. ``model``/``provider`` apply to this gateway; any other
        non-secret key updates the config (and ``~/.agent-harness/config.yaml``
        when ``persist`` is true). Secrets are only ever read from the environment."""
        from ah.core.config import DEFAULTS, SECRET_KEYS

        key, value = params.get("key"), params.get("value")
        persist = bool(params.get("persist"))
        async with self._config_lock:
            if key == "model":
                self._set_model(value)
                value = self.model
            elif key == "provider":
                self._set_provider(value)
                value = self.provider
            elif key in SECRET_KEYS:
                raise RpcError(INVALID_PARAMS, f"{key} is a secret; use /keys or .env instead")
            elif isinstance(key, str) and key in DEFAULTS:
                # Coerce first so invalid values raise; persist the coerced value.
                value = features.coerce_config_value(key, value)
                config.set(key, value, persist=persist)
            else:
                raise RpcError(INVALID_PARAMS, f"unknown setting: {key}")
            if persist and key in ("model", "provider"):
                config.set(key, value, persist=True)
        result: dict[str, Any] = {"model": self.model, "provider": self.provider, "key": key}
        result["value"] = getattr(self, key) if key in ("model", "provider") else config.get(key)
        return result

    async def _shutdown(self, params: dict[str, Any]) -> dict[str, Any]:
        logger.info("Gateway shutdown requested")
        self.closing = True
        return {}

    # ─── turns ─────────────────────────────────────────────────────────────
    async def _run_turn(self, session_id: uuid.UUID, turn_id: str, text: str) -> None:
        """Run one agent turn, translating agent stream events into protocol events."""
        sid = str(session_id)

        def emit(event_type: str, **fields: Any) -> None:
            self._write(
                {
                    "jsonrpc": "2.0",
                    "method": "event",
                    "params": {"type": event_type, "sessionId": sid, "turnId": turn_id, **fields},
                }
            )

        def complete(final: Any = None, cancelled: bool = False) -> None:
            emit(
                "message.complete",
                text=final.content if final is not None else "",
                # Prefer the agent's final accounting; fall back to the tokens
                # accumulated from token_usage events along the way.
                tokens=(final.tokens_used or tokens) if final is not None else tokens,
                iterations=final.iterations if final is not None else 0,
                toolCalls=len(final.tool_calls) if final is not None else 0,
                cancelled=cancelled,
            )

        emit("message.start")
        open_tools: dict[str, str] = {}  # tool id -> tool name, in start order
        tool_count = 0
        tokens = 0
        final = None
        try:
            agent = self._agent_factory(self.model, self.provider)
            async for event in agent.run_stream(session_id, text, verbose=False):
                if event.type == "text":
                    emit("message.delta", text=event.content)
                elif event.type == "tool_call":
                    tool_count += 1
                    tool_id = f"{turn_id}-{tool_count}"
                    open_tools[tool_id] = event.tool_name
                    self._turn_progress[turn_id] = {
                        "tokens": tokens,
                        "iterations": 0,
                        "toolCalls": tool_count,
                    }
                    emit("tool.start", id=tool_id, name=event.tool_name, args=event.tool_args)
                elif event.type == "tool_result":
                    # Pair by tool-call id when the event carries one;
                    # otherwise fall back to the oldest open call with the
                    # same name (FIFO).
                    result_id = getattr(event, "tool_call_id", None) or getattr(event, "id", None)
                    match_id: str | None = None
                    if result_id is not None and str(result_id) in open_tools:
                        match_id = str(result_id)
                    else:
                        for tid, tname in open_tools.items():
                            if tname == event.tool_name:
                                match_id = tid
                                break
                    if match_id is not None:
                        del open_tools[match_id]
                    result = str(event.tool_result)
                    truncated = len(result) > MAX_TOOL_RESULT_CHARS
                    emit(
                        "tool.complete",
                        id=match_id if match_id else f"{turn_id}-{tool_count}",
                        name=event.tool_name,
                        result=result[:MAX_TOOL_RESULT_CHARS],
                        truncated=truncated,
                        isError=result.startswith("Error:"),
                    )
                elif event.type == "token_usage":
                    tokens += event.tokens_used
                    self._turn_progress[turn_id] = {
                        "tokens": tokens,
                        "iterations": 0,
                        "toolCalls": tool_count,
                    }
                    emit("usage", tokens=tokens)
                elif event.type == "done":
                    final = event.response
                    self._turn_progress[turn_id] = {
                        "tokens": (getattr(final, "tokens_used", 0) or tokens),
                        "iterations": (getattr(final, "iterations", 0) or 0),
                        "toolCalls": len(getattr(final, "tool_calls", None) or []),
                    }
                else:
                    logger.warning("Unknown stream event type: %s", getattr(event, "type", "?"))
        except asyncio.CancelledError:
            complete(cancelled=True)
            return
        except Exception:
            logger.exception("Turn %s failed", turn_id)
            emit("error", message="agent turn failed")
            complete()
            return
        finally:
            if self._turns.get(sid) is asyncio.current_task():
                del self._turns[sid]
        complete(final)

    async def _run_turn_with_timeout(self, session_id: uuid.UUID, turn_id: str, text: str) -> None:
        """Run a turn with a configurable timeout.

        ``wait_for`` cancels the inner turn on timeout, and the turn's own
        ``CancelledError`` handler already emits ``message.complete`` — so a
        timeout must not emit a second ``complete`` when cleanup already ran.
        """
        timeout = config.get("turn_timeout")
        sid = str(session_id)
        current = asyncio.current_task()
        try:
            await asyncio.wait_for(self._run_turn(session_id, turn_id, text), timeout=timeout)
        except TimeoutError:
            # Inner turn already emitted message.complete(cancelled=True) while
            # handling the cancellation: skip the duplicate if it cleaned up.
            if self._turns.get(sid) is not current:
                return
            logger.warning("Turn %s timed out after %ss", turn_id, timeout)
            progress = self._turn_progress.get(
                turn_id, {"tokens": 0, "iterations": 0, "toolCalls": 0}
            )
            self._write(
                {
                    "jsonrpc": "2.0",
                    "method": "event",
                    "params": {
                        "type": "error",
                        "sessionId": sid,
                        "turnId": turn_id,
                        "message": "turn timed out",
                    },
                }
            )
            self._write(
                {
                    "jsonrpc": "2.0",
                    "method": "event",
                    "params": {
                        "type": "message.complete",
                        "sessionId": sid,
                        "turnId": turn_id,
                        "text": "",
                        "tokens": progress.get("tokens", 0),
                        "iterations": progress.get("iterations", 0),
                        "toolCalls": progress.get("toolCalls", 0),
                        "cancelled": False,
                    },
                }
            )
            # Delete only if the stored task is still this timed-out turn.
            if self._turns.get(sid) is current:
                del self._turns[sid]
        except asyncio.CancelledError:
            # prompt.cancel won the race; the inner turn already emitted
            # message.complete(cancelled=True). Preserve that, just tidy up.
            if self._turns.get(sid) is current:
                del self._turns[sid]
            raise
        finally:
            self._turn_progress.pop(turn_id, None)


def _parse_session_id(params: dict[str, Any]) -> uuid.UUID:
    """Parse sessionId, accepting canonical, braced, and URN UUID forms."""
    try:
        return uuid.UUID(str(params.get("sessionId")).strip())
    except (ValueError, TypeError):
        raise RpcError(INVALID_PARAMS, "sessionId must be a UUID") from None
