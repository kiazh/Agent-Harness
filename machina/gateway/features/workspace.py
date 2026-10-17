"""Workspace feature handlers (plan §19) — thin, strict RPC surface.

Validation rules:

* All params are untrusted input. Types and lengths are validated here before
  anything reaches :class:`~machina.workspace.service.WorkspaceService`.
* Any ``actor``/``role``/``user`` field a caller sends is ignored: the
  authenticated gateway token is the only user authority (plan §19) and the
  workspace client binding is minted server-side. File authorization is
  resolved-path containment, never a textual path prefix.
* Read/list/save refusals are structured results (``{"refused": ...}`` /
  ``{"ok": false, "refused": ...}``) per the frozen protocol, not crashes.
  Unknown workspace ids are ``NOT_FOUND``; malformed params are
  ``INVALID_PARAMS``.
* The service is created once per gateway and stored on ``gw.workspace_service``
  (lazy factory below). Workspace events are wrapped here in the JSON-RPC
  ``event`` notification frame — they are a separate family from the chat
  stream and never carry ``sessionId``/``turnId``.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from machina.gateway.errors import INVALID_PARAMS, NOT_FOUND, RpcError
from machina.gateway.features._common import _bool, _int
from machina.workspace.lsp import LspManager
from machina.workspace.models import (
    DEFAULT_LIST_LIMIT,
    DEFAULT_QUICK_OPEN_LIMIT,
    INTELLIGENCE_OPERATIONS,
    MAX_CHANGE_TEXT_BYTES,
    MAX_CHANGES_PER_REQUEST,
    MAX_PATH_CHARS,
    MAX_QUERY_CHARS,
    MAX_QUICK_OPEN_LIMIT,
    MAX_QUICK_OPEN_QUERY_CHARS,
    MAX_REVISION_CHARS,
    MAX_SAVE_PARAM_CHARS,
    MAX_URI_CHARS,
    WorkspaceError,
)
from machina.workspace.service import MANAGER_STOP_TIMEOUT, WorkspaceService

if TYPE_CHECKING:
    from machina.gateway.server import Gateway

logger = logging.getLogger(__name__)

_MAX_WORKSPACE_ID_CHARS = 128
_MAX_CLIENT_ID_CHARS = 128
_MAX_PROFILE_CHARS = 128
_MAX_LANGUAGE_ID_CHARS = 128
_MAX_ACTION_CHARS = 32
_MAX_INT32 = 2_147_483_647


def _service(gw: Gateway) -> WorkspaceService:
    """Lazy per-gateway factory (one service instance per Gateway)."""
    service = getattr(gw, "workspace_service", None)
    if not isinstance(service, WorkspaceService):
        service = WorkspaceService(send_event=lambda event: _write_event(gw, event))
        gw.workspace_service = service
    return service


def _retired_stop_tasks(gw: Gateway) -> set[asyncio.Task[Any]]:
    """Per-gateway set of in-flight retired-manager stops (lazily created)."""
    tasks = getattr(gw, "_workspace_lsp_stop_tasks", None)
    if not isinstance(tasks, set):
        tasks = set()
        gw._workspace_lsp_stop_tasks = tasks
    return tasks


async def _await_if_needed(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _run_manager_stop(stop_all: Callable[[], Any]) -> None:
    try:
        await _await_if_needed(stop_all())
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("retired language-server manager stop failed", exc_info=True)


def _retire_manager(gw: Gateway, manager: Any) -> asyncio.Task[Any] | None:
    """Detach a retired manager's ``stop_all``; retained for the shutdown drain.

    The task runs to completion on its own; nothing here cancels it.
    """
    stop_all = getattr(manager, "stop_all", None)
    if not callable(stop_all):
        return None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None  # no event loop: there is no async transport to stop
    task = loop.create_task(_run_manager_stop(stop_all), name="workspace-lsp-retire")
    tasks = _retired_stop_tasks(gw)
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return task


async def _retire_manager_bounded(gw: Gateway, manager: Any) -> None:
    """Observe a retired manager's stop, bounded, without ever cancelling it.

    On timeout the stop keeps running in the background and stays retained on
    the gateway so :func:`drain_retired_lsp_managers` can join it at shutdown.
    """
    task = _retire_manager(gw, manager)
    if task is None:
        return
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(asyncio.shield(task), timeout=MANAGER_STOP_TIMEOUT)
    if not task.done():
        logger.warning(
            "language-server stop exceeded %.1fs; continuing in background",
            MANAGER_STOP_TIMEOUT,
        )


async def retire_active_lsp_manager(gw: Gateway) -> None:
    """Retire and boundedly stop the gateway's currently active manager.

    ``Gateway.close`` and ``workspace.close`` both need this: the active
    manager is only moved into the retired-stop set when a *new* workspace
    open replaces it, so shutdown otherwise left its child language servers
    running (combined #13 / original #40).
    """
    manager = getattr(gw, "_workspace_lsp_manager", None)
    if manager is None:
        return
    gw._workspace_lsp_manager = None
    service = getattr(gw, "workspace_service", None)
    if service is not None:
        try:
            service.set_lsp_manager(None)
        except Exception:
            logger.debug("workspace service refused LSP detach", exc_info=True)
    await _retire_manager_bounded(gw, manager)


async def drain_retired_lsp_managers(
    gw: Gateway, *, timeout: float = MANAGER_STOP_TIMEOUT
) -> None:
    """Bounded, non-cancelling join of retired managers' stops (shutdown hook).

    A stop still running when the bound elapses is left detached and logged,
    never cancelled mid-shutdown.
    """
    tasks = [task for task in _retired_stop_tasks(gw) if not task.done()]
    if not tasks:
        return
    try:
        _done, pending = await asyncio.wait(tasks, timeout=timeout)
    except Exception:
        logger.warning("language-server retirement drain failed", exc_info=True)
        return
    if pending:
        logger.warning(
            "language-server retirement still running after %.1fs; leaving it detached",
            timeout,
        )


async def _attach_lsp_manager(gw: Gateway, service: WorkspaceService, identity: Any) -> None:
    """Composition seam for the real LspManager (T7 integration point).

    Without this, the service has no manager in production and reports
    "no language servers" forever even when profiles resolve. Creating the
    manager is cheap; servers still start lazily on the first ``documentOpen``
    for a language. Trust governs language servers (plan §14): an untrusted
    open detaches any manager. A previous manager for the same gateway is
    retired bounded before a new root rebinds it.
    """
    previous = getattr(gw, "_workspace_lsp_manager", None)
    if previous is not None:
        # Bound the retirement: a hanging stop_all must never block a reopen
        # indefinitely, but it must also never be cancelled — the stop keeps
        # running in a detached task retained on the gateway for the shutdown
        # drain (the new generation attaches regardless; status stays truthful).
        await _retire_manager_bounded(gw, previous)
        gw._workspace_lsp_manager = None
    if not identity.trusted:
        service.set_lsp_manager(None)
        return
    manager = LspManager(identity.resolved_root)
    gw._workspace_lsp_manager = manager
    service.set_lsp_manager(manager)


def _write_event(gw: Gateway, event: dict[str, Any]) -> None:
    try:
        gw._write({"jsonrpc": "2.0", "method": "event", "params": event})
    except Exception:  # a dead channel only drops the frame
        logger.debug("workspace event write failed", exc_info=True)


def _require(gw: Gateway, params: dict[str, Any]) -> WorkspaceService:
    """Resolve the open workspace; wrong/absent ids fail closed as NOT_FOUND."""
    service = _service(gw)
    workspace_id = _raw_str(params, "workspaceId", max_len=_MAX_WORKSPACE_ID_CHARS)
    if service.workspace_id is None or workspace_id != service.workspace_id:
        raise RpcError(NOT_FOUND, "workspace is not open")
    return service


def _raw_str(
    params: dict[str, Any],
    key: str,
    *,
    required: bool = True,
    max_len: int,
    default: str = "",
) -> str:
    """Type/length-checked string without stripping.

    Paths, URIs, text and revisions may contain significant leading/trailing
    whitespace, so unlike ``_common._str`` this helper never strips; it only
    enforces type, presence and a bound.
    """
    value = params.get(key, default)
    if value is None:
        if required:
            raise RpcError(INVALID_PARAMS, f"{key} is required")
        return default
    if not isinstance(value, str):
        raise RpcError(INVALID_PARAMS, f"{key} must be a string")
    if len(value) > max_len:
        raise RpcError(INVALID_PARAMS, f"{key} is too long (max {max_len} characters)")
    if required and not value:
        raise RpcError(INVALID_PARAMS, f"{key} is required")
    return value


def _required_int(params: dict[str, Any], key: str, lo: int, hi: int) -> int:
    value = params.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
        raise RpcError(INVALID_PARAMS, f"{key} must be an integer between {lo} and {hi}")
    return value


def _optional_int(params: dict[str, Any], key: str, lo: int, hi: int) -> int | None:
    value = params.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
        raise RpcError(INVALID_PARAMS, f"{key} must be an integer between {lo} and {hi}")
    return value


def _epoch(params: dict[str, Any]) -> int | None:
    """The caller's workspace epoch (absence is fenced by the service).

    Mutating routes require it: the service treats ``None`` as a denial for
    those operations, surfacing a structured ``refused`` result. Values are
    still type/range checked here because params are untrusted input.
    """
    return _optional_int(params, "epoch", 0, _MAX_INT32)


def _client_id(params: dict[str, Any]) -> str | None:
    """Optional client binding for a mutating route (absent on stdio)."""
    return _raw_str(params, "clientId", required=False, max_len=_MAX_CLIENT_ID_CHARS) or None


def _position(value: Any, label: str) -> dict[str, int]:
    if not isinstance(value, dict):
        raise RpcError(INVALID_PARAMS, f"{label} must be an object with line/character")
    line = value.get("line")
    character = value.get("character")
    for key, number in (("line", line), ("character", character)):
        if not isinstance(number, int) or isinstance(number, bool) or number < 0:
            raise RpcError(INVALID_PARAMS, f"{label}.{key} must be a non-negative integer")
    return {"line": line, "character": character}


def _changes(params: dict[str, Any]) -> list[dict[str, Any]]:
    changes = params.get("changes")
    if not isinstance(changes, list):
        raise RpcError(INVALID_PARAMS, "changes must be an array")
    if len(changes) > MAX_CHANGES_PER_REQUEST:
        raise RpcError(
            INVALID_PARAMS, f"changes is too long (max {MAX_CHANGES_PER_REQUEST} entries)"
        )
    validated: list[dict[str, Any]] = []
    total_bytes = 0
    for index, change in enumerate(changes):
        if not isinstance(change, dict):
            raise RpcError(INVALID_PARAMS, f"changes[{index}] must be an object")
        span = change.get("range")
        if not isinstance(span, dict):
            raise RpcError(INVALID_PARAMS, f"changes[{index}].range must be an object")
        text = change.get("text")
        if not isinstance(text, str):
            raise RpcError(INVALID_PARAMS, f"changes[{index}].text must be a string")
        try:
            total_bytes += len(text.encode("utf-8"))
        except UnicodeEncodeError as error:
            raise RpcError(
                INVALID_PARAMS, f"changes[{index}].text is not encodable as UTF-8"
            ) from error
        validated.append(
            {
                "range": {
                    "start": _position(span.get("start"), f"changes[{index}].range.start"),
                    "end": _position(span.get("end"), f"changes[{index}].range.end"),
                },
                "text": text,
            }
        )
    if total_bytes > MAX_CHANGE_TEXT_BYTES:
        raise RpcError(INVALID_PARAMS, f"changes text exceeds {MAX_CHANGE_TEXT_BYTES} bytes")
    return validated


def _refusal(exc: WorkspaceError) -> dict[str, Any]:
    return exc.as_refusal()


# ─── methods ────────────────────────────────────────────────────────────────
async def workspace_open(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Open a workspace root with an explicit trust decision.

    ``params.actor``/``params.role`` (if sent) are intentionally ignored: the
    gateway token authenticates the human UI, and the workspace identity is
    minted server-side. Trust itself is the parameter the user must state.
    """
    root = _raw_str(params, "root", max_len=MAX_PATH_CHARS)
    if "trust" not in params:
        raise RpcError(INVALID_PARAMS, "trust is required (an explicit trust decision)")
    trust = _bool(params, "trust")
    service = _service(gw)
    try:
        identity = service.open(root, trust)
    except WorkspaceError as exc:
        raise RpcError(INVALID_PARAMS, exc.message) from exc
    await _attach_lsp_manager(gw, service, identity)
    return identity.as_dict()


async def workspace_list(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    service = _require(gw, params)
    path = _raw_str(params, "path", required=False, max_len=MAX_PATH_CHARS)
    cursor = params.get("cursor")
    if cursor is not None:
        if (
            not isinstance(cursor, str)
            or not cursor.isascii()
            or not cursor.isdigit()
            or len(cursor) > 12
        ):
            raise RpcError(INVALID_PARAMS, "cursor must be a non-negative integer string")
    limit = _int(params, "limit", DEFAULT_LIST_LIMIT, 1, 200)
    include_hidden = _bool(params, "includeHidden")
    try:
        page = service.list(path, cursor=cursor, limit=limit, include_hidden=include_hidden)
    except WorkspaceError as exc:
        return _refusal(exc)
    return page.as_dict()


async def workspace_read(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    service = _require(gw, params)
    uri = _raw_str(params, "uri", max_len=MAX_URI_CHARS)
    try:
        result = service.read(uri)
    except WorkspaceError as exc:
        return _refusal(exc)
    return result.as_dict()


async def workspace_save(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    service = _require(gw, params)
    uri = _raw_str(params, "uri", max_len=MAX_URI_CHARS)
    text = params.get("text")
    if not isinstance(text, str):
        raise RpcError(INVALID_PARAMS, "text must be a string")
    if len(text) > MAX_SAVE_PARAM_CHARS:
        raise RpcError(INVALID_PARAMS, f"text is too long (max {MAX_SAVE_PARAM_CHARS} characters)")
    expected_revision = (
        _raw_str(params, "expectedRevision", required=False, max_len=MAX_REVISION_CHARS) or None
    )
    ui_version = _optional_int(params, "uiVersion", 0, _MAX_INT32)
    epoch = _epoch(params)
    client_id = _client_id(params)
    try:
        result = await service.save(
            uri,
            text,
            expected_revision,
            ui_version=ui_version,
            epoch=epoch,
            client_id=client_id,
        )
    except WorkspaceError as exc:
        return {"ok": False, **_refusal(exc)}
    return result.as_dict()


async def workspace_document_open(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    service = _require(gw, params)
    uri = _raw_str(params, "uri", max_len=MAX_URI_CHARS)
    language_id = (
        _raw_str(params, "languageId", required=False, max_len=_MAX_LANGUAGE_ID_CHARS)
        or "plaintext"
    )
    version = _required_int(params, "version", 0, _MAX_INT32)
    text = params.get("text", "")
    if not isinstance(text, str):
        raise RpcError(INVALID_PARAMS, "text must be a string")
    if len(text) > MAX_SAVE_PARAM_CHARS:
        raise RpcError(INVALID_PARAMS, f"text is too long (max {MAX_SAVE_PARAM_CHARS} characters)")
    epoch = _epoch(params)
    try:
        result = service.document_open(
            uri, language_id, version, text, epoch=epoch, client_id=_client_id(params)
        )
    except WorkspaceError as exc:
        return _refusal(exc)
    return result.as_dict()


async def workspace_document_change(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    service = _require(gw, params)
    uri = _raw_str(params, "uri", max_len=MAX_URI_CHARS)
    base_version = _required_int(params, "baseVersion", 0, _MAX_INT32)
    version = _required_int(params, "version", 0, _MAX_INT32)
    changes = _changes(params)
    epoch = _epoch(params)
    try:
        result = service.document_change(
            uri, base_version, version, changes, epoch=epoch, client_id=_client_id(params)
        )
    except WorkspaceError as exc:
        return _refusal(exc)
    return result.as_dict()


async def workspace_document_close(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    service = _require(gw, params)
    uri = _raw_str(params, "uri", max_len=MAX_URI_CHARS)
    epoch = _epoch(params)
    try:
        return service.document_close(uri, epoch=epoch, client_id=_client_id(params))
    except WorkspaceError as exc:
        return _refusal(exc)


async def workspace_quick_open(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Bounded ranked file search over the background Quick Open index.

    ``query`` is optional (an empty query lists what is indexed so far),
    ``limit`` bounds the returned rows (1..200, default 50), ``epoch`` lets a
    stale client fail closed, and ``rebuild`` explicitly restarts the index
    scan. Partial/truncated/stale answers are structured result fields, never
    errors: the client must be able to disclose them.
    """
    service = _require(gw, params)
    query = _raw_str(params, "query", required=False, max_len=MAX_QUICK_OPEN_QUERY_CHARS)
    limit = _int(params, "limit", DEFAULT_QUICK_OPEN_LIMIT, 1, MAX_QUICK_OPEN_LIMIT)
    epoch = _optional_int(params, "epoch", 0, _MAX_INT32)
    rebuild = _bool(params, "rebuild")
    try:
        result = await service.quick_open(query, limit, epoch=epoch, rebuild=rebuild)
    except WorkspaceError as exc:
        return _refusal(exc)
    return result.as_dict()


async def workspace_quick_open_cancel(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Cancel an in-flight recursive index scan (a no-op when none runs)."""
    service = _require(gw, params)
    return service.quick_open_cancel()


async def workspace_intelligence(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    service = _require(gw, params)
    operation = _raw_str(params, "operation", max_len=64)
    if operation not in INTELLIGENCE_OPERATIONS:
        raise RpcError(
            INVALID_PARAMS,
            f"operation must be one of: {', '.join(INTELLIGENCE_OPERATIONS)}",
        )
    uri = _raw_str(params, "uri", required=False, max_len=MAX_URI_CHARS) or None
    version = _optional_int(params, "version", 0, _MAX_INT32)
    query = _raw_str(params, "query", required=False, max_len=MAX_QUERY_CHARS) or None
    position = None
    if params.get("position") is not None:
        position = _position(params.get("position"), "position")
    epoch = _optional_int(params, "epoch", 0, _MAX_INT32)
    try:
        result = await service.intelligence(
            operation,
            uri=uri,
            version=version,
            position=position,
            query=query,
            epoch=epoch,
        )
    except WorkspaceError as exc:
        return _refusal(exc)
    return result.as_dict()


async def workspace_lsp_status(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    service = _require(gw, params)
    try:
        return await service.lsp_status()
    except WorkspaceError as exc:
        return _refusal(exc)


async def workspace_lsp_control(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    service = _require(gw, params)
    action = _raw_str(params, "action", max_len=_MAX_ACTION_CHARS)
    if action not in ("start", "stop", "restart"):
        raise RpcError(INVALID_PARAMS, "action must be start, stop or restart")
    profile = _raw_str(params, "profile", max_len=_MAX_PROFILE_CHARS)
    try:
        return await service.lsp_control(
            action, profile, epoch=_epoch(params), client_id=_client_id(params)
        )
    except WorkspaceError as exc:
        return _refusal(exc)


async def workspace_close(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    service = _require(gw, params)
    try:
        return service.close()
    finally:
        # The active manager is not in the retired-stop set until shutdown;
        # retire it here so `/workspace close` also stops its child servers.
        await retire_active_lsp_manager(gw)
