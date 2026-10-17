"""Structured instrumentation for every operational seam.

This module is the single, dependency-light entry point for "is this thing
working?" logging across the codebase:

* :func:`instrument` — a synchronous context manager that emits a ``start``
  record and exactly one terminal record (``finish`` / ``error`` /
  ``cancelled``) with a duration. It re-raises the original exception
  unchanged and is fail-open: instrumentation never alters behaviour.
* :func:`log_event` — one structured record for point-in-time facts.
* :func:`instrumented` — decorator form for sync and async callables.

Records are JSON Lines, one object per line, written to a dedicated sink:
``$MACHINA_INSTRUMENTATION_LOG`` when set, else
``$MACHINA_HOME/logs/instrumentation-YYYY-MM-DD.jsonl``. The sink is
size-rotated (10 MiB, one previous file), appended under a threading lock and
an ``fcntl`` lock where available, and opened with ``0o600``.

Payload hygiene: field values pass through the shared secret redactor, string
values are bounded, container depth/size is bounded, and body-like keys
(prompts, messages, tool args/results) are replaced by a length + digest.
Every emitted record is also a Python ``logging`` record on
``machina.instrumentation`` and bumps the ``instrumentation.events`` counter.

If the sink cannot be opened the record still reaches the logger and the
counter; no instrumentation path raises into the caller.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import inspect
import json
import logging
import os
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from machina.memory.redaction import redact_secrets

__all__ = ["instrument", "instrumented", "log_event", "warm"]

logger = logging.getLogger("machina.instrumentation")
# A library logger must not fall through to logging.lastResort (which writes
# bare warning messages to stderr when the host configured no handlers).
logger.addHandler(logging.NullHandler())

# ─── bounds ─────────────────────────────────────────────────────────────────

#: Maximum characters kept for one string field (longer values are marked).
MAX_FIELD_CHARS = 512
#: Maximum entries walked in one container field.
MAX_CONTAINER_ITEMS = 50
#: Maximum nesting depth walked in a container field.
MAX_CONTAINER_DEPTH = 4
#: Rotation threshold for the dedicated JSONL sink (keep one previous file).
MAX_LOG_BYTES = 10 * 1024 * 1024
#: Logger name used for the parallel Python ``logging`` record.
LOGGER_NAME = "machina.instrumentation"
#: Metrics counter (the collector has no label dimension; one bounded family).
METRICS_COUNTER = "instrumentation.events"

_REQUESTED_FIELDS: tuple[str, ...] = (
    "ts",
    "event",
    "level",
    "pid",
    "component",
    "operation",
    "status",
)

#: Keys whose values are user/document/agent bodies. They are never written
#: raw: only a character count and a short digest are logged.
_BODY_KEYS = frozenset(
    {
        "args",
        "arguments",
        "body",
        "completion",
        "content",
        "contents",
        "doc",
        "document",
        "input",
        "message",
        "messages",
        "output",
        "payload",
        "prompt",
        "prompts",
        "query",
        "response",
        "result",
        "results",
        "script",
        "stderr",
        "stdout",
        "text",
        "transcript",
    }
)

_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "warn": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}

# ─── sink state ─────────────────────────────────────────────────────────────

_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, threading.Lock] = {}
#: Paths that already reported an open failure (warn once, not per event).
_REPORTED_FAILURES: set[str] = set()

try:  # pragma: no cover - platform dependent
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows
    _fcntl = None  # type: ignore[assignment]


def _lock_for(path: Path) -> threading.Lock:
    key = str(path)
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock


def _resolve_log_path() -> Path:
    explicit = os.environ.get("MACHINA_INSTRUMENTATION_LOG", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    home = os.environ.get("MACHINA_HOME", "").strip()
    root = Path(home).expanduser() if home else Path.home() / ".machina"
    stamp = datetime.now(UTC).strftime("%Y-%m-%d")
    return root / "logs" / f"instrumentation-{stamp}.jsonl"


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ─── sanitisation ───────────────────────────────────────────────────────────


def _redact_text(text: str) -> str:
    """Redact secrets, failing closed when the shared detector fails.

    Raw text must never be emitted merely because redaction broke; the
    marker is the only safe fallback.
    """
    try:
        return redact_secrets(text).text
    except Exception:
        return "[REDACTION_FAILED]"


def _bound_text(text: str) -> str:
    if len(text) <= MAX_FIELD_CHARS:
        return text
    return text[:MAX_FIELD_CHARS] + "...[truncated]"


def _digest(value: object) -> str:
    """Return a body-safe summary (length + sha256 prefix), never the value."""
    try:
        if isinstance(value, bytes):
            raw = value
        elif isinstance(value, str):
            raw = value.encode("utf-8", "replace")
        else:
            raw = repr(value).encode("utf-8", "replace")
        digest = hashlib.sha256(raw).hexdigest()[:16]
        return f"<redacted len={len(raw)} sha256={digest}>"
    except Exception:
        return "<redacted>"


def _is_body_key(key: str) -> bool:
    lowered = key.lower()
    return (
        lowered in _BODY_KEYS
        or lowered.endswith("_body")
        or lowered.endswith("_content")
        or lowered.endswith("_text")
        or lowered.endswith("_prompt")
    )


def _sanitize_value(key: str, value: object, depth: int = 0) -> object:
    """Return a JSON-safe, redacted, bounded representation of *value*."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if _is_body_key(key):
        return _digest(value)
    if isinstance(value, str):
        return _bound_text(_redact_text(value))
    if isinstance(value, bytes):
        return f"<bytes len={len(value)}>"
    if depth >= MAX_CONTAINER_DEPTH:
        return "<max-depth>"
    if isinstance(value, dict):
        out: dict[str, object] = {}
        for index, (item_key, item) in enumerate(value.items()):
            if index >= MAX_CONTAINER_ITEMS:
                out["...[truncated]"] = len(value) - MAX_CONTAINER_ITEMS
                break
            out[_bound_text(_redact_text(str(item_key)))] = _sanitize_value(
                str(item_key), item, depth + 1
            )
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        try:
            items = list(value)
        except Exception:
            return "<unserializable>"
        out_list: list[object] = []
        for index, item in enumerate(items):
            if index >= MAX_CONTAINER_ITEMS:
                out_list.append(f"...[truncated {len(items) - MAX_CONTAINER_ITEMS}]")
                break
            out_list.append(_sanitize_value(key, item, depth + 1))
        return out_list
    if isinstance(value, BaseException):
        return f"{type(value).__name__}: {_bound_text(_redact_text(str(value)))}"
    try:
        return _bound_text(_redact_text(str(value)))
    except Exception:
        return "<unserializable>"


