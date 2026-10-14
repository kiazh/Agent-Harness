"""JSON-RPC gateway between the terminal UI and the agent core.

The UI (``ui/``) spawns ``python -m machina.gateway`` and exchanges one JSON object
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
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from machina import __version__
from machina.core.background_tasks import drain_background_tasks, join_agent_background_tasks
from machina.core.config import config
from machina.core.context import context_manager
from machina.core.models import Session
from machina.core.provider import PROVIDERS
from machina.core.runtime import RuntimeServices, runtime_services
from machina.core.session import session_manager
from machina.db.connection import db
from machina.gateway import features
from machina.gateway.errors import (  # noqa: F401 — re-exported for clients and tests
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
from machina.gateway.features._common import _bool
from machina.gateway.serializers import history_from_chunks, session_to_dict
from machina.observability.diagnostics import record_failure

__all__ = [
    "Gateway",
    "RpcError",
    "history_from_chunks",
    "session_to_dict",
]

logger = logging.getLogger(__name__)
CLAIM_RENEW_INTERVAL_SECONDS = 30.0
CANCEL_JOIN_TIMEOUT_SECONDS = 0.1
# W5: bound session.resume history like the REST session endpoint
# (machina/api/routes/sessions.py reads context chunks with limit=200).
# RF4-W5: session.resume must not serialize an unbounded transcript, but the
# bound must not re-introduce the historical "resume truncated at 200" bug
# (pinned deliberately by test_resume_reads_beyond_two_hundred_chunks).
# 1000 keeps the payload bounded while preserving that contract; the DB-side
# fetch still full-scan (residual noted by FM1 for the context.py owner).
MAX_RESUME_HISTORY_CHUNKS = 1000
# W4: initialize may carry these keys without a token — the bare handshake.
# Any other key (model/provider/mode today, future state mutations) requires
# the auth token when one is configured.
_BARE_INITIALIZE_KEYS = frozenset({"protocolVersion", "token"})


# ---------------------------------------------------------------------------
# Gateway logging — file + console
# ---------------------------------------------------------------------------
def _setup_gateway_logging() -> None:
    """Configure gateway logging to file and console."""
    log_dir = os.path.join(os.path.expanduser("~"), ".machina", "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "gateway.log")

    # File handler — rotating
    from logging.handlers import RotatingFileHandler

    file_handler = RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    file_handler.setLevel(logging.DEBUG)

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    console_handler.setLevel(logging.WARNING)

    # Root logger for machina.gateway
    gw_logger = logging.getLogger("machina.gateway")
    gw_logger.setLevel(logging.DEBUG)
    gw_logger.addHandler(file_handler)
    gw_logger.addHandler(console_handler)

    # Also capture machina.core logs
    core_logger = logging.getLogger("machina.core")
    core_logger.setLevel(logging.DEBUG)
    core_logger.addHandler(file_handler)

    logger.info("Gateway logging initialized: %s", log_file)


_setup_gateway_logging()

# PROVIDERS is single-sourced from machina.core.provider (imported above) so the
# gateway, HTTP API and UI always agree on valid provider names.
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
    # Sentinel identity only: Gateway._run_turn branches on
    # ``self._agent_factory is _default_agent_factory`` and builds through
    # build_agent_for_session; this body is never invoked by the gateway.
    # The lazy imports below remain for any direct/legacy caller.
    from machina.core.agent import ReActAgent
    from machina.core.provider import get_provider

    llm = get_provider(provider=provider, model=model)
    agent = ReActAgent(
        provider=llm,
        agent_id=config.get("agent_id"),
    )
    agent._owns_provider = True  # type: ignore[attr-defined]
    return agent


class Gateway:
    """Dispatches JSON-RPC requests and streams agent turns as events."""

    def __init__(
        self,
        write: Writer,
        agent_factory: Callable[[str, str], StreamingAgent] = _default_agent_factory,
        owns_db: bool = True,
        *,
        runtime: RuntimeServices | None = None,
        inherit_env_token: bool = True,
    ) -> None:
        self._write = write
        self.protocol_version = 1
        self._agent_factory = agent_factory
        self._owns_db = owns_db
        # Stdio owns process cleanup; HTTP adapters borrow the app runtime.
        self.runtime = runtime if runtime is not None else runtime_services
        self.model: str = config.get("model")
        self.provider: str = config.get("provider")
        self.closing = False
        self._db_ready = False
        self._job_runner = None
        self._turns: dict[str, asyncio.Task[None]] = {}
        self._turn_tokens: dict[str, str] = {}
        self._turn_ownership_lost: dict[str, asyncio.Event] = {}
        self._turn_progress: dict[str, dict[str, int]] = {}
        # AH-AUDIT-028: turn ids that already emitted their single terminal
        # completion (quarantine fallback vs late genuine completion races).
        self._turn_finished: set[str] = set()
        self._cancel_requested: set[str] = set()
        # AH-AUDIT-029: accumulated genuine human-approval wait per turn.
        # Compute accounting pauses during these waits; approval timeout
        # stays separately bounded by the broker handler.
        self._approval_wait_total: dict[str, float] = {}
        self._approval_wait_started: dict[str, float] = {}
        # H-07: Sanitized progress stage tracker for hang diagnosis.
        # Records the current stage for each turn without logging sensitive data.
        self._turn_stages: dict[str, dict[str, Any]] = {}
        # Pending human approvals by request_id (Phase E). The broker's
        # approval handler emits permission.required and awaits the future;
        # approvals.resolve completes it. Reconnect-safe via durable DB rows.
        # Entries are (future, turn_id, session_id): turn cleanup cancels
        # ONLY its own turn's waits, never another session's (addendum check).
        self._pending_approvals: dict[str, tuple[asyncio.Future[str], str, str]] = {}
        self._config_lock = asyncio.Lock()
        # Token-based auth for the stdio channel. Local UIs set
        # MACHINA_GATEWAY_TOKEN; None means "open" (stdio local use) with a warning.
        # W8: an embedded gateway (HTTP /api/v1/rpc, already behind API-key
        # auth) sets inherit_env_token=False so the stdio token never reaches
        # it — explicit signal instead of mutating _auth_token from outside.
        self._inherit_env_token = inherit_env_token
        self._auth_token: str | None = (
            os.environ.get("MACHINA_GATEWAY_TOKEN") if inherit_env_token else None
        )
        if self._auth_token is None and inherit_env_token:
            logger.warning(
                "MACHINA_GATEWAY_TOKEN not set — gateway accepts unauthenticated requests"
            )
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

    def event_payload(
        self, event_type: str, session_id: str, turn_id: str, **fields: Any
    ) -> dict[str, Any]:
        from machina.protocol import gateway_event

        return gateway_event(event_type, session_id, turn_id, self.protocol_version, **fields)

    def add_method(
        self, name: str, handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
    ) -> None:
        """Register an extra JSON-RPC method (used by ``machina.gateway.features``)."""
        if name in self._methods:
            logger.debug("method already registered, overwriting: %s", name)
        self._methods[name] = handler

    def turn_running(self, session_id: uuid.UUID) -> bool:
        task = self._turns.get(str(session_id))
        return task is not None and not task.done()

    def _record_stage(self, turn_id: str, stage: str, **details: Any) -> None:
        """Record a progress stage for hang diagnosis (H-07).

        Sanitized: no raw keys, prompts, tool payloads, or credentials.
        Records: stage name, timestamp, elapsed time, and safe metadata.
        """
        now = time.monotonic()
        existing = self._turn_stages.get(turn_id)
        if existing:
            existing["elapsed_ms"] = int((now - existing["started_at"]) * 1000)
        self._turn_stages[turn_id] = {
            "stage": stage,
            "started_at": now,
            "elapsed_ms": 0,
            "details": {k: v for k, v in details.items() if k in ("session_id", "turn_id")},
        }
        logger.debug("Turn %s stage: %s", turn_id, stage)

    def cancel_turn_approvals(self, turn_id: str) -> int:
        """Cancel pending approval waits for ONE turn (LP additional check).

        A turn finishing in session A never cancels session B's approval:
        entries are partitioned by turn id.
        """
        n = 0
        for _rid, _entry in list(self._pending_approvals.items()):
            _fut, _tid, _sid = _entry
            if _tid == turn_id and not _fut.done():
                _fut.cancel()
                n += 1
        return n

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
            logger.warning("Invalid gateway request")
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
            if not self._token_ok(params.get("token")):
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
            logger.warning("← %s error %d (id=%s)", method, e.code, rid)
            self._send_error(rid, e.code, e.message)
            return
        except Exception:  # never let a handler crash the gateway
            logger.exception("Gateway method %s failed", method)
            self._send_error(rid, INTERNAL_ERROR, "internal error")
            return

        logger.debug("← %s ok (id=%s)", method, rid)
        if rid is not None:
            self._write({"jsonrpc": "2.0", "id": rid, "result": result})

    async def close(self, timeout: float = 10.0) -> None:
        """Cancel running turns, stop the job runner, release the DB pool.

        AH-AUDIT-024: bounded joining with a hard outer deadline.
        Requested cancellation is distinguished from confirmed termination:
        turns that suppress cancellation are quarantined (logged, left for
        process shutdown) instead of holding close() forever. Ownership
        safety is preserved — quarantined tasks keep their tokens until
        expiry rather than being force-released to a newer owner.
        """
        from machina.core.runtime import _bounded_join

        if self._job_runner is not None:
            try:
                await asyncio.wait_for(self._job_runner.stop(), timeout=timeout)
            except asyncio.CancelledError:
                # Shutdown cancellation of close() itself must propagate; only
                # the job runner's own bounded failures are absorbed below.
                raise
            except (TimeoutError, Exception):
                logger.warning("gateway job runner stop exceeded its bound; continuing")
            self._job_runner = None
        tasks = [t for t in self._turns.values() if not t.done()]
        if tasks:
            leftovers = await _bounded_join(tasks, timeout=timeout, label="gateway turns")
            for t in leftovers:
                logger.warning("gateway turn task quarantined on close: %s", t.get_name())
        if self._owns_db:
            try:
                await self.runtime.shutdown_connectors(timeout=timeout)
            except (asyncio.CancelledError, Exception) as _boundary_error:
                record_failure("server.close", _boundary_error)
        if self._db_ready and self._owns_db:
            # Agent background work (consolidation/learning reviews) must
            # finish before the pool closes; bounded, never raises.
            try:
                await drain_background_tasks()
            except (TimeoutError, asyncio.CancelledError, Exception) as _boundary_error:
                record_failure("server.close", _boundary_error)
            from machina.observability.audit import audit_persistence

            try:
                await asyncio.wait_for(audit_persistence.stop(), timeout=5)
            except (TimeoutError, asyncio.CancelledError, Exception) as _boundary_error:
                # Optional fallback preserves the primary outcome; report no payload.
                record_failure("server.close", _boundary_error)
            try:
                await asyncio.wait_for(db.close(), timeout=5)
            except (TimeoutError, asyncio.CancelledError, Exception) as _boundary_error:
                # Optional fallback preserves the primary outcome; report no payload.
                record_failure("server.close", _boundary_error)
        self._db_ready = False

    def _start_job_runner(self) -> None:
        """Run scheduled jobs in the background while the gateway is up.

        Disabled with MACHINA_GATEWAY_NO_SCHEDULER=1 (tests drive the runner directly).
        """
        if os.environ.get("MACHINA_GATEWAY_NO_SCHEDULER") or self._job_runner is not None:
            return
        from machina.core.scheduler import JobRunner

        self._job_runner = JobRunner()
        self._job_runner.start()

    def _send_error(self, rid: Any, code: int, message: str) -> None:
        self._write({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})

    def _token_ok(self, token: Any) -> bool:
        """Constant-time token check; malformed tokens fail closed.

        RF4-W3: compare_digest raises TypeError on non-ASCII str operands; a
        malformed token must never crash the stdio loop. True when no token is
        configured (open local mode).
        """
        if self._auth_token is None:
            return True
        try:
            return isinstance(token, str) and secrets.compare_digest(token, self._auth_token)
        except TypeError:
            return False

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
        version = params.get("protocolVersion", 1)
        if type(version) is not int or version not in (1, 2):
            raise RpcError(INVALID_PARAMS, "protocolVersion must be 1|2")
        # Pick up a token set after construction (e.g. tests / late env). An
        # embedded gateway opted out of env inheritance (W8) and never does.
        if self._auth_token is None and self._inherit_env_token:
            env_token = os.environ.get("MACHINA_GATEWAY_TOKEN")
            if env_token:
                self._auth_token = env_token
        # W4: the bare handshake stays token-free so a fresh client can
        # authenticate, but ANY state-changing parameter (model/provider/mode
        # today, future keys) requires the token once one is configured. The
        # check runs before protocol_version/plugins/config/DB are touched.
        if set(params) - _BARE_INITIALIZE_KEYS and not self._token_ok(params.get("token")):
            logger.warning("Unauthorized state-changing initialize rejected")
            raise RpcError(UNAUTHORIZED, "unauthorized")
        self.protocol_version = version
        logger.info("Gateway initialize: model=%s provider=%s", self.model, self.provider)
        from machina.plugins.loader import load_plugins

        load_plugins()
        async with self._config_lock:
            if params.get("model"):
                self._set_model(params["model"])
            if params.get("provider"):
                self._set_provider(params["provider"])
            # LP-13: CLI --mode reaches the gateway (previously dropped at the
            # Node boundary). Applied to this gateway process only
            # (non-persistent); new sessions auto-grant under a full default.
            if params.get("mode"):
                mode = str(params["mode"]).strip().lower()
                if mode not in ("ask", "workspace", "sandbox", "full"):
                    raise RpcError(INVALID_PARAMS, "mode must be ask|workspace|sandbox|full")
                config.set("execution_mode", mode)
        if not self._db_ready:
            try:
                await db.connect()
            except Exception as e:
                raise RpcError(DATABASE_UNAVAILABLE, f"database unavailable: {e}") from e
            self._db_ready = True
            try:
                await db.initialize_runtime_schema()
            except Exception as error:
                self._db_ready = False
                raise RpcError(DATABASE_UNAVAILABLE, "runtime schema upgrade failed") from error
            from machina.core.usage import usage_store

            # W7: startup cleanup must not reap another worker's in-flight
            # reservations. Pass the documented 10-minute grace
            # (UsageStore.cleanup_orphaned_reservations docstring): only rows
            # older than an entire turn can be orphans.
            await usage_store.cleanup_orphaned_reservations(older_than_minutes=10)
            from machina.observability.audit import audit_persistence

            audit_persistence.start()
            self._start_job_runner()
        return {
            "version": __version__,
            "protocolVersion": self.protocol_version,
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
        agent_id = str(params.get("agent") or "harness").strip()[:100] or "harness"
        if agent_id != "harness":
            # Fail fast on unknown agents (including profile scopes without
            # a profile); the turn factory would otherwise fail closed later.
            from machina.core.agent_def import agent_registry

            try:
                definition = await agent_registry.get(agent_id)
            except Exception as exc:
                raise RpcError(INVALID_PARAMS, f"unknown agent: {agent_id}") from exc
            if definition is None:
                raise RpcError(INVALID_PARAMS, f"unknown agent: {agent_id}")
        async with self._config_lock:
            model, provider = self.model, self.provider
        session = await session_manager.create(
            title=title,
            agent_id=agent_id,
            model=model,
            provider=provider,
            context_budget=config.get("context_budget"),
        )
        try:
            from machina.gateway.features.mode import grant_full_default

            await grant_full_default(session)
        except Exception as _boundary_error:
            # Optional fallback preserves the primary outcome; report no payload.
            record_failure("server._session_create", _boundary_error)
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
        chunks = await context_manager.get_transcript_chunks(session.id)
        # W5: get_transcript_chunks has no LIMIT; the REST session endpoint
        # bounds history at 200 chunks (machina/api/routes/sessions.py). Slice
        # the newest-first snapshot to the same bound so resume cannot return
        # an unbounded payload (or force an unbounded JSON serialization).
        return {
            "session": session_to_dict(session),
            "history": history_from_chunks(chunks[:MAX_RESUME_HISTORY_CHUNKS]),
        }

    async def _usage_get(self, params: dict[str, Any]) -> dict[str, Any]:
        self.require_db()
        session = await self.get_session(params)
        agent = params.get("agent") or session.agent_id
        if not isinstance(agent, str) or not 1 <= len(agent) <= 100:
            raise RpcError(INVALID_PARAMS, "agent must be a non-empty string up to 100 characters")
        from machina.core.usage import usage_store

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
        from machina.core.images import validate_images

        try:
            images = validate_images(params.get("images", []))
        except ValueError as exc:
            raise RpcError(INVALID_PARAMS, str(exc)) from exc
        for index in range(1, len(images) + 1):
            marker = f"[image {index}]"
            if marker not in text:
                text += "\n" + marker
        key = str(session.id)
        # Shared coordinator claim (cross-transport owner token). The DB claim
        # fences same-process and REST/scheduler workers alike (N3, final pass:
        # the former `_turn_locks` in-process lock was declared but never
        # acquired — removed). Fails closed on claim errors.
        from machina.core.turns import try_begin_turn

        turn_id = uuid.uuid4().hex[:12]
        token = await try_begin_turn(session.id, turn_id=turn_id)
        if not token:
            raise RpcError(TURN_IN_PROGRESS, "a turn is already running for this session")
        running = self._turns.get(key)
        if running is not None and not running.done():
            from machina.core.turns import end_turn as _end

            await _end(session.id, token)
            raise RpcError(TURN_IN_PROGRESS, "a turn is already running for this session")
        logger.info("Turn %s started for session %s", turn_id, session.id)
        self._turn_tokens[key] = token
        image_kwargs = {"images": images} if images else {}
        self._turns[key] = asyncio.create_task(
            self._run_turn_with_timeout(session.id, turn_id, text, **image_kwargs)
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
        except (asyncio.CancelledError, TimeoutError):
            pass
        # Entry removal is left to _run_turn's finally block, which deletes
        # only if the stored task is still the current one (identity check).
        return {"cancelled": True}

    async def _config_set(self, params: dict[str, Any]) -> dict[str, Any]:
        """Set a setting. ``model``/``provider`` apply to this gateway; any other
        non-secret key updates the config (and ``~/.machina/config.yaml``
        when ``persist`` is true). Secrets are only ever read from the environment."""
        from machina.core.config import DEFAULTS, SECRET_KEYS

        key, value = params.get("key"), params.get("value")
        persist = _bool(params, "persist")
        async with self._config_lock:
            previous_route = getattr(self, key) if key in ("model", "provider") else None
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
                if key == "reasoning_effort":
                    from machina.core.provider import normalize_reasoning_effort

                    try:
                        value = normalize_reasoning_effort(value)
                    except Exception as e:
                        raise RpcError(INVALID_PARAMS, str(e)) from None
                config.set(key, value, persist=persist)
            else:
                raise RpcError(INVALID_PARAMS, f"unknown setting: {key}")
            if persist and key in ("model", "provider"):
                try:
                    config.set(key, value, persist=True)
                except Exception:
                    setattr(self, key, previous_route)
                    raise
                # MACHINA_MODEL/PROVIDER from .env shadows config.yaml on
                # restart (Config.get checks env first). Sync both so a saved
                # default survives restarts and live config.get reads.
                env_key = f"MACHINA_{key.upper()}"
                os.environ[env_key] = value
                try:
                    from machina.security.env_file import set_env_values

                    set_env_values({env_key: value})
                except (OSError, ValueError) as e:
                    logger.warning(
                        "config.set: .env sync failed for %s (%s)",
                        env_key,
                        type(e).__name__,
                    )
        result: dict[str, Any] = {"model": self.model, "provider": self.provider, "key": key}
        result["value"] = getattr(self, key) if key in ("model", "provider") else config.get(key)
        return result

    async def _shutdown(self, params: dict[str, Any]) -> dict[str, Any]:
        logger.info("Gateway shutdown requested")
        self.closing = True
        return {}

    # ─── turns ─────────────────────────────────────────────────────────────
    async def _run_turn(
        self,
        session_id: uuid.UUID,
        turn_id: str,
        text: str,
        *,
        images: list[dict[str, str]] | None = None,
    ) -> None:
        """Run one agent turn, translating agent stream events into protocol events."""
        sid = str(session_id)
        owned_token = self._turn_tokens.get(sid)
        registered_turn = self._turns.get(sid)
        self._record_stage(turn_id, "turn_started", session_id=sid)
        # S2 observe-only hook: open the turn span (never gates execution).
        try:
            from machina.core.intent import classify_task as _trace_classify
            from machina.observability.traces import start_turn as _trace_start_turn

            try:
                _trace_task_class = _trace_classify(text)
            except Exception:
                _trace_task_class = ""
            _trace_start_turn(turn_id, session_id=sid, task_class=_trace_task_class)
        except Exception:
            pass

        def emit(event_type: str, **fields: Any) -> None:
            self._write(
                {
                    "jsonrpc": "2.0",
                    "method": "event",
                    "params": self.event_payload(event_type, sid, turn_id, **fields),
                }
            )

        def complete(final: Any = None, cancelled: bool = False) -> None:
            cancelled = cancelled or turn_id in self._cancel_requested
            # AH-AUDIT-027: message.complete.text is the canonical complete
            # answer — the fully-redacted final content exactly once. The
            # streaming tail is already part of final.content; prepending it
            # would duplicate it in the payload while the visible stream
            # omits it. Only when there is no final (error/empty path) does
            # the flushed tail become the text.
            # Exactly-once guard: quarantined timeout paths may emit a
            # fallback completion; a late genuine completion is then a
            # duplicate and is suppressed (first terminal event wins).
            if turn_id in self._turn_finished:
                return
            self._turn_finished.add(turn_id)
            # S2 observe-only hook: close the turn span (never gates execution).
            try:
                from machina.observability.traces import end_turn as _trace_end_turn

                if cancelled:
                    _trace_end_turn(turn_id, status="cancelled", stop_reason="cancelled")
                elif final is None:
                    _trace_end_turn(turn_id, status="error", stop_reason="error")
                else:
                    _trace_end_turn(
                        turn_id,
                        status="ok",
                        stop_reason=str(getattr(final, "outcome", "") or "completed"),
                    )
            except Exception:
                pass
            try:
                from machina.memory.redaction import redact_secrets as _redact

                text = _redact(final.content).text if final is not None else ""
            except Exception:
                text = ""
            if final is None:
                try:
                    tail = _turn_redactor.flush()
                except Exception:
                    tail = ""
                if tail:
                    try:
                        from machina.memory.redaction import redact_secrets as _redact2

                        tail = _redact2(tail).text
                    except Exception as _boundary_error:
                        # Optional fallback preserves the primary outcome; report no payload.
                        record_failure("server._run_turn", _boundary_error)
                    text = tail
            else:
                try:
                    _turn_redactor.flush()
                except Exception as _boundary_error:
                    # Optional fallback preserves the primary outcome; report no payload.
                    record_failure("server._run_turn", _boundary_error)
            pauses = []
            try:
                for p in getattr(final, "needs_approval", None) or []:
                    if isinstance(p, dict) and p.get("request_id"):
                        from machina.protocol import approval_summary

                        pauses.append(approval_summary(p))
            except Exception:
                pauses = []
            emit(
                "message.complete",
                text=text,
                # Prefer the agent's final accounting; fall back to the tokens
                # accumulated from token_usage events along the way.
                tokens=(final.tokens_used or tokens) if final is not None else tokens,
                iterations=final.iterations if final is not None else 0,
                toolCalls=len(final.tool_calls) if final is not None else 0,
                cancelled=cancelled,
                **({"needsApproval": pauses} if pauses else {}),
            )

        emit("message.start")
        open_tools: dict[str, str] = {}  # tool id -> tool name, in start order
        tool_count = 0
        tokens = 0
        final = None
        agent = None
        _cancelled = False
        _release_failed = False
        # Streaming redaction per turn (5.1): text deltas pass the frontier
        # redactor; the withheld tail flushes at completion. A safe helper
        # unused by this transport would not be a fix.
        from machina.memory.redaction import StreamingSecretRedactor as _SSR

        _turn_redactor = _SSR()
        # Phase E: human approval handler for this turn. Emits
        # permission.required and awaits the UI/HTTP resolve without holding
        # DB transactions — logical turn ownership is retained.
        from machina.permissions.broker import set_approval_handler as _set_handler

        async def _turn_approver(card: dict) -> str:
            loop = asyncio.get_running_loop()
            fut: asyncio.Future[str] = loop.create_future()
            self._pending_approvals[card["request_id"]] = (fut, turn_id, sid)
            # Concurrent approval cards share one paused wall-clock interval.
            self._approval_wait_started.setdefault(turn_id, loop.time())
            # AH-AUDIT-003/004: the card carries the exact executable and
            # arguments (argv list), cwd, backend, timeout, network, and
            # file-write content identity + diff. Never a bare operation
            # name with an empty target.
            from machina.protocol import approval_fields

            emit("needs_approval", **approval_fields(card))
            # AH-AUDIT-029: genuine human waits pause compute accounting
            # (tracked separately); approval timeout stays broker-bounded.
            # Renew logical ownership while waiting so a long human pause
            # does not expire the live claim mid-turn.
            try:
                from machina.core.turns import renew_turn as _renew_claim

                _renew_token = owned_token
            except Exception as exc:
                record_failure("server.approval_renew", exc)
                _renew_token = None
            try:
                return await fut
            finally:
                self._pending_approvals.pop(card["request_id"], None)
                try:
                    if not any(
                        owner == turn_id for _, owner, _ in self._pending_approvals.values()
                    ):
                        started = self._approval_wait_started.pop(turn_id, None)
                        if started is not None:
                            waited = asyncio.get_running_loop().time() - started
                            self._approval_wait_total[turn_id] = self._approval_wait_total.get(
                                turn_id, 0.0
                            ) + max(0.0, waited)
                    if _renew_token:
                        renewed = await _renew_claim(session_id, _renew_token)
                        if not renewed:
                            from machina.core.turns import owns_claim as _owns_claim

                            if not await _owns_claim(session_id, _renew_token):
                                # Ownership moved on during the human wait:
                                # stop owned effects instead of interleaving
                                # with the new owner.
                                _current = asyncio.current_task()
                                if _current is not None:
                                    _current.cancel()
                                raise RuntimeError("turn ownership lost during approval wait")
                except RuntimeError:
                    raise
                except Exception as _boundary_error:
                    # Optional fallback preserves the primary outcome; report no payload.
                    record_failure("server._run_turn", _boundary_error)

        _set_handler(_turn_approver, principal="tui", turn_id=turn_id)
        # Growth indicator: background training (consolidation wins, staged
        # reviews) surfaces as growth.status events on this turn's stream.
        from machina.core.agent import add_growth_listener, remove_growth_listener

        def _growth_emit(notify_session: str, stage: str, detail: str) -> None:
            if notify_session != sid:
                return
            try:
                emit("growth.status", stage=stage, detail=detail)
            except Exception as _boundary_error:
                record_failure("server.growth_emit", _boundary_error)

        add_growth_listener(_growth_emit)
        # AH-AUDIT-013: managed claim renewal for the whole turn (compute +
        # approval waits). A live renewing owner blocks takeovers; repeated
        # renewal failure means the claim lapsed — owned effects stop via
        # the loss signal. Crash expiry recovery is preserved (a dead
        # worker renews nothing and its claim becomes reclaimable).
        _ownership_lost = self._turn_ownership_lost.setdefault(turn_id, asyncio.Event())
        _owned_task = asyncio.current_task()
        from machina.core.turns import TurnOwnership as _TurnOwnership

        _owner = _TurnOwnership(session_id, turn_id, owned_token, ownership_lost=_ownership_lost)

        def _on_ownership_lost() -> None:
            # Stop owned effects FIRST: the cancel is the critical effect and
            # must not be skippable by an emit failure. The frame (correct
            # protocol name: turn.ownership_lost, underscore) follows.
            if _owned_task is not None:
                _owned_task.cancel()
            emit("turn.ownership_lost", reason="claim_renewal_failed")

        _renew_task = _owner.start_renewal(
            interval_s=CLAIM_RENEW_INTERVAL_SECONDS, on_lost=_on_ownership_lost
        )
        try:
            try:
                # AH-021/AH-022: resolve the session's stored model/provider and
                # agent definition (allowed_tools/persona). Resumed sessions
                # execute with their own settings, not the gateway globals;
                # globals only apply to newly created sessions. Direct execution
                # of a specialist child can no longer bypass its restrictions.
                # AH-AUDIT-019: execution-critical read bypasses the display
                # cache so resumed sessions run with current durable settings.
                session = await session_manager.get_fresh(session_id)
                if session is None:
                    # W1: no terminal here — the single outer finally below runs
                    # cleanup and emits message.complete after release.
                    emit("error", message="session not found")
                    return
                # S2 observe-only hook: attribute the agent id (never gates execution).
                try:
                    from machina.observability.traces import set_turn_attributes as _trace_set_attrs

                    _trace_set_attrs(turn_id, agent_id=getattr(session, "agent_id", ""))
                except Exception:
                    pass
                try:
                    from machina.core.agent_factory import build_agent_for_session
                except Exception:
                    # Fail closed (Pattern C/5.4): no silent fallback to a weaker
                    # global factory. Surface the error; the turn cannot run.
                    logger.exception("Agent factory unavailable")
                    # W1: terminal is emitted once by the outer finally, after
                    # renewal join + claim release.
                    emit("error", message="agent factory unavailable; turn aborted")
                    return
                self._record_stage(turn_id, "building_agent", session_id=sid)
                if self._agent_factory is _default_agent_factory:
                    agent = await build_agent_for_session(session)
                else:
                    # Explicit in-process dependency injection is trusted code,
                    # never a fallback after a default factory failure. Resolve
                    # stored session settings on this path too.
                    agent = self._agent_factory(
                        session.model or self.model, session.provider or self.provider
                    )
                self._record_stage(turn_id, "waiting_for_provider", session_id=sid)
                stream_kwargs = {"images": images} if images else {}
                async for event in agent.run_stream(
                    session_id, text, verbose=False, **stream_kwargs
                ):
                    if event.type == "text":
                        safe = _turn_redactor.feed(event.content or "")
                        if safe:
                            emit("message.delta", text=safe)
                    elif event.type == "tool_call":
                        tool_count += 1
                        self._record_stage(turn_id, "running_tool", session_id=sid)
                        # AH-020: prefer the agent's stable call ID so out-of-order
                        # completions pair correctly; fall back to turn counter.
                        tool_id = getattr(event, "tool_call_id", "") or f"{turn_id}-{tool_count}"
                        open_tools[tool_id] = event.tool_name
                        self._turn_progress[turn_id] = {
                            "tokens": tokens,
                            "iterations": 0,
                            "toolCalls": tool_count,
                        }
                        from machina.memory.redaction import redact_value

                        emit(
                            "tool.start",
                            id=tool_id,
                            name=event.tool_name,
                            args=redact_value(event.tool_args),
                        )
                    elif event.type == "tool_result":
                        # Pair by tool-call id when the event carries one;
                        # otherwise fall back to the oldest open call with the
                        # same name (FIFO).
                        result_id = getattr(event, "tool_call_id", None) or getattr(
                            event, "id", None
                        )
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
                        from machina.memory.redaction import redact_secrets

                        result = redact_secrets(str(event.tool_result)).text
                        truncated = len(result) > MAX_TOOL_RESULT_CHARS
                        emit(
                            "tool.complete",
                            id=match_id if match_id else f"{turn_id}-{tool_count}",
                            name=event.tool_name,
                            result=result[:MAX_TOOL_RESULT_CHARS],
                            truncated=truncated,
                            isError=result.startswith("Error:"),
                            **({"outcome": event.tool_outcome} if event.tool_outcome else {}),
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
                # AH-AUDIT-028: do NOT emit terminal completion here. Emit a
                # stopping state (UI stays blocked), run cleanup + ownership
                # release in finally, then emit the single terminal completion
                # below — completion means the turn is no longer owned.
                _cancelled = True
                emit("message.stopping", reason="cancelled")
            except Exception:
                _cancelled = _ownership_lost.is_set()
                logger.exception("Turn %s failed", turn_id)
                emit("error", message="agent turn failed")
            finally:
                self._record_stage(turn_id, "cleanup_started", session_id=sid)
                # Release the approval handler and cancel any still-pending
                # approval waits (cancellation stops owned waits, not siblings).
                try:
                    from machina.permissions.broker import set_approval_handler as _clear

                    _clear(None)
                except Exception as _boundary_error:
                    # Optional fallback preserves the primary outcome; report no payload.
                    record_failure("server._run_turn", _boundary_error)
                self.cancel_turn_approvals(turn_id)
                # H-01/H-02: Bounded cleanup contract.
                # 1. Learning tasks: bounded join, then cancel leftovers.
                # 2. Provider close: bounded with timeout.
                # 3. Claim release: independent outer finally, always runs.
                try:
                    if agent is not None:
                        self._record_stage(turn_id, "joining_optional_work", session_id=sid)
                        # Shared helper: bounded, settles before propagating, and
                        # timeout + settle_grace stays under the turn-completion
                        # deadline even for cancellation-resistant tasks.
                        await join_agent_background_tasks(agent, timeout=1.7, settle_grace=0.35)
                except (TimeoutError, Exception) as _boundary_error:
                    # Optional fallback preserves the primary outcome; report no payload.
                    record_failure("server._run_turn", _boundary_error)
                finally:
                    # Claim release in independent outer finally.
                    try:
                        self._record_stage(turn_id, "closing_owned_clients", session_id=sid)
                        # AH-023: close only owned providers, bounded.
                        if agent is not None:
                            try:
                                from machina.core.agent_factory import close_agent_provider

                                await close_agent_provider(agent)
                            except (TimeoutError, asyncio.CancelledError):
                                logger.warning("Provider close timed out for session %s", sid)
                            except Exception:
                                logger.exception(
                                    "Could not close turn provider for session %s", sid
                                )
                    finally:
                        self._record_stage(turn_id, "releasing_ownership", session_id=sid)
                        # RF4-C1 hotfix: stop renewal BEFORE release. A cancellation
                        # delivered while releasing previously skipped the
                        # renewal-stop below, orphaning the renewal loop and
                        # keeping an unreleased claim renewed forever (permanent
                        # TURN_IN_PROGRESS lock until process restart).
                        if _renew_task is not None and not _renew_task.done():
                            _renew_task.cancel()
                        # Owner-token release on EVERY exit (success/failure/cancel/timeout).
                        # Stale-safe: only this turn's token clears its own claim.
                        try:
                            from machina.core.turns import end_turn as _end_turn

                            if owned_token:
                                # Release is shielded so a cancellation delivered
                                # mid-flight cannot orphan the claim: wait (bounded)
                                # for the release to settle, then continue cleanup
                                # and terminal emission instead of unwinding.
                                _release = asyncio.ensure_future(_end_turn(session_id, owned_token))
                                try:
                                    await asyncio.shield(_release)
                                except asyncio.CancelledError:
                                    try:
                                        await asyncio.wait_for(
                                            asyncio.shield(_release), timeout=5.0
                                        )
                                    except (TimeoutError, asyncio.CancelledError, Exception):
                                        logger.warning(
                                            "turn release did not settle within bound; "
                                            "claim falls back to TTL expiry"
                                        )
                            if self._turn_tokens.get(sid) == owned_token:
                                self._turn_tokens.pop(sid, None)
                        except Exception as _boundary_error:
                            _release_failed = True
                            # Preserve the owner token until DB recovery or claim expiry.
                            record_failure("server._run_turn", _boundary_error)
                        if registered_turn is not None and self._turns.get(sid) is registered_turn:
                            del self._turns[sid]
        finally:
            # W1: ONE always-run cleanup after add_growth_listener. The two
            # early return paths (session not found / agent factory
            # unavailable) used to call complete() BEFORE cleanup and then
            # skip this block entirely: the growth listener and the
            # _turn_ownership_lost entry leaked, the renewal loop was never
            # joined (a delayed spurious turn.ownership_lost could land after
            # message.complete) and _cancel_requested kept the turn id.
            # AH-AUDIT-013: stop/join renewal during release (never leaks).
            if _renew_task is not None and not _renew_task.done():
                _renew_task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(_renew_task), timeout=5)
            except asyncio.CancelledError:
                # The join itself cancels (and joins) the renewal task, so an
                # inner-task CancelledError is the expected completion. A
                # cancellation delivered to the turn while it is being torn
                # down (shutdown) must propagate instead of being absorbed.
                if not _renew_task.cancelled():
                    raise
            except (TimeoutError, Exception):
                if not _renew_task.done():
                    _renew_task.cancel()
            finally:
                self._turn_ownership_lost.pop(turn_id, None)
            try:
                if _release_failed:
                    # No terminal: the claim may belong to a successor, so
                    # completion cannot claim the turn is unowned. Recovery
                    # stays via DB reconciliation / TTL expiry.
                    emit("turn.cleanup_pending", reason="ownership release unavailable")
                else:
                    # AH-AUDIT-027: emit the safe flushed tail as a delta
                    # exactly once before terminal completion, so concatenated
                    # displayed text equals the canonical redacted answer
                    # exactly once (also when prior deltas were emitted and
                    # when the whole answer was withheld).
                    try:
                        _tail = _turn_redactor.flush()
                        if _tail:
                            from machina.memory.redaction import redact_secrets as _redact_tail

                            _tail = _redact_tail(_tail).text
                            if _tail:
                                emit("message.delta", text=_tail)
                    except Exception as _boundary_error:
                        # Optional fallback preserves the primary outcome; report no payload.
                        record_failure("server._run_turn", _boundary_error)
                    # AH-AUDIT-028: this is the single terminal completion for
                    # every path (success/error/cancel/early-abort) — emitted
                    # only after cleanup and ownership release succeeded.
                    complete(final, cancelled=_cancelled)
            finally:
                # Growth notices may land with late background consolidation;
                # keep the listener until the terminal event, then always drop it.
                remove_growth_listener(_growth_emit)
                self._cancel_requested.discard(turn_id)
                self._record_stage(turn_id, "answer_complete", session_id=sid)
        if _release_failed:
            # Claim still owned by this turn's token (release unavailable):
            # do not schedule cross-owner maintenance.
            return
        # Phase C: threshold-triggered auto-compaction at the safe turn
        # boundary (ownership released above). AH-AUDIT-025: registered
        # with the shared runtime so shutdown cancels/joins it before DB
        # closure instead of leaking a use-after-close call.
        try:
            self._record_stage(turn_id, "compaction_maintenance", session_id=sid)
            from machina import services as _services
            from machina.core.runtime import runtime_services as _runtime

            task = asyncio.create_task(_services.maybe_auto_compact(session_id))
            task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
            try:
                _runtime.track(task)
            except Exception as _boundary_error:
                # Optional fallback preserves the primary outcome; report no payload.
                record_failure("server._run_turn", _boundary_error)
        except Exception as _boundary_error:
            # Optional fallback preserves the primary outcome; report no payload.
            record_failure("server._run_turn", _boundary_error)

    async def _run_turn_with_timeout(
        self,
        session_id: uuid.UUID,
        turn_id: str,
        text: str,
        *,
        images: list[dict[str, str]] | None = None,
    ) -> None:
        """Observe compute time separately from approvals; retirement owns completion.

        Timeout and cancel request termination and observe a bounded grace. A
        resistant inner task retains its claim and strong registration; it alone
        emits terminal completion after its actual cleanup. A late result cannot
        turn a requested cancellation into success.
        """
        timeout = float(config.get("turn_timeout") or 300)
        sid = str(session_id)
        current = asyncio.current_task()
        turn_kwargs = {"images": images} if images else {}
        inner = asyncio.create_task(self._run_turn(session_id, turn_id, text, **turn_kwargs))
        start = asyncio.get_running_loop().time()

        def retired(task) -> None:
            if not task.cancelled():
                task.exception()
            if self._turns.get(sid) is task:
                self._turns.pop(sid, None)
            self._turn_progress.pop(turn_id, None)
            self._turn_stages.pop(turn_id, None)  # W2: stage tracker no longer grows forever
            self._approval_wait_total.pop(turn_id, None)
            self._approval_wait_started.pop(turn_id, None)
            self._cancel_requested.discard(turn_id)

        inner.add_done_callback(retired)

        async def request_stop(reason: str) -> None:
            self._cancel_requested.add(turn_id)
            inner.cancel()
            done, _ = await asyncio.wait({inner}, timeout=CANCEL_JOIN_TIMEOUT_SECONDS)
            if inner not in done:
                self._write(
                    {
                        "jsonrpc": "2.0",
                        "method": "event",
                        "params": self.event_payload(
                            "turn.cleanup_pending", sid, turn_id, reason=reason
                        ),
                    }
                )

        try:
            while not inner.done():
                now = asyncio.get_running_loop().time()
                waited = self._approval_wait_total.get(turn_id, 0.0)
                approval_started = self._approval_wait_started.get(turn_id)
                if approval_started is not None:
                    waited += max(0.0, now - approval_started)
                if now - start - waited >= timeout:
                    logger.warning("Turn %s exceeded compute timeout", turn_id)
                    self._write(
                        {
                            "jsonrpc": "2.0",
                            "method": "event",
                            "params": self.event_payload(
                                "error", sid, turn_id, message="turn timed out"
                            ),
                        }
                    )
                    await request_stop("timeout")
                    return
                await asyncio.wait({inner}, timeout=0.2)
            if not inner.cancelled():
                inner.result()
        except asyncio.CancelledError:
            await request_stop("cancelled")
            raise
        finally:
            if self._turns.get(sid) is current:
                if inner.done():
                    self._turns.pop(sid, None)
                else:
                    self._turns[sid] = inner
                    from machina.core.runtime import runtime_services

                    runtime_services.track(inner)
            if len(self._turn_finished) > 1000:
                self._turn_finished.clear()


def _parse_session_id(params: dict[str, Any]) -> uuid.UUID:
    """Parse sessionId, accepting canonical, braced, and URN UUID forms."""
    try:
        return uuid.UUID(str(params.get("sessionId")).strip())
    except (ValueError, TypeError):
        raise RpcError(INVALID_PARAMS, "sessionId must be a UUID") from None
