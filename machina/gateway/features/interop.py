"""Interop harness handlers: detect, register, and delegate tasks.

Delegates directly to ``machina.interop`` in this process (that module is
stdlib-only and import-light; the child-process leaf binds lazily). Detection,
enable/disable and task delegation spawn platform CLIs under bounded
deadlines, so the blocking work runs on a worker thread — the gateway event
loop stays responsive for turns while a probe is in flight.

``interop.enable`` / ``interop.disable`` return the full ``ActionResult``
payload. A platform-side failure (``ok=False``) is reported honestly instead
of raised, so the UI can show the message, steps, and verification; only
client input errors (unknown platform, bad scope) use the shared
INVALID_PARAMS style.

The newer surface (:func:`interop_list`, :func:`interop_run`) drives the TUI
``/harnesses`` menu: list every supported harness with a runnability hint, and
run one prompt inside a harness. Both reach the frozen ``machina.interop``
run surface through module-level indirections (:func:`supported_runs`,
:func:`run_task`, :func:`state_path`) so this feature module imports and tests
without the optional run support present, and so stubbing them is the only
place a harness CLI could ever be spawned.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from machina.gateway.errors import INVALID_PARAMS, RpcError
from machina.gateway.features._common import _bool, _str
from machina.interop import (
    DetectResult,
    PlatformSpec,
    UnknownPlatform,
    detect,
    disable,
    enable,
    resolve_platform,
)

if TYPE_CHECKING:
    from machina.gateway.server import Gateway

#: Bounds for the ``interop.run`` payload (bounded output contract).
MAX_RUN_STDOUT_CHARS = 32_000
MAX_RUN_STDERR_CHARS = 8_000
MAX_RUN_PROMPT_CHARS = 8_000
MAX_RUN_TIMEOUT_SECONDS = 900.0
DEFAULT_RUN_TIMEOUT_SECONDS = 300.0

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")


def _platform(platform: str) -> PlatformSpec:
    """Resolve *platform* or raise the registered INVALID_PARAMS error."""
    try:
        return resolve_platform(platform)
    except UnknownPlatform as error:
        raise RpcError(INVALID_PARAMS, str(error)) from None


def supported_runs() -> dict[str, str]:
    """Platform -> human description of the prompt it runs (frozen H1 surface).

    Bound lazily so this module imports even when the run surface is absent;
    tests stub this name (the ``machina.interop`` external-process boundary).
    """
    from machina import interop as module

    return module.supported_runs()


def run_task(platform: str, prompt: str, *, timeout: float = DEFAULT_RUN_TIMEOUT_SECONDS) -> Any:
    """Delegate one prompt into *platform* (frozen H1 surface, bound lazily).

    Returns ``machina.interop``'s ``RunResult``; read field-by-field so a shape
    drift degrades to an honest empty result instead of an internal error.
    """
    from machina import interop as module

    return module.run_task(platform, prompt, timeout=timeout)


def state_path() -> Any:
    """The interop state file path (frozen H1 state reader, bound lazily)."""
    from machina import interop as module

    return module.state_path()


def _redact(text: str) -> str:
    """ANSI-stripped, secret-redacted text (never trust CLI output)."""
    text = _ANSI_RE.sub("", text or "").strip()
    try:
        from machina.memory.redaction import redact_secrets

        return redact_secrets(text).text
    except Exception:
        return text


def _bounded(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len("\n[truncated]"))] + "\n[truncated]"


def _run_scope(result: DetectResult) -> str | None:
    """Current registration scope, tolerating either detect shape."""
    scope = getattr(result, "scope", None)
    if scope:
        return str(scope)
    state_scope = getattr(result, "state_scope", None)
    if state_scope:
        return str(state_scope)
    scopes = getattr(result, "scopes", None)
    return ", ".join(scopes) if scopes else None


def _command_text(command: Any) -> str:
    if isinstance(command, (list, tuple)):
        return " ".join(str(part) for part in command)
    return "" if command is None else str(command)


def _exit_code(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _run_timeout(params: dict[str, Any]) -> float:
    value = params.get("timeout")
    if value is None:
        return DEFAULT_RUN_TIMEOUT_SECONDS
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RpcError(INVALID_PARAMS, "timeout must be a number of seconds")
    timeout = float(value)
    if not 1.0 <= timeout <= MAX_RUN_TIMEOUT_SECONDS:
        raise RpcError(
            INVALID_PARAMS,
            f"timeout must be between 1 and {int(MAX_RUN_TIMEOUT_SECONDS)} seconds",
        )
    return timeout


def _row(result: DetectResult) -> dict[str, Any]:
    """One explicit JSON-safe status row (no binary paths, no secrets)."""
    scope = result.state_scope or (", ".join(result.scopes) if result.scopes else None)
    return {
        "platform": result.platform,
        "label": result.label,
        "installed": bool(result.installed),
        "version": result.version,
        "running": bool(result.running),
        "registered": bool(result.registered),
        "enabled": None if result.enabled is None else bool(result.enabled),
        "scope": scope,
        "notes": list(result.notes),
    }


async def interop_status(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Read-only detection of every supported harness (threaded; bounded)."""
    results = await asyncio.to_thread(detect)
    return {"platforms": [_row(result) for result in results.values()]}


