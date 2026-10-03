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
import subprocess
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from ah import __version__
from ah.core.config import config
from ah.core.context import context_manager
from ah.core.models import ContextChunk, Session
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
    RpcError,
)

__all__ = [
    "Gateway",
    "RpcError",
    "history_from_chunks",
    "session_to_dict",
]

logger = logging.getLogger(__name__)

PROVIDERS = ("openrouter", "ollama")
MAX_TOOL_RESULT_CHARS = 4000

Writer = Callable[[dict[str, Any]], None]


class StreamingAgent(Protocol):
    def run_stream(self, session_id: uuid.UUID, user_message: str, verbose: bool = ...) -> Any: ...


def session_to_dict(session: Session) -> dict[str, Any]:
    return {
        "id": str(session.id),
        "title": session.title or "",
        "model": session.model or "",
        "provider": session.provider or "",
        "status": session.status,
        "lastActivity": session.last_activity.isoformat() if session.last_activity else None,
    }


def history_from_chunks(chunks: list[ContextChunk]) -> list[dict[str, Any]]:
    """Turn stored context chunks (newest first) into a chronological transcript."""
    history: list[dict[str, Any]] = []
    for chunk in reversed(chunks):
        payload = chunk.payload
        if chunk.chunk_type == "user_message":
            history.append({"role": "user", "content": str(payload.get("content", ""))})
        elif chunk.chunk_type == "assistant_message":
            history.append({"role": "assistant", "content": str(payload.get("content", ""))})
        elif chunk.chunk_type == "tool_call":
            history.append(
                {
                    "role": "tool",
                    "tool": str(payload.get("tool", "")),
                    "content": str(payload.get("result_preview", "")),
                }
            )
        elif chunk.chunk_type == "compression_summary":
            history.append({"role": "system", "content": str(payload.get("content", ""))})
    return history


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
    ) -> None:
        self._write = write
        self._agent_factory = agent_factory
        self.model: str = config.get("model")
        self.provider: str = config.get("provider")
        self.closing = False
        self._db_ready = False
        self._turns: dict[str, asyncio.Task[None]] = {}
        self._methods: dict[str, Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]] = {
            "initialize": self._initialize,
            "session.create": self._session_create,
            "session.list": self._session_list,
            "session.resume": self._session_resume,
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
            self._send_error(None, PARSE_ERROR, f"parse error: {e}")
            return

        rid = message.get("id") if isinstance(message, dict) else None
        if (
            not isinstance(message, dict)
            or message.get("jsonrpc") != "2.0"
            or not isinstance(message.get("method"), str)
        ):
            self._send_error(rid, INVALID_REQUEST, "invalid request")
            return

        params = message.get("params") or {}
        if not isinstance(params, dict):
            self._send_error(rid, INVALID_PARAMS, "params must be an object")
            return

        handler = self._methods.get(message["method"])
        if handler is None:
            self._send_error(rid, METHOD_NOT_FOUND, f"unknown method: {message['method']}")
            return

        try:
            result = await handler(params)
        except RpcError as e:
            self._send_error(rid, e.code, e.message)
            return
        except Exception as e:  # never let a handler crash the gateway
            logger.exception("Gateway method %s failed", message["method"])
            self._send_error(rid, INTERNAL_ERROR, f"{type(e).__name__}: {e}")
            return

        if rid is not None:
            self._write({"jsonrpc": "2.0", "id": rid, "result": result})

    async def close(self) -> None:
        """Cancel running turns and release the database pool."""
        tasks = [t for t in self._turns.values() if not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._db_ready:
            await db.close()
            self._db_ready = False

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
        return {
            "version": __version__,
            "model": self.model,
            "provider": self.provider,
            "cwd": os.getcwd(),
            "branch": _git_branch(),
        }

    async def _session_create(self, params: dict[str, Any]) -> dict[str, Any]:
        self.require_db()
        title = str(params.get("title") or "").strip()[:80] or "New session"
        session = await session_manager.create(
            title=title,
            model=self.model,
            provider=self.provider,
            context_budget=config.get("context_budget"),
        )
        return {"session": session_to_dict(session)}

    async def _session_list(self, params: dict[str, Any]) -> dict[str, Any]:
        self.require_db()
        limit = params.get("limit", 20)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
            raise RpcError(INVALID_PARAMS, "limit must be an integer between 1 and 200")
        sessions = await session_manager.list_sessions(limit=limit)
        return {"sessions": [session_to_dict(s) for s in sessions]}

    async def _session_resume(self, params: dict[str, Any]) -> dict[str, Any]:
        self.require_db()
        session = await self.get_session(params)
        chunks = await context_manager.get_chunks(session.id, limit=200)
        return {"session": session_to_dict(session), "history": history_from_chunks(chunks)}

    async def _prompt_submit(self, params: dict[str, Any]) -> dict[str, Any]:
        self.require_db()
        session = await self.get_session(params)
        text = params.get("text")
        if not isinstance(text, str) or not text.strip():
            raise RpcError(INVALID_PARAMS, "text must be a non-empty string")
        key = str(session.id)
        running = self._turns.get(key)
        if running is not None and not running.done():
            raise RpcError(TURN_IN_PROGRESS, "a turn is already running for this session")
        turn_id = uuid.uuid4().hex[:12]
        self._turns[key] = asyncio.create_task(self._run_turn(session.id, turn_id, text.strip()))
        return {"turnId": turn_id}

    async def _prompt_cancel(self, params: dict[str, Any]) -> dict[str, Any]:
        key = str(_parse_session_id(params))
        task = self._turns.get(key)
        if task is None or task.done():
            return {"cancelled": False}
        task.cancel()
        return {"cancelled": True}

    async def _config_set(self, params: dict[str, Any]) -> dict[str, Any]:
        """Set a setting. ``model``/``provider`` apply to this gateway; any other
        non-secret key updates the config (and ``~/.agent-harness/config.yaml``
        when ``persist`` is true). Secrets are only ever read from the environment."""
        from ah.core.config import DEFAULTS, SECRET_KEYS

        key, value = params.get("key"), params.get("value")
        if key == "model":
            self._set_model(value)
        elif key == "provider":
            self._set_provider(value)
        elif key in SECRET_KEYS:
            raise RpcError(INVALID_PARAMS, f"{key} is a secret; set it in .env instead")
        elif isinstance(key, str) and key in DEFAULTS:
            config.set(
                key, features.coerce_config_value(key, value), persist=bool(params.get("persist"))
            )
        else:
            raise RpcError(INVALID_PARAMS, f"unknown setting: {key}")
        if params.get("persist") and key in ("model", "provider"):
            config.set(key, value, persist=True)
        result: dict[str, Any] = {"model": self.model, "provider": self.provider, "key": key}
        result["value"] = getattr(self, key) if key in ("model", "provider") else config.get(key)
        return result

    async def _shutdown(self, params: dict[str, Any]) -> dict[str, Any]:
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
                tokens=final.tokens_used if final is not None else tokens,
                iterations=final.iterations if final is not None else 0,
                toolCalls=len(final.tool_calls) if final is not None else 0,
                cancelled=cancelled,
            )

        emit("message.start")
        open_tools: list[tuple[str, str]] = []  # (tool id, tool name), in start order
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
                    open_tools.append((tool_id, event.tool_name))
                    emit("tool.start", id=tool_id, name=event.tool_name, args=event.tool_args)
                elif event.type == "tool_result":
                    match = next((t for t in open_tools if t[1] == event.tool_name), None)
                    if match is not None:
                        open_tools.remove(match)
                    result = str(event.tool_result)
                    emit(
                        "tool.complete",
                        id=match[0] if match else f"{turn_id}-{tool_count}",
                        name=event.tool_name,
                        result=result[:MAX_TOOL_RESULT_CHARS],
                        isError=result.startswith("Error:"),
                    )
                elif event.type == "token_usage":
                    tokens += event.tokens_used
                    emit("usage", tokens=tokens)
                elif event.type == "done":
                    final = event.response
        except asyncio.CancelledError:
            complete(cancelled=True)
            return
        except Exception as e:
            logger.exception("Turn %s failed", turn_id)
            emit("error", message=f"{type(e).__name__}: {e}")
            complete()
            return
        finally:
            if self._turns.get(sid) is asyncio.current_task():
                del self._turns[sid]
        complete(final)


def _parse_session_id(params: dict[str, Any]) -> uuid.UUID:
    try:
        return uuid.UUID(str(params.get("sessionId")))
    except (ValueError, TypeError):
        raise RpcError(INVALID_PARAMS, "sessionId must be a UUID") from None