def _sanitize_fields(fields: dict[str, Any]) -> dict[str, object]:
    safe: dict[str, object] = {}
    for raw_key, value in fields.items():
        try:
            key = str(raw_key)
            safe[_bound_text(_redact_text(key))] = _sanitize_value(key, value)
        except Exception:
            safe[str(raw_key)[:64]] = "<sanitization-failed>"
    return safe


def _correlation_fields() -> dict[str, str]:
    """Best-effort correlation ids from ContextVars owned by other modules.

    Modules are read through ``sys.modules`` so instrumentation never imports
    (or creates a cycle with) the agent/trace stacks, and a missing module is
    simply an absent field.
    """
    correlations: dict[str, str] = {}
    probes = (
        ("session_id", "machina.tools.agents", "current_session_id"),
        ("agent_id", "machina.tools.agents", "current_agent_id"),
        ("turn_id", "machina.observability.traces", "_active_turn"),
    )
    for field, module_name, attribute in probes:
        module = sys.modules.get(module_name)
        if module is None:
            continue
        variable = getattr(module, attribute, None)
        getter = getattr(variable, "get", None)
        if getter is None:
            continue
        try:
            value = getter()
        except Exception:
            continue
        if value:
            correlations[field] = str(value)
    return correlations


# ─── emission ───────────────────────────────────────────────────────────────


def _record(event: str, level: str, fields: dict[str, Any]) -> dict[str, object]:
    safe_fields = _sanitize_fields(fields)
    record: dict[str, object] = {}
    record.update(_correlation_fields())
    record.update(safe_fields)
    # Reserved keys always describe this record; caller fields cannot spoof
    # the schema (they might, however, collide harmlessly on ``status``).
    record.update(
        {
            "ts": _timestamp(),
            "event": str(event),
            "level": level,
            "pid": os.getpid(),
        }
    )
    return record


def _serialize(record: dict[str, object]) -> str:
    return json.dumps(record, ensure_ascii=False, default=str, separators=(",", ":")) + "\n"


def _write_line(path: Path, line: str) -> None:
    """Append one line to *path* under thread + file locks (best effort).

    Lock acquisition is bounded: instrumentation must never stall the caller
    behind another writer. A skipped record is preferable to a blocked turn.
    """
    lock = _lock_for(path)
    if not lock.acquire(timeout=0.05):
        return
    try:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            # The open below reports the real failure; a read-only parent is
            # still surfaced as a sink fallback instead of an exception.
            pass
        if path.exists():
            try:
                if path.stat().st_size + len(line.encode("utf-8")) > MAX_LOG_BYTES:
                    previous = path.with_name(path.name + ".1")
                    os.replace(path, previous)
            except OSError:
                pass
        fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            if _fcntl is not None:
                try:
                    # Non-blocking: another process writing must not stall us.
                    _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
                except OSError:
                    pass
            try:
                os.write(fd, line.encode("utf-8"))
            finally:
                if _fcntl is not None:
                    try:
                        _fcntl.flock(fd, _fcntl.LOCK_UN)
                    except OSError:
                        pass
        finally:
            os.close(fd)
    finally:
        lock.release()