async def interop_enable(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Idempotently register Machina's MCP server with one harness."""
    spec = _platform(_str(params, "platform", max_len=32))
    scope = _str(params, "scope", required=False, max_len=16).lower() or "project"
    if scope not in ("project", "global"):
        raise RpcError(INVALID_PARAMS, "scope must be project|global")
    dry_run = _bool(params, "dry_run")
    result = await asyncio.to_thread(enable, spec.key, scope=scope, dry_run=dry_run)
    return asdict(result)


async def interop_disable(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Remove the registration where the platform CLI supports it."""
    spec = _platform(_str(params, "platform", max_len=32))
    result = await asyncio.to_thread(disable, spec.key)
    return asdict(result)


async def interop_list(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Every supported harness with its live state and runnability hint.

    One on-demand detection pass (bounded probes run on a worker thread), plus
    the frozen ``supported_runs`` map deciding which harnesses the UI may offer
    to run a task in. ``stateFile`` is the shared interop state path so the TUI
    can show where registration state lives.
    """
    results, runs = await asyncio.to_thread(lambda: (detect(), supported_runs()))
    harnesses = []
    for result in results.values():
        hint = runs.get(result.platform)
        harnesses.append(
            {
                "key": result.platform,
                "label": result.label,
                "installed": bool(result.installed),
                "version": result.version,
                "running": bool(result.running),
                "registered": bool(result.registered),
                "scope": _run_scope(result),
                "runSupported": hint is not None,
                "runHint": hint,
            }
        )
    state_file = await asyncio.to_thread(state_path)
    return {"harnesses": harnesses, "stateFile": str(state_file) if state_file else None}


async def interop_run(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Delegate one prompt into an external harness, bounded and redacted.

    The blocking child process runs on a worker thread. The returned payload is
    intentionally narrow — ``ok``, ``exitCode``, ``command``, ``stdout``,
    ``stderr`` and an optional ``unsupportedReason`` — with stdout/stderr
    capped and both redacted so CLI diagnostics cannot leak a credential.
    """
    spec = _platform(_str(params, "platform", max_len=32))
    prompt = _str(params, "prompt", max_len=MAX_RUN_PROMPT_CHARS)
    timeout = _run_timeout(params)
    result = await asyncio.to_thread(run_task, spec.key, prompt, timeout=timeout)
    unsupported = getattr(result, "unsupported_reason", None)
    return {
        "ok": bool(getattr(result, "ok", False)),
        "exitCode": _exit_code(getattr(result, "exit_code", None)),
        "command": _redact(_command_text(getattr(result, "command", None))),
        "stdout": _bounded(_redact(getattr(result, "stdout", "") or ""), MAX_RUN_STDOUT_CHARS),
        "stderr": _bounded(_redact(getattr(result, "stderr", "") or ""), MAX_RUN_STDERR_CHARS),
        "unsupportedReason": str(unsupported) if unsupported else None,
    }