def _report_sink_failure(path: Path, error: BaseException) -> None:
    key = str(path)
    with _LOCKS_GUARD:
        first = key not in _REPORTED_FAILURES
        if first:
            _REPORTED_FAILURES.add(key)
    if not first:
        return
    try:
        logger.warning(
            "instrumentation sink unavailable (%s); falling back to logger/metrics",
            type(error).__name__,
        )
    except Exception:
        pass


def _emit(event: str, level: str, fields: dict[str, Any]) -> None:
    """Emit one record; every path is fail-open."""
    try:
        # Deferred: instrumentation is imported by leaf modules that must run
        # with machina.core blocked (tools.base leaf-independence).
        from machina.core.metrics import metrics

        metrics.increment_counter(METRICS_COUNTER)
    except Exception:
        pass
    record: dict[str, object] = {}
    try:
        record = _record(event, level, fields)
    except Exception:
        record = {"ts": _timestamp(), "event": str(event), "level": level, "pid": os.getpid()}
    try:
        logger.log(_LEVELS.get(level, logging.INFO), str(event), extra={"fields": record})
    except Exception:
        pass
    path = None
    try:
        path = _resolve_log_path()
        _write_line(path, _serialize(record))
    except Exception as error:
        if path is not None:
            _report_sink_failure(path, error)


# ─── public API ─────────────────────────────────────────────────────────────


class _Span:
    """Context manager implementing instrumentation for one operation."""

    __slots__ = ("component", "operation", "fields", "_started", "_closed")

    def __init__(self, component: str, operation: str, fields: dict[str, Any]) -> None:
        self.component = component
        self.operation = operation
        self.fields = fields
        self._started = 0.0
        self._closed = False

    def __enter__(self) -> _Span:
        self._started = time.perf_counter()
        if not self._closed:
            self._emit("start", "debug", None)
        return self

    def note(self, **fields: Any) -> None:
        """Attach late-known fields (tokens, counts) to the terminal record."""
        if self._closed:
            return
        try:
            self.fields.update(fields)
        except Exception:
            pass

    def __exit__(self, exc_type, exc, traceback) -> bool:
        if self._closed:
            return False
        self._closed = True
        duration_ms = round((time.perf_counter() - self._started) * 1000, 3)
        if exc_type is None:
            self._emit("finish", "info", ("ok", duration_ms, {}))
        elif issubclass(exc_type, (asyncio.CancelledError, GeneratorExit)):
            self._emit("cancelled", "info", ("cancelled", duration_ms, {}))
        else:
            detail = {
                "error_class": exc_type.__name__,
                "error_message": _bound_text(_redact_text(str(exc))),
            }
            self._emit("error", "warning", ("error", duration_ms, detail))
        return False

    def _emit(
        self, suffix: str, level: str, terminal: tuple[str, float, dict[str, Any]] | None
    ) -> None:
        try:
            component = str(self.component)
            operation = str(self.operation)
            fields: dict[str, Any] = dict(self.fields)
            fields.update({"component": component, "operation": operation})
            if terminal is not None:
                status, duration_ms, detail = terminal
                fields.update({"status": status, "duration_ms": duration_ms})
                fields.update(detail)
            else:
                fields["status"] = "start"
            _emit(f"{component}.{operation}.{suffix}", level, fields)
        except Exception:
            # Instrumentation is observe-only: never surface a logging bug.
            pass


class _NullSpan:
    __slots__ = ()

    def __enter__(self) -> _NullSpan:
        return self

    def note(self, **fields: Any) -> None:
        return None

    def __exit__(self, exc_type, exc, traceback) -> bool:
        return False


def instrument(component: str, operation: str, **fields: Any) -> Any:
    """Instrument one operation as ``component.operation.{start,finish,…}``.

    Usage::

        with instrument("tool", "execute", tool=name, session_id=sid):
            ...

    The returned object also exposes ``note(**fields)`` for values that are
    only known at the end of the operation (token counts, result sizes).
    Exceptions are re-raised unchanged; logging failures are swallowed.
    """
    try:
        return _Span(str(component), str(operation), dict(fields))
    except Exception:
        return _NullSpan()


def log_event(event: str, *, level: str = "info", **fields: Any) -> None:
    """Emit one structured record for a point-in-time event (never raises)."""
    try:
        _emit(str(event), str(level).lower(), fields)
    except Exception:
        pass


_WARMED = False


def warm() -> None:
    """Pay one-time instrumentation costs (imports, redaction regexes) at startup.

    The first record would otherwise import the redaction machinery and metrics
    on the caller's thread (~100 ms), which can be a visible event-loop stall
    if it lands mid-turn. Startup hooks call this once, off the hot path; it is
    idempotent and fail-open.
    """
    global _WARMED
    if _WARMED:
        return
    _WARMED = True
    try:
        log_event("instrumentation.warm", level="debug")
    except Exception:
        pass


def instrumented(component: str, operation: str) -> Any:
    """Decorator form of :func:`instrument` for sync and async callables."""

    def decorator(func):
        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs):
                with instrument(component, operation):
                    return await func(*args, **kwargs)

            return async_wrapper

        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            with instrument(component, operation):
                return func(*args, **kwargs)

        return sync_wrapper

    return decorator


# ─── provider transport helper ───────────────────────────────────────────────
#
# Provider wiring lives here so ``machina/core/provider`` does not grow past
# its package ratchet. The decorated function is expected to take a ``client``
# (anything exposing ``base_url.host``) and a ``payload`` mapping; both are
# read for bounded metadata only — never message content.

_PROVIDER_HOSTS = (
    ("openrouter", "openrouter"),
    ("openai", "openai"),
    ("anthropic", "anthropic"),
    ("generativelanguage", "google"),
    ("googleapis", "google"),
    ("deepseek", "deepseek"),
    ("x.ai", "xai"),
    ("groq", "groq"),
    ("mistral", "mistral"),
    ("together", "together"),
    ("ollama", "ollama"),
    ("localhost", "local"),
    ("127.0.0.1", "local"),
    ("0.0.0.0", "local"),  # nosec B104 # Provider hostname classification, not a bind address.
)


def _provider_family(client: object) -> str:
    try:
        base_url = getattr(client, "base_url", None)
        host = str(getattr(base_url, "host", "") or "").lower()
    except Exception:
        return "unknown"
    for marker, family in _PROVIDER_HOSTS:
        if marker in host:
            return family
    return "other"


def _provider_fields(func: Any, args: tuple, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Bounded request metadata extracted from the decorated call."""
    try:
        bound = inspect.signature(func).bind_partial(*args, **kwargs)
        arguments = dict(bound.arguments)
    except Exception:
        arguments = dict(kwargs)
    client = arguments.get("client")
    if client is None and args:
        client = args[0]
    payload = arguments.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    model = payload.get("model")
    message_count = 0
    for key in ("messages", "input"):
        value = payload.get(key)
        if isinstance(value, list):
            message_count = len(value)
            break
    return {
        "model": str(model) if model is not None else "",
        "provider": _provider_family(client),
        "message_count": message_count,
    }


def _note_provider_response(span: Any, response: object, *, with_tokens: bool) -> None:
    try:
        status_code = getattr(response, "status_code", None)
        if isinstance(status_code, int):
            span.note(status_code=status_code)
        if not with_tokens:
            return
        data = response.json()
        usage = data.get("usage") if isinstance(data, dict) else None
        if not isinstance(usage, dict):
            return
        for field, keys in (
            ("prompt_tokens", ("prompt_tokens", "input_tokens")),
            ("completion_tokens", ("completion_tokens", "output_tokens")),
        ):
            for key in keys:
                value = usage.get(key)
                if isinstance(value, int) and not isinstance(value, bool):
                    span.note(**{field: value})
                    break
    except Exception:
        pass


def instrumented_provider(kind: str) -> Any:
    """Instrument a provider request/stream helper (see transport wiring).

    *kind* is ``"request"`` or ``"stream"``. Request calls get token counts
    from the returned response when available; streams record status only
    (the payload is never read). Body content is never logged.
    """

    def decorator(func):
        if inspect.isasyncgenfunction(func):

            @functools.wraps(func)
            async def gen_wrapper(*args, **kwargs):
                span = instrument(
                    "provider", kind, stream=True, **_provider_fields(func, args, kwargs)
                )
                with span:
                    async for item in func(*args, **kwargs):
                        _note_provider_response(span, item, with_tokens=False)
                        yield item

            return gen_wrapper

        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            span = instrument(
                "provider", kind, stream=False, **_provider_fields(func, args, kwargs)
            )
            with span:
                result = await func(*args, **kwargs)
                _note_provider_response(span, result, with_tokens=True)
                return result

        return wrapper

    return decorator
