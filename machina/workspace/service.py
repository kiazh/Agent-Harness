"""WorkspaceService — identity, file access, mirrors, LSP sync and intelligence.

One service instance per gateway (plan §19). ``open(root, trust)`` mints a
backend-issued workspace identity bound to the resolved root: ``workspaceId``,
``clientId`` and a monotonic ``epoch``. The epoch accompanies every mirror,
event and intelligence result so a result from an old workspace can never be
mistaken for the new one.

Language servers (``machina/workspace/lsp.py``, T7) are injected with
:meth:`set_lsp_manager`. ``open`` only *learns* which profiles resolve — it
never spawns. The first ``documentOpen`` for a language starts its server
lazily; ``lspControl`` starts/stops/restarts explicitly. When no manager (or
no server) can answer, results are truthful ``missing``/``failed``/``pending``
states instead of fabricated ones (plan §13/§16/§19).

Ordering invariants encoded here:

* mirror accepted → lazy start → bounded ready wait → ``didOpen``; the
  connection's single ordered write queue then preserves that order for every
  later frame;
* ``documentChange`` → mirror accepted → ``didChange`` (full snapshot or the
  validated incremental ranges per the initialize result) *only* against the
  generation that received the ``didOpen``; a change accepted while the
  server is not ready stays in the mirror and is re-synchronized by the
  ``on_ready`` reopen (never a ``didChange`` against a dead generation);
* ``intelligence`` flushes the document's pending sync chain before issuing
  its request, so the server always observes ``didChange`` before the request
  that depends on it;
* ``save`` order: confirmed disk save → pending ``didChange`` flush →
  ``didSave`` (includeText per server capabilities); the RPC returns at the
  commit and the flush runs detached when a chain is in flight, while
  ``workspace.fileChanged`` follows the confirmed commit;
* diagnostics from a generation other than the live one are never served; a
  crashed server's last set is surfaced as ``stale`` (kept, not silently
  cleared and never presented as current).

Workspace events are a separate family from the chat stream: they carry
``workspaceId``/``epoch`` and never ``sessionId``/``turnId``. The injected
``send_event`` callback receives the bare event payload; the gateway feature
layer wraps it in the JSON-RPC notification frame.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import os
import secrets
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from machina.workspace import lsp as lsp_module
from machina.workspace import quickopen as quickopen_module
from machina.workspace.documents import DocumentMirrorStore
from machina.workspace.files import WorkspaceFiles
from machina.workspace.intelligence import (
    DEFAULT_INTELLIGENCE_TIMEOUTS,
    MAX_DIAGNOSTICS_PER_FILE,
    POSITION_OPERATIONS,
    DiagnosticsStore,
    capability_summary,
    language_for_profile,
    lsp_request_for,
    lsp_sync_kind,
    normalize_for,
    sanitize_log_text,
)
from machina.workspace.lsp import (
    STATE_FAILED,
    STATE_INITIALIZING,
    STATE_MISSING,
    STATE_READY,
    STATE_STARTING,
    STATE_STOPPED,
    STATE_STOPPING,
    LspError,
    LspRequestError,
    LspTimeoutError,
)
from machina.workspace.models import (
    DEFAULT_LIST_LIMIT,
    DEFAULT_QUICK_OPEN_LIMIT,
    INTELLIGENCE_OPERATIONS,
    LSP_PROFILE_LANGUAGES,
    MAX_POSITION_VALUE,
    MAX_QUICK_OPEN_LIMIT,
    MAX_QUICK_OPEN_QUERY_CHARS,
    MAX_READ_BYTES,
    MAX_URI_CHARS,
    QUICK_OPEN_PARTIAL_WAIT,
    DocumentChangeResult,
    DocumentOpenResult,
    IntelligenceResult,
    ListPage,
    QuickOpenResult,
    ReadResult,
    SaveResult,
    WorkspaceError,
    WorkspaceIdentity,
    diagnostics_event,
    file_changed_event,
    index_progress_event,
    language_id_for,
    lsp_language_for,
    lsp_state_event,
)

logger = logging.getLogger(__name__)

SendEvent = Callable[[dict[str, Any]], None]

_BASE_CAPABILITIES = (
    "list",
    "read",
    "save",
    "documentOpen",
    "documentChange",
    "documentClose",
    "close",
)
_TRUSTED_CAPABILITIES = ("intelligence", "lspStatus", "lspControl")

#: Operations the bounded agent-facing ``code_intelligence`` tool exposes
#: (plan §20). Single source for the tool schema enum, the self-knowledge
#: brief and its tests: tool modules may import this, and ``machina.core``
#: may import it, so the three surfaces cannot drift apart.
CODE_INTELLIGENCE_OPERATIONS = (
    "diagnostics",
    "definition",
    "references",
    "symbols",
    "hover",
)

#: How long a requested didOpen may wait for the handshake before reporting a
#: truthful ``starting``/``initializing`` (the reopen hook completes it later).
SYNC_READY_TIMEOUT = 10.0

#: Bounded stop of the manager's servers on ``close``.
MANAGER_STOP_TIMEOUT = 15.0


async def _await_if_needed(value: Any) -> Any:
    """Await a manager call only when it is actually awaitable."""
    if inspect.isawaitable(value):
        return await value
    return value


@dataclass
class _TrackedConnection:
    """A connection the service observed and therefore keeps bookkeeping for."""

    connection: Any
    language: str
    epoch: int


@dataclass
class _OpenRecord:
    """What the server was last told about one open document.

    ``generation`` scopes it to the process generation that received the
    ``didOpen``; a generation mismatch means the server knows nothing about
    this document any more and a full reopen is required.
    """

    language: str
    version: int
    generation: int


class WorkspaceService:
    """Bounds and structured outcomes for one authorized workspace identity."""

    #: Lazily created Quick Open scan owner for the live (root, epoch) binding.
    #: Deliberately not created in ``__init__``: the quick-open seam is strictly
    #: additive on this service.
    _quick_indexer: quickopen_module.QuickOpenIndexer | None

    def __init__(
        self,
        *,
        send_event: SendEvent | None = None,
        sync_ready_timeout: float = SYNC_READY_TIMEOUT,
        intelligence_timeouts: Mapping[str, float] | None = None,
        manager_stop_timeout: float = MANAGER_STOP_TIMEOUT,
    ) -> None:
        self._send_event: SendEvent = send_event if send_event is not None else (lambda _e: None)
        self._lsp_manager: Any | None = None
        self._files: WorkspaceFiles | None = None
        self._documents: DocumentMirrorStore | None = None
        self._identity: WorkspaceIdentity | None = None
        self._epoch = 0
        self._generation = 0
        self._sync_ready_timeout = max(0.1, float(sync_ready_timeout))
        self._manager_stop_timeout = max(0.1, float(manager_stop_timeout))
        # start/stop/restart may include a full stop + initialize cycle.
        self._control_timeout = max(30.0, self._sync_ready_timeout + 15.0)
        self._intelligence_timeouts = dict(DEFAULT_INTELLIGENCE_TIMEOUTS)
        for operation, seconds in (intelligence_timeouts or {}).items():
            if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) and seconds > 0:
                self._intelligence_timeouts[operation] = float(seconds)
        self._profiles: dict[str, bool] = {}
        self._connections: dict[str, _TrackedConnection] = {}
        # Subscription bookkeeping, by object identity: a connection/manager is
        # observed once for the service lifetime (handlers re-check epoch and
        # tracking before acting), so no duplicate events can be emitted.
        self._state_subscribed: list[Any] = []
        self._ready_subscribed: list[Any] = []
        self._open_records: dict[str, _OpenRecord] = {}
        self._sync_tasks: dict[str, asyncio.Task[None]] = {}
        self._sync_locks: dict[str, asyncio.Lock] = {}
        # Detached flush-then-didSave chains for confirmed saves (never awaited
        # by the save RPC; cancelled only when the workspace epoch resets).
        self._save_tasks: set[asyncio.Task[None]] = set()
        self._diagnostics = DiagnosticsStore()
        self._stop_task: asyncio.Task[None] | None = None
        # The manager whose bounded stop owns ``_stop_task`` (kept even when a
        # reopen detaches ``_lsp_manager`` so escalation targets the right
        # processes).
        self._stopping_manager: Any | None = None
        # The manager close() retired: stop_all closes it permanently, so a
        # later open() must not pretend a fresh server can come from it.
        self._closing_manager: Any | None = None

    # ─── identity ───────────────────────────────────────────────────────────
    @property
    def workspace_id(self) -> str | None:
        return self._identity.workspace_id if self._identity is not None else None

    @property
    def epoch(self) -> int:
        return self._epoch

    @property
    def client_generation(self) -> int:
        return self._generation

    @property
    def trusted(self) -> bool:
        return bool(self._identity is not None and self._identity.trusted)

    def set_lsp_manager(self, manager: Any | None) -> None:
        """Inject the optional language-server manager (T7 integration point).

        A different manager invalidates every tracked connection, open record
        and stored diagnostic (they belonged to the previous manager's
        processes); the ``on_ready`` hook is subscribed once per manager so a
        restart can reopen every mirror with its latest snapshot.
        """
        self._lsp_manager = manager
        if manager is not self._closing_manager:
            self._closing_manager = None
        self._reset_lsp_state()
        if manager is None:
            return
        on_ready = getattr(manager, "on_ready", None)
        if callable(on_ready) and not any(
            existing is manager for existing in self._ready_subscribed
        ):
            self._ready_subscribed.append(manager)
            on_ready(self._handle_connection_ready)

    def open(self, root: str, trust: bool) -> WorkspaceIdentity:
        """Open (or reopen) the workspace; explicit trust decision required.

        Reopening mints a fresh identity and epoch and drops all mirrors, so a
        stale client can no longer write documents or reads into the new
        workspace binding. No language server is started here: ``open`` only
        learns which profiles currently resolve; the first ``documentOpen``
        for a language starts it lazily.
        """
        if not isinstance(trust, bool):
            raise WorkspaceError("denied", "an explicit boolean trust decision is required")
        files = WorkspaceFiles(root)
        if self._lsp_manager is not None and self._lsp_manager is self._closing_manager:
            # The previous close() retired this manager (its stop_all closed it
            # permanently): honest unavailability beats a fake start.
            self._lsp_manager = None
            self._closing_manager = None
        self._epoch += 1
        self._generation += 1
        capabilities = list(_BASE_CAPABILITIES)
        if trust:
            capabilities.extend(_TRUSTED_CAPABILITIES)
        self._identity = WorkspaceIdentity(
            workspace_id=f"ws_{secrets.token_hex(8)}",
            client_id=f"cl_{secrets.token_hex(8)}",
            epoch=self._epoch,
            root_label=files.root.name or str(files.root),
            resolved_root=str(files.root),
            trusted=trust,
            capabilities=tuple(capabilities),
        )
        self._files = files
        self._documents = DocumentMirrorStore(epoch=self._epoch, generation=self._generation)
        self._reset_lsp_state()
        self._profiles = {
            language: lsp_module.resolve_profile(language) is not None
            for language in LSP_PROFILE_LANGUAGES
        }
        # T10 (plan §20): the process-wide "the app opened this workspace"
        # registry (one workspace per process, v1). Last open wins; close only
        # clears when this service is still the active one.
        set_active_workspace_service(self)
        return self._identity

    def close(self) -> dict[str, Any]:
        """Retire mirrors/identity and request a bounded language-server stop.

        Servers are stopped through the injected manager's ``stop_all`` under a
        deadline; the request is scheduled so this synchronous retirement can
        never hang a caller. Calling ``close`` again is safe, and
        ``await wait_stopped()`` observes the bounded stop when one was
        scheduled.
        """
        self._reset_lsp_state()
        self._documents = None
        self._files = None
        self._identity = None
        self._generation += 1
        clear_active_workspace_service(self)
        manager = self._lsp_manager
        if manager is not None:
            self._request_manager_stop(manager)
        return {"closed": True}

    async def wait_stopped(self) -> None:
        """Await the bounded manager stop requested by :meth:`close` (if any).

        The wait is bounded. If the graceful stop exceeds the deadline it is
        escalated to a best-effort synchronous force-kill so a shutdown can
        never leave an orphan language-server process behind; the original
        stop task is still retained (never cancelled here) and this method
        never raises from the shutdown path.
        """
        task = self._stop_task
        if task is None:
            return
        done, _pending = await asyncio.wait({task}, timeout=self._manager_stop_timeout)
        if not done:
            logger.warning(
                "language-server stop exceeded %.1fs; escalating to force-kill",
                self._manager_stop_timeout,
            )
            self._force_kill_manager(self._stopping_manager or self._lsp_manager)

    def _force_kill_manager(self, manager: Any) -> None:
        """Best-effort, non-raising synchronous kill of a manager's servers."""
        if manager is None:
            return
        force_kill_all = getattr(manager, "force_kill_all", None)
        if not callable(force_kill_all):
            return
        try:
            force_kill_all()
        except asyncio.CancelledError:
            # This is a synchronous escalation inside a shutdown path; the
            # caller's own cancellation (if any) still propagates.
            logger.warning("language-server force-kill was cancelled")
        except Exception:
            logger.warning("language-server force-kill failed", exc_info=True)

    def _reset_lsp_state(self) -> None:
        """Drop all per-workspace LSP state, including every stored diagnostic.

        Called on open, close and manager replacement: a diagnostic set is
        only meaningful for the identity/epoch that received it, so clearing
        here guarantees a previous workspace's diagnostics can never attach to
        a new one (the store additionally filters by epoch/generation).
        """
        for task in list(self._sync_tasks.values()):
            task.cancel()
        self._sync_tasks.clear()
        self._sync_locks.clear()
        for task in list(self._save_tasks):
            task.cancel()
        self._save_tasks.clear()
        self._connections.clear()
        self._open_records.clear()
        self._diagnostics.clear()

    def _request_manager_stop(self, manager: Any) -> None:
        existing = self._stop_task
        if existing is not None and not existing.done():
            return  # a bounded stop is already in flight; close stays idempotent
        self._closing_manager = manager
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no event loop: there is no async transport to stop here
        self._stopping_manager = manager
        self._stop_task = loop.create_task(
            self._stop_manager_detached(manager), name="workspace-lsp-stop"
        )

    async def _stop_manager_detached(self, manager: Any) -> None:
        """Run the manager's ``stop_all`` to completion in a retained task.

        On timeout the bounded observation (and its force-kill escalation)
        lives in :meth:`wait_stopped`. If this task is itself cancelled (for
        example while the event loop is torn down) or the stop fails midway,
        a best-effort synchronous force-kill still runs so no process is
        orphaned. Cancellation is re-raised; nothing is converted.
        """
        stop_all = getattr(manager, "stop_all", None)
        if not callable(stop_all):
            return
        completed = False
        try:
            await _await_if_needed(stop_all())
            completed = True
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("language-server stop failed", exc_info=True)
        finally:
            if not completed:
                self._force_kill_manager(manager)

    def _require_files(self) -> WorkspaceFiles:
        if self._files is None or self._identity is None:
            raise WorkspaceError("denied", "no workspace is open; call workspace.open first")
        return self._files

    def _require_documents(self) -> DocumentMirrorStore:
        self._require_files()
        assert self._documents is not None  # guarded by _require_files
        return self._documents

    def _check_epoch(self, epoch: int | None, *, required: bool = False) -> int:
        """Validate a caller's workspace epoch.

        Reads stay backward-compatible (``required=False``): an omitted epoch
        means "current", so older read clients keep working. Mutating
        operations pass ``required=True``: an absent epoch is refused instead
        of being silently treated as the current one, otherwise a stale caller
        that simply omits the field would always pass the fence.
        """
        if epoch is None:
            if required:
                raise WorkspaceError(
                    "denied", "workspace epoch is required for this operation"
                )
            effective = self._epoch
        else:
            effective = epoch
        if (
            not isinstance(effective, int)
            or isinstance(effective, bool)
            or effective != self._epoch
        ):
            raise WorkspaceError("denied", "stale workspace epoch; the workspace was reopened")
        return effective

    def _check_client(self, client_id: str | None) -> None:
        """Verify the optional client binding on a mutating request.

        Absent is allowed: a single-client stdio connection is itself the
        binding. When supplied (UI/HTTP path), it must equal the identity this
        service minted on open, so a request minted by another client cannot
        mutate this workspace.
        """
        if client_id is None:
            return
        if self._identity is None or client_id != self._identity.client_id:
            raise WorkspaceError("denied", "client id does not match the live workspace")

    # ─── URI translation (authorization still happens in WorkspaceFiles) ────
    def _uri_to_rel(self, uri: str) -> str:
        files = self._require_files()
        try:
            parsed = urllib.parse.urlsplit(uri)
        except ValueError as error:
            raise WorkspaceError("denied", "uri is not a valid file URI") from error
        if parsed.scheme != "file":
            raise WorkspaceError("denied", "uri must be a file:// URI")
        if parsed.netloc not in ("", "localhost"):
            raise WorkspaceError("denied", "remote file URIs are not supported")
        try:
            path = urllib.parse.unquote(parsed.path, errors="strict")
        except (UnicodeDecodeError, ValueError) as error:
            raise WorkspaceError("denied", "uri path is not valid percent-encoding") from error
        if not path.startswith("/"):
            # A relative file URI would otherwise be resolved against the
            # gateway process CWD; require an absolute path explicitly.
            raise WorkspaceError("denied", "uri must carry an absolute path")
        if "\x00" in path:
            raise WorkspaceError("denied", "uri contains a NUL byte")
        # Canonicalize only platform aliases outside the resolved root (e.g.
        # macOS /tmp -> /private/tmp). An in-root path keeps its requested
        # lexical identity even when a component is a symlink; resolving it
        # here rewrote the URI to a different in-root location (combined #58 /
        # original #71). Authority remains with resolve() at the I/O boundary,
        # which still refuses an outside target.
        root_str = str(files.root)
        lexical = os.path.normpath(path)
        under_root = lexical == root_str or lexical.startswith(root_str + os.sep)
        if not under_root:
            try:
                path = os.path.realpath(path)
            except OSError as error:
                raise WorkspaceError("denied", "uri is not accessible") from error
        if path == root_str:
            return ""
        try:
            rel = os.path.relpath(path, root_str)
        except ValueError as error:
            raise WorkspaceError(
                "denied", "uri is outside the authorized workspace root"
            ) from error
        rel = rel.replace(os.sep, "/")
        if rel == "." or rel == ".." or rel.startswith("../"):
            raise WorkspaceError("denied", "uri is outside the authorized workspace root")
        # Real containment (symlinks included) is enforced by resolve() at
        # the I/O boundary; this is only shape validation, never authority.
        return rel

    def _rel_to_uri(self, rel: str) -> str:
        files = self._require_files()
        normalized = files.normalized_rel(rel)
        absolute = files.root if not normalized else files.root / normalized
        return Path(absolute).as_uri()

    def _language_for_uri(self, uri: str) -> str | None:
        try:
            path = urllib.parse.unquote(urllib.parse.urlsplit(uri).path)
        except (UnicodeDecodeError, ValueError):
            return None
        return lsp_language_for(path)

    def _emit(self, event: dict[str, Any]) -> None:
        try:
            self._send_event(event)
        except Exception:  # a dead channel must not fail the operation
            logger.debug("workspace event dropped", exc_info=True)

    # ─── file operations ────────────────────────────────────────────────────
    def list(
        self,
        path: str = "",
        *,
        cursor: str | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
        include_hidden: bool = False,
    ) -> ListPage:
        return self._require_files().list_dir(
            path, cursor=cursor, limit=limit, include_hidden=include_hidden
        )

    def read(self, uri: str) -> ReadResult:
        files = self._require_files()
        rel = self._uri_to_rel(uri)
        result = files.read_text(rel, max_bytes=MAX_READ_BYTES)
        return replace(result, uri=self._rel_to_uri(rel))

    async def save(
        self,
        uri: str,
        text: str,
        expected_revision: str | None = None,
        *,
        ui_version: int | None = None,
        epoch: int | None = None,
        client_id: str | None = None,
    ) -> SaveResult:
        self._check_epoch(epoch, required=True)
        self._check_client(client_id)
        files = self._require_files()
        rel = self._uri_to_rel(uri)
        canonical = self._rel_to_uri(rel)
        result = await files.save_text(rel, text, expected_revision, ui_version=ui_version)
        if result.ok and result.revision is not None and self._identity is not None:
            # Defined order (plan §11 step 8): the disk commit is confirmed
            # first, pending didChange frames for this document flush next and
            # didSave follows (includeText only when the server asked). The
            # flush runs detached when a chain is in flight, so a parked sync
            # chain can never hold this RPC for the sync deadline; the
            # fileChanged event is emitted once the commit is confirmed.
            self._schedule_save_notification(canonical, text)
            self._emit(
                file_changed_event(
                    self._identity.workspace_id,
                    self._epoch,
                    canonical,
                    result.revision,
                    "save",
                )
            )
        return result

    # ─── document mirrors ───────────────────────────────────────────────────
    def document_open(
        self,
        uri: str,
        language_id: str,
        version: int,
        text: str,
        *,
        epoch: int | None = None,
        client_id: str | None = None,
    ) -> DocumentOpenResult:
        """Accept a mirror, then lazily start/synchronize its language server.

        The mirror is transport state and is accepted synchronously; the LSP
        side runs through an ordered, bounded per-document chain. The returned
        ``state`` is truthful at return time — ``ready`` when ``didOpen`` was
        queued inline, ``starting`` when a lazy start was scheduled, the
        connection's own state otherwise — and the UI may retry.
        """
        self._check_epoch(epoch, required=True)
        self._check_client(client_id)
        store = self._require_documents()
        rel = self._uri_to_rel(uri)
        canonical = self._rel_to_uri(rel)
        result = store.open_document(
            canonical, language_id, version, text, epoch=self._check_epoch(epoch)
        )
        language = lsp_language_for(rel)
        state, generation = self._lsp_document_accepted(canonical, language)
        if state is None:
            state = self._manager_state_for(language_id)
        return replace(
            result,
            state=state,
            server_generation=generation if generation is not None else result.server_generation,
        )

    def _lsp_document_accepted(
        self, uri: str, language: str | None
    ) -> tuple[str | None, int | None]:
        """Bring the server's view of an accepted mirror up to date.

        Returns ``(state, generation)`` for the orchestrated path and
        ``(None, None)`` when no real manager is attached (a duck-typed
        manager that owns dispatch reports its own state instead).
        """
        manager = self._lsp_manager
        if (
            manager is None
            or language is None
            or not callable(getattr(manager, "ensure_started", None))
        ):
            return None, None
        if not self._profile_supported(language):
            return None, None
        connection = self._tracked_connection(language)
        if connection is None:
            # Lazy start: never at open(), only on the first documentOpen for
            # this language (or an explicit lspControl start).
            if self._schedule_lsp_sync(uri, language, start=True):
                return STATE_STARTING, None
            logger.debug("no running event loop; language server was not started")
            return STATE_STOPPED, None
        if connection.state == STATE_READY:
            try:
                self._apply_document_sync(connection, uri, language, None)
            except LspError as error:
                logger.debug("didOpen could not be queued: %s", error)
            return connection.state, connection.generation
        if connection.state in (STATE_STARTING, STATE_INITIALIZING):
            self._schedule_lsp_sync(uri, language, start=True)
            return connection.state, connection.generation
        if connection.state == STATE_STOPPED:
            if self._schedule_lsp_sync(uri, language, start=True):
                return STATE_STARTING, connection.generation
            return connection.state, connection.generation
        if connection.state == STATE_FAILED:
            # Recovery is an explicit restart (or the manager's own bounded
            # auto-restart); never report "starting" for an attempt the
            # restart budget may refuse.
            self._schedule_lsp_sync(uri, language, start=True)
        return connection.state, connection.generation

    def document_change(
        self,
        uri: str,
        base_version: int,
        version: int,
        changes: list[dict[str, Any]],
        *,
        epoch: int | None = None,
        client_id: str | None = None,
    ) -> DocumentChangeResult:
        """Accept an incremental change and mirror it to a ready server.

        The mirror is the transport state: a change accepted while the server
        is not ready stays in the mirror and is re-synchronized as a full
        snapshot by the ``on_ready`` reopen. A ``didChange`` is queued only
        against the live generation that received the ``didOpen`` — never
        against a dead one, and never fabricated from stale ranges.
        """
        self._check_epoch(epoch, required=True)
        self._check_client(client_id)
        store = self._require_documents()
        rel = self._uri_to_rel(uri)
        canonical = self._rel_to_uri(rel)
        result = store.change_document(
            canonical, base_version, version, changes, epoch=self._check_epoch(epoch)
        )
        if result.resync or result.accepted_version != version:
            return result
        language = lsp_language_for(rel)
        if language is None:
            return result
        connection = self._tracked_connection(language)
        if connection is None or connection.state != STATE_READY:
            return result
        payload = {
            "base_version": base_version,
            "version": version,
            "changes": [dict(change) for change in changes if isinstance(change, dict)],
        }
        try:
            self._apply_document_sync(connection, canonical, language, payload)
        except LspError as error:
            # The mirror accepted the edit but the server did not receive it.
            # Tell the client to resync from a snapshot instead of claiming
            # the server is current (combined #59 / original #72).
            logger.debug("didChange could not be queued: %s", error)
            return DocumentChangeResult(accepted_version=result.accepted_version, resync=True)
        return result

    def document_close(
        self,
        uri: str,
        *,
        epoch: int | None = None,
        client_id: str | None = None,
    ) -> dict[str, Any]:
        """Retire a mirror and tell the server only if it knows the document.

        ``didClose`` is sent only when the document was opened on the
        connection's current generation; anything else has nothing to close
        and is dropped without a fabricated frame.
        """
        self._check_epoch(epoch, required=True)
        self._check_client(client_id)
        store = self._require_documents()
        rel = self._uri_to_rel(uri)
        canonical = self._rel_to_uri(rel)
        closed = store.close_document(canonical, epoch=self._check_epoch(epoch))
        # A reopen starts a fresh mirror at version 0: the retained version
        # gates for this URI must go with the mirror, or every later publish
        # for it is rejected as "older" forever.
        self._diagnostics.clear_uri(canonical)
        record = self._open_records.pop(canonical, None)
        if record is not None:
            connection = self._tracked_connection(record.language)
            if (
                connection is not None
                and connection.state == STATE_READY
                and connection.generation == record.generation
            ):
                try:
                    connection.did_close(canonical)
                except LspError as error:
                    logger.debug("didClose could not be queued: %s", error)
        return {"closed": closed, "uri": canonical}

    def document_snapshot(self, uri: str) -> dict[str, Any] | None:
        """Transport-state inspection for tests and later LSP synchronization."""
        if self._documents is None:
            return None
        canonical = self._rel_to_uri(self._uri_to_rel(uri))
        return self._documents.snapshot(canonical)

    def _manager_state_for(self, language_id: str) -> str:
        """Truthful state for a document when the orchestrated path declined.

        Duck-typed managers that expose ``state_for_language`` keep their old
        answer; a real manager without a per-language accessor is reported as
        ``missing`` (the orchestration already reported its own state whenever
        it could observe a connection).
        """
        manager = self._lsp_manager
        if manager is None:
            return STATE_MISSING
        state_for = getattr(manager, "state_for_language", None)
        if callable(state_for):
            try:
                value = state_for(language_id)
            except Exception:
                logger.warning("lsp manager state_for_language failed", exc_info=True)
                return "degraded"
            return str(value) if value else "degraded"
        if callable(getattr(manager, "ensure_started", None)):
            return STATE_MISSING
        return "degraded"

    # ─── language-server bookkeeping ────────────────────────────────────────
    def _profile_supported(self, language: str) -> bool:
        """Whether the profile for ``language`` resolved when the workspace opened.

        The snapshot from :meth:`open` is authoritative for the session (an
        installed-mid-session server is started through an explicit
        ``lspControl`` request, never implicitly).
        """
        known = self._profiles.get(language)
        if known is not None:
            return known
        return lsp_module.resolve_profile(language) is not None

    def _tracked_connection(self, language: str | None) -> Any | None:
        if language is None:
            return None
        entry = self._connections.get(language)
        if entry is None or entry.epoch != self._epoch:
            return None
        return entry.connection

    def _track_connection(self, language: str, connection: Any) -> None:
        """Remember a started connection and observe its lifecycle once.

        The subscriptions stay valid across workspace epochs: the handlers
        look up the live tracking table and drop anything else, so an event
        from a no-longer-tracked (old epoch) connection can never surface.
        """
        self._connections[language] = _TrackedConnection(
            connection=connection, language=language, epoch=self._epoch
        )
        if not any(existing is connection for existing in self._state_subscribed):
            self._state_subscribed.append(connection)
            subscribe_state = getattr(connection, "on_state_change", None)
            if callable(subscribe_state):
                subscribe_state(self._handle_connection_state)
            subscribe_notifications = getattr(connection, "on_notification", None)
            if callable(subscribe_notifications):

                def _notification(method: str, params: Any) -> None:
                    self._handle_connection_notification(connection, method, params)

                subscribe_notifications(_notification)
        if connection.state == STATE_READY:
            self._reopen_documents(connection, language)

    def _tracked_entry(self, connection: Any) -> _TrackedConnection | None:
        for entry in self._connections.values():
            if entry.connection is connection and entry.epoch == self._epoch:
                return entry
        return None

    def _handle_connection_state(self, connection: Any, state: str) -> None:
        """Emit ``workspace.lspState``; reopen mirrors when a server becomes ready.

        The ready transition reopens every mirror with its latest snapshot —
        belt and braces for injected managers that do not expose ``on_ready``
        (the manager-level hook does the same, idempotently, when it exists).
        """
        if self._identity is None:
            return
        entry = self._tracked_entry(connection)
        if entry is None:
            return
        if state == STATE_READY:
            self._diagnostics.prune(epoch=self._epoch, generation_for=self._producer_generation)
            self._reopen_documents(connection, entry.language)
        detail = ""
        if state in (STATE_FAILED, STATE_STOPPING, STATE_STOPPED, STATE_MISSING):
            error = sanitize_log_text(str(getattr(connection, "error", "") or ""))
            if getattr(connection, "cleanup_pending", False):
                pending = "cleanup pending: process not reaped"
                detail = f"{pending} ({error})" if error else pending
            else:
                detail = error
        self._emit(
            lsp_state_event(
                self._identity.workspace_id,
                self._epoch,
                str(connection.profile_id),
                state,
                int(connection.generation),
                detail,
            )
        )

    def _handle_connection_ready(self, connection: Any) -> None:
        """After every successful initialize, reopen mirrors with latest text.

        Stable order (sorted URIs) and generation-scoped: a document whose
        record already matches the new generation and version is untouched.
        """
        if self._identity is None or self._documents is None:
            return
        for language, entry in self._connections.items():
            if entry.connection is connection and entry.epoch == self._epoch:
                self._diagnostics.prune(epoch=self._epoch, generation_for=self._producer_generation)
                self._reopen_documents(connection, language)
                return

    def _reopen_documents(self, connection: Any, language: str) -> None:
        if self._documents is None or connection.state != STATE_READY:
            return
        for uri in self._documents.uris():
            if self._language_for_uri(uri) != language:
                continue
            try:
                self._apply_document_sync(connection, uri, language, None)
            except LspError as error:
                logger.debug("reopen of %s could not be queued: %s", uri, error)

    def _producer_generation(self, producer: str) -> int | None:
        for entry in self._connections.values():
            if entry.epoch == self._epoch and entry.connection.profile_id == producer:
                return int(entry.connection.generation)
        return None

    # ─── ordered document synchronization ───────────────────────────────────
    def _schedule_lsp_sync(self, uri: str, language: str, *, start: bool) -> bool:
        """Queue an ordered, bounded synchronization step for one document.

        Returns False when no event loop is running (nothing async can be
        started), which callers report honestly instead of pretending a start
        is in flight.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        epoch = self._epoch
        lock = self._sync_locks.setdefault(uri, asyncio.Lock())
        task = loop.create_task(
            self._lsp_sync_chain(uri, language, epoch, lock, start),
            name=f"workspace-lsp-sync:{uri}",
        )
        self._sync_tasks[uri] = task

        def _discard(completed: asyncio.Task[None], uri: str = uri) -> None:
            if self._sync_tasks.get(uri) is completed:
                self._sync_tasks.pop(uri, None)

        task.add_done_callback(_discard)
        return True

    async def _lsp_sync_chain(
        self, uri: str, language: str, epoch: int, lock: asyncio.Lock, start: bool
    ) -> None:
        try:
            async with lock:
                if epoch != self._epoch or self._documents is None:
                    return  # the workspace was reopened/closed: drop stale work
                connection = self._tracked_connection(language)
                if start and (
                    connection is None or connection.state in (STATE_STOPPED, STATE_FAILED)
                ):
                    manager = self._lsp_manager
                    if manager is None:
                        return
                    # Lazy start: a first open starts from scratch; a stopped
                    # server starts again; a failed one is retried only within
                    # the manager's bounded restart budget (typed refusal
                    # otherwise, contained below).
                    connection = await _await_if_needed(manager.ensure_started(language))
                    if epoch != self._epoch:
                        return
                    self._track_connection(language, connection)
                if connection is None:
                    return
                if connection.state != STATE_READY:
                    # Bounded wait for starting/initializing → ready; a server
                    # that fails or exceeds the wait is resynchronized later by
                    # the on_ready reopen hook, never by a stale frame.
                    with contextlib.suppress(LspError):
                        await connection.wait_ready(self._sync_ready_timeout)
                if connection.state != STATE_READY:
                    return
                self._apply_document_sync(connection, uri, language, None)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("language-server synchronization failed", exc_info=True)

    def _apply_document_sync(
        self, connection: Any, uri: str, language: str, payload: dict[str, Any] | None
    ) -> None:
        """Advance the server's view of one document to the mirror's newest version.

        ``payload`` carries a validated incremental change ``{base_version,
        version, changes}`` and is used only when the server sits exactly at
        ``base_version`` and the change reaches the mirror's newest version.
        Any gap falls back to a deliberate full snapshot (full-text
        ``didChange`` for sync kind 1, rangeless whole-document replacement for
        kind 2) — never a change whose base the server does not have. The
        fallback must not compute a range from the new snapshot: when the
        document shrinks that range describes the new extent, not the text the
        server currently holds, so old content survives the replacement.
        """
        store = self._documents
        if store is None or connection.state != STATE_READY:
            return
        snapshot = store.snapshot(uri)
        if snapshot is None:
            return
        mirror_version = int(snapshot["version"])
        record = self._open_records.get(uri)
        generation = int(connection.generation)
        if record is None or record.generation != generation:
            # didOpen carries the DOCUMENT's protocol languageId (what the
            # server uses for language behavior, e.g. "typescriptreact" for
            # .tsx), while `language` above only selected the server profile
            # by extension. Prefer the extension mapping; fall back to the
            # client-declared id when the extension has no mapping.
            protocol_language = language_id_for(uri)
            if protocol_language == "plaintext":
                declared = snapshot.get("languageId")
                protocol_language = str(declared) if declared else language
            connection.did_open(uri, protocol_language, mirror_version, snapshot["text"])
            self._open_records[uri] = _OpenRecord(
                language=language, version=mirror_version, generation=generation
            )
            return
        if record.version >= mirror_version:
            return
        sync_kind = lsp_sync_kind(connection.capabilities)
        if (
            sync_kind == 2
            and payload is not None
            and payload.get("base_version") == record.version
            and payload.get("version") == mirror_version
            and payload.get("changes")
        ):
            connection.did_change(uri, payload["version"], payload["changes"])
            record.version = int(payload["version"])
            return
        if sync_kind == 1:
            connection.did_change(uri, mirror_version, [{"text": snapshot["text"]}])
            record.version = mirror_version
        elif sync_kind == 2:
            # LSP permits the full-text variant of a content change even for
            # an incremental server; it has no range to mis-describe the
            # server-held text and is the only safe recovery when the old
            # server text is not retained here.
            connection.did_change(uri, mirror_version, [{"text": snapshot["text"]}])
            record.version = mirror_version
        # else: the server negotiated no text synchronization (kind 0/absent);
        # nothing may be sent and nothing is recorded as synced.

    async def _flush_document(self, uri: str) -> None:
        """Await the document's pending sync chain, bounded, without cancelling."""
        task = self._sync_tasks.get(uri)
        if task is None or task.done():
            return
        done, _pending = await asyncio.wait({task}, timeout=self._sync_ready_timeout + 5.0)
        if not done:
            logger.debug("document sync for %s did not finish within its deadline", uri)

    def _did_save_if_open(self, canonical: str, text: str) -> None:
        """Queue didSave for a document the live generation knows about.

        ``LspConnection.did_save`` includes the text only when the server's
        capabilities asked for it; a document that was never opened (or whose
        server is not on this generation) gets no fabricated notification.
        """
        record = self._open_records.get(canonical)
        if record is None:
            return
        connection = self._tracked_connection(record.language)
        if (
            connection is None
            or connection.state != STATE_READY
            or connection.generation != record.generation
        ):
            return
        try:
            connection.did_save(canonical, text)
        except LspError as error:
            logger.debug("didSave could not be queued: %s", error)

    def _schedule_save_notification(self, uri: str, text: str) -> None:
        """Deliver ``didSave`` after the document's pending sync chain, detached.

        The disk commit is already confirmed when this runs. With no sync work
        in flight the notification is queued inline (zero added latency, the
        defined commit → flush → didSave order holds); with a chain in flight
        the flush plus ``didSave`` run in a bounded background task so a parked
        chain cannot hold the save RPC for the sync deadline.
        """
        pending = self._sync_tasks.get(uri)
        if pending is None or pending.done():
            self._did_save_if_open(uri, text)
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no event loop: nothing async can be in flight either
        task = loop.create_task(
            self._save_notification_chain(uri, text), name=f"workspace-lsp-save:{uri}"
        )
        self._save_tasks.add(task)
        task.add_done_callback(self._save_tasks.discard)

    async def _save_notification_chain(self, uri: str, text: str) -> None:
        try:
            await self._flush_document(uri)
            self._did_save_if_open(uri, text)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("didSave after save could not be delivered", exc_info=True)

    # ─── intelligence ───────────────────────────────────────────────────────
    async def intelligence(
        self,
        operation: str,
        *,
        uri: str | None = None,
        version: int | None = None,
        position: dict[str, Any] | None = None,
        query: str | None = None,
        epoch: int | None = None,
    ) -> IntelligenceResult:
        """Closed-allowlist intelligence dispatch (no raw LSP method tunnel).

        Request operations flush the document's pending synchronization chain
        first (ordering invariant), then issue exactly one standard LSP
        request against the live generation. Every reply is normalized into a
        bounded payload; malformed server data is dropped, never guessed.
        """
        self._require_files()
        if not self.trusted:
            raise WorkspaceError("denied", "workspace is not trusted for code intelligence")
        if operation not in INTELLIGENCE_OPERATIONS:
            raise WorkspaceError("denied", f"unsupported intelligence operation: {operation}")
        workspace_epoch = self._check_epoch(epoch)

        canonical_uri = ""
        language: str | None = None
        source = "disk"
        mirror: dict[str, Any] | None = None
        if uri:
            rel = self._uri_to_rel(uri)
            canonical_uri = self._rel_to_uri(rel)
            language = lsp_language_for(rel)
            mirror = self._documents.snapshot(canonical_uri) if self._documents else None
            if mirror is not None:
                source = "editor-buffer"
        document_version = version
        if document_version is None and mirror is not None:
            document_version = int(mirror["version"])

        manager = self._lsp_manager
        if manager is None:
            return self._intelligence_result(
                workspace_epoch=workspace_epoch,
                operation=operation,
                source=source,
                uri=canonical_uri,
                document_version=document_version,
                state=STATE_MISSING,
                freshness="unavailable",
                result=None,
                generation=None,
            )
        dispatch = getattr(manager, "intelligence", None)
        if callable(dispatch):
            # Explicit dispatch override (duck-typed managers, tests, future
            # responders) keeps precedence over the orchestrated path.
            return await self._delegated_intelligence(
                dispatch,
                operation=operation,
                workspace_epoch=workspace_epoch,
                source=source,
                uri=canonical_uri,
                document_version=document_version,
                position=position,
                query=query,
            )
        return await self._lsp_intelligence(
            operation,
            workspace_epoch=workspace_epoch,
            source=source,
            uri=canonical_uri,
            language=language,
            mirror=mirror,
            requested_version=version,
            position=position,
        )

    async def _delegated_intelligence(
        self,
        dispatch: Callable[..., Any],
        *,
        operation: str,
        workspace_epoch: int,
        source: str,
        uri: str,
        document_version: int | None,
        position: dict[str, Any] | None,
        query: str | None,
    ) -> IntelligenceResult:
        """Delegate to a manager that owns its own intelligence dispatch."""

        if uri:
            # The ordering invariant holds for delegated managers too: their
            # request must not overtake this document's pending didChange.
            await self._flush_document(uri)

        def result_for(
            state: str,
            freshness: str,
            payload: Any = None,
            generation: Any = None,
            truncated: bool = False,
        ) -> IntelligenceResult:
            return self._intelligence_result(
                workspace_epoch=workspace_epoch,
                operation=operation,
                source=source,
                uri=uri,
                document_version=document_version,
                state=state,
                freshness=freshness,
                result=payload,
                generation=generation,
                truncated=truncated,
            )

        request = {
            "operation": operation,
            "uri": uri or None,
            "version": document_version,
            "position": position,
            "query": query,
            "source": source,
            "workspaceEpoch": workspace_epoch,
        }
        try:
            payload = await _await_if_needed(dispatch(request))
        except Exception:
            logger.warning("lsp manager intelligence failed", exc_info=True)
            return result_for("failed", "unavailable")
        if not isinstance(payload, dict):
            logger.warning("lsp manager returned a non-object intelligence payload")
            return result_for("failed", "unavailable")
        state = str(payload.get("state") or "degraded")
        freshness = str(payload.get("freshness") or "unavailable")
        resolved_source = str(payload.get("source") or source)
        return self._intelligence_result(
            workspace_epoch=workspace_epoch,
            operation=operation,
            source=resolved_source,
            uri=uri,
            document_version=document_version,
            state=state,
            freshness=freshness,
            result=payload.get("result"),
            generation=payload.get("serverGeneration"),
            truncated=bool(payload.get("truncated")),
        )

    async def _lsp_intelligence(
        self,
        operation: str,
        *,
        workspace_epoch: int,
        source: str,
        uri: str,
        language: str | None,
        mirror: dict[str, Any] | None,
        requested_version: int | None,
        position: dict[str, Any] | None,
    ) -> IntelligenceResult:
        mirror_version = int(mirror["version"]) if mirror is not None else None

        def failure(
            state: str,
            freshness: str,
            generation: int | None = None,
            document_version: int | None = mirror_version,
        ) -> IntelligenceResult:
            return self._intelligence_result(
                workspace_epoch=workspace_epoch,
                operation=operation,
                source=source,
                uri=uri,
                document_version=document_version,
                state=state,
                freshness=freshness,
                result=None,
                generation=generation,
            )

        if not uri or language is None:
            # No file, or a file whose extension maps to no profile language.
            return failure(STATE_MISSING, "unavailable")
        request_position = None
        if operation in POSITION_OPERATIONS:
            # A cursor operation without a position is malformed input and is
            # refused deterministically, whatever the server state happens to be.
            request_position = self._require_position(position)
        await self._flush_document(uri)
        connection = self._tracked_connection(language)
        if connection is None:
            observed = await self._manager_language_status(language)
            if observed is None:
                return failure(STATE_MISSING, "unavailable")
            state, generation = observed
            freshness = (
                "pending" if state in (STATE_STARTING, STATE_INITIALIZING) else "unavailable"
            )
            return failure(state, freshness, generation)
        generation = int(connection.generation)
        if operation == "diagnostics":
            # Diagnostics come from the notification store: a crashed or
            # stopped server still has a last-known set, served as `stale`.
            return self._diagnostics_intelligence(
                connection=connection,
                workspace_epoch=workspace_epoch,
                source=source,
                uri=uri,
                mirror=mirror,
            )
        if connection.state != STATE_READY:
            freshness = (
                "pending"
                if connection.state in (STATE_STARTING, STATE_INITIALIZING)
                else "unavailable"
            )
            return failure(connection.state, freshness, generation)
        if (
            mirror_version is not None
            and requested_version is not None
            and requested_version > mirror_version
        ):
            # The caller's buffer is ahead of the accepted mirror: there is no
            # result for that version yet, and the old text is not a substitute.
            return failure(STATE_READY, "stale", generation, mirror_version)
        record = self._open_records.get(uri)
        synced = (
            mirror_version is not None
            and record is not None
            and record.generation == generation
            and record.version == mirror_version
        )
        freshness = "versioned" if synced else "unversioned"
        method, params = lsp_request_for(operation, uri, request_position)
        timeout = self._intelligence_timeouts.get(operation, 10.0)
        try:
            raw = await connection.request(method, params, timeout)
        except LspTimeoutError:
            return failure(STATE_FAILED, "unavailable", generation, mirror_version)
        except LspRequestError as error:
            freshness = "stale" if error.content_modified else "unavailable"
            return failure(STATE_FAILED, freshness, generation, mirror_version)
        except LspError:
            state = connection.state if connection.state != STATE_READY else STATE_FAILED
            return failure(state, "unavailable", generation, mirror_version)
        normalized = normalize_for(operation, raw)
        return self._intelligence_result(
            workspace_epoch=workspace_epoch,
            operation=operation,
            source=source,
            uri=uri,
            document_version=mirror_version,
            state=STATE_READY,
            freshness=freshness,
            result=normalized.result if normalized is not None else None,
            generation=generation,
            truncated=bool(normalized is not None and normalized.truncated),
        )

    def _diagnostics_intelligence(
        self,
        *,
        connection: Any,
        workspace_epoch: int,
        source: str,
        uri: str,
        mirror: dict[str, Any] | None,
    ) -> IntelligenceResult:
        """Serve the notification store; never invent a "clean" result."""
        generation = int(connection.generation)
        state = str(connection.state)
        mirror_version = int(mirror["version"]) if mirror is not None else None
        stored_sets = self._diagnostics.entries(
            uri, epoch=self._epoch, generation_for=self._producer_generation
        )
        if not stored_sets:
            if state == STATE_READY:
                # Nothing published (yet): unversioned, not "clean".
                freshness = "unversioned"
            elif state in (STATE_STARTING, STATE_INITIALIZING, STATE_STOPPING):
                freshness = "pending"
            else:
                freshness = "unavailable"
            return self._intelligence_result(
                workspace_epoch=workspace_epoch,
                operation="diagnostics",
                source=source,
                uri=uri,
                document_version=mirror_version,
                state=state,
                freshness=freshness,
                result=None,
                generation=generation,
            )
        producers: list[dict[str, Any]] = []
        combined: list[dict[str, Any]] = []
        settled: set[str] = set()
        omitted = 0
        for stored in stored_sets:
            if stored.version is None or mirror_version is None:
                per_freshness = "unversioned"
            elif stored.version == mirror_version:
                per_freshness = "versioned"
            else:
                per_freshness = "stale"
            settled.add(per_freshness)
            producers.append(
                {
                    "source": stored.producer,
                    "version": stored.version,
                    "freshness": per_freshness,
                    "count": len(stored.diagnostics),
                }
            )
            # Tag every row with its producer key so clients group pulled
            # diagnostics under the SAME key as workspace.diagnostics events
            # (otherwise the display source string duplicates the set).
            for diagnostic in stored.diagnostics:
                combined.append({**diagnostic, "producer": stored.producer})
            omitted += stored.omitted
        capped = combined[:MAX_DIAGNOSTICS_PER_FILE]
        omitted += len(combined) - len(capped)
        if state in (STATE_STARTING, STATE_INITIALIZING, STATE_STOPPING):
            freshness = "pending"
        elif state != STATE_READY:
            # A crashed/stopped server keeps its last set, clearly marked
            # stale — never cleared silently and never presented as current.
            freshness = "stale"
        elif "stale" in settled:
            freshness = "stale"
        elif "unversioned" in settled:
            freshness = "unversioned"
        else:
            freshness = "versioned"
        return self._intelligence_result(
            workspace_epoch=workspace_epoch,
            operation="diagnostics",
            source=source,
            uri=uri,
            document_version=mirror_version,
            state=state,
            freshness=freshness,
            result={"diagnostics": capped, "omitted": omitted, "producers": producers},
            generation=generation,
            truncated=omitted > 0,
        )

    async def _manager_language_status(self, language: str) -> tuple[str, int] | None:
        """Observed state for a language from the manager's status view."""
        manager = self._lsp_manager
        status = getattr(manager, "status", None)
        if not callable(status):
            return None
        try:
            payload = await _await_if_needed(status())
        except Exception:
            logger.debug("lsp manager status failed", exc_info=True)
            return None
        entries = payload.get("servers") if isinstance(payload, Mapping) else payload
        if not isinstance(entries, (list, tuple)):
            return None
        for entry in entries:
            if not isinstance(entry, Mapping) or entry.get("languageId") != language:
                continue
            state = str(entry.get("state") or STATE_MISSING)
            generation = entry.get("generation")
            if not isinstance(generation, int) or isinstance(generation, bool):
                generation = 0
            return state, generation
        return None

    def _require_position(self, position: Any) -> dict[str, int]:
        if not isinstance(position, Mapping):
            raise WorkspaceError("denied", "a position is required for this operation")
        line = position.get("line")
        character = position.get("character")
        for number in (line, character):
            if (
                not isinstance(number, int)
                or isinstance(number, bool)
                or number < 0
                or number > MAX_POSITION_VALUE
            ):
                raise WorkspaceError(
                    "denied", "position line/character must be non-negative integers"
                )
        return {"line": line, "character": character}

    def _intelligence_result(
        self,
        *,
        workspace_epoch: int,
        operation: str,
        source: str,
        uri: str,
        document_version: int | None,
        state: str,
        freshness: str,
        result: Any,
        generation: Any,
        truncated: bool = False,
    ) -> IntelligenceResult:
        return IntelligenceResult(
            workspace_id=self._identity.workspace_id if self._identity else "",
            source=source,
            uri=uri,
            document_version=document_version,
            server_generation=(
                generation
                if isinstance(generation, int) and not isinstance(generation, bool)
                else None
            ),
            workspace_epoch=workspace_epoch,
            operation=operation,
            state=state,
            freshness=freshness,
            truncated=truncated,
            result=result,
        )

    # ─── diagnostics notifications (plan §16) ───────────────────────────────
    def _handle_connection_notification(self, connection: Any, method: str, params: Any) -> None:
        """Store ``textDocument/publishDiagnostics`` per (uri, producer).

        Replace semantics live in :class:`DiagnosticsStore`: a publish
        replaces that producer's set, an empty list clears only that producer,
        and older-version publications are rejected. Diagnostics for URIs
        outside the authorized root are dropped, and an event is emitted for
        every accepted publish (including clears) with epoch and generation.

        The epoch is resolved from the live tracking table at delivery time
        (never captured at subscription time): a connection re-tracked after a
        workspace reopen publishes under the new epoch, while a connection
        that is no longer live in this epoch is dropped.
        """
        if method != "textDocument/publishDiagnostics":
            return
        if self._identity is None:
            return
        entry = self._tracked_entry(connection)
        if entry is None:
            return  # not live in this epoch: an old server never writes in
        if not isinstance(params, Mapping):
            return
        uri = params.get("uri")
        if not isinstance(uri, str) or not uri or len(uri) > MAX_URI_CHARS:
            return
        try:
            canonical = self._rel_to_uri(self._uri_to_rel(uri))
        except WorkspaceError:
            return  # outside the authorized root: never stored, never emitted
        raw_diagnostics = params.get("diagnostics", [])
        if not isinstance(raw_diagnostics, (list, tuple)):
            return
        version = params.get("version")
        if not isinstance(version, int) or isinstance(version, bool) or version < 0:
            version = None
        producer = str(connection.profile_id)
        outcome = self._diagnostics.publish(
            uri=canonical,
            producer=producer,
            diagnostics=list(raw_diagnostics),
            version=version,
            generation=int(connection.generation),
            epoch=entry.epoch,
        )
        if outcome is None:
            return  # older-version results never replace newer ones
        self._emit(
            diagnostics_event(
                self._identity.workspace_id,
                entry.epoch,
                outcome.generation,
                canonical,
                outcome.version,
                producer,
                list(outcome.diagnostics),
            )
        )

    # ─── language-server status and control ─────────────────────────────────
    async def lsp_status(self) -> dict[str, Any]:
        """Server rows when a manager is attached; empty list when trusted
        without a manager; refuses (denied) for an untrusted workspace."""
        self._require_files()
        if not self.trusted:
            raise WorkspaceError("denied", "workspace is not trusted for language-server status")
        manager = self._lsp_manager
        if manager is None:
            return {"servers": []}
        status = getattr(manager, "status", None)
        if not callable(status):
            return {"servers": []}
        try:
            payload = await _await_if_needed(status())
        except Exception:
            logger.warning("lsp manager status failed", exc_info=True)
            return {"servers": []}
        entries = payload.get("servers") if isinstance(payload, Mapping) else payload
        if not isinstance(entries, (list, tuple)):
            return {"servers": []}
        return {
            "servers": [
                self._status_entry(entry) for entry in entries if isinstance(entry, Mapping)
            ]
        }

    def _status_entry(self, entry: Mapping[str, Any]) -> dict[str, Any]:
        generation = entry.get("generation")
        server: dict[str, Any] = {
            "profile": str(entry.get("profile", "")),
            "languageId": str(entry.get("languageId", "")),
            "state": str(entry.get("state", STATE_MISSING)),
            "generation": (
                generation
                if isinstance(generation, int) and not isinstance(generation, bool)
                else 0
            ),
        }
        capabilities = entry.get("capabilities")
        if isinstance(capabilities, Mapping):
            server["capabilities"] = capability_summary(capabilities)
        detail = entry.get("detail")
        if isinstance(detail, str) and detail:
            server["detail"] = sanitize_log_text(detail)
        return server

    async def lsp_control(
        self,
        action: str,
        profile: str,
        *,
        epoch: int | None = None,
        client_id: str | None = None,
    ) -> dict[str, Any]:
        """start/stop/restart through the injected manager; never installation.

        ``profile`` accepts the profile ids surfaced by ``lspStatus``
        ("pyright", "typescript-language-server") and the plain language names
        ("python", "typescript"); anything else is an honest ``missing``.
        """
        self._check_epoch(epoch, required=True)
        self._check_client(client_id)
        if action not in ("start", "stop", "restart"):
            raise WorkspaceError("denied", f"unsupported lsp control action: {action}")
        self._require_files()
        if not self.trusted:
            raise WorkspaceError("denied", "workspace is not trusted for language-server control")
        manager = self._lsp_manager
        if manager is None:
            return {
                "action": action,
                "profile": profile,
                "state": STATE_MISSING,
                "detail": "no language-server manager is configured; setup is explicit",
            }
        ensure_started = getattr(manager, "ensure_started", None)
        if not callable(ensure_started):
            return {
                "action": action,
                "profile": profile,
                "state": STATE_MISSING,
                "detail": "language-server manager does not support control requests",
            }
        language = language_for_profile(profile) if isinstance(profile, str) else None
        if language is None:
            return {
                "action": action,
                "profile": profile,
                "state": STATE_MISSING,
                "detail": "unsupported language-server profile",
            }
        if action == "start":
            return await self._control_start(action, profile, language, ensure_started)
        if action == "stop":
            return await self._control_stop(action, profile, language)
        return await self._control_restart(action, profile, language, manager, ensure_started)

    async def _control_start(
        self, action: str, profile: str, language: str, ensure_started: Callable[..., Any]
    ) -> dict[str, Any]:
        try:
            connection = await asyncio.wait_for(
                _await_if_needed(ensure_started(language)), timeout=self._control_timeout
            )
        except TimeoutError:
            return {
                "action": action,
                "profile": profile,
                "state": STATE_STARTING,
                "detail": "start exceeded its deadline and was cancelled",
            }
        except LspError as error:
            return self._control_failure(action, profile, str(error))
        except Exception as error:
            logger.warning("lsp start failed", exc_info=True)
            return self._control_failure(action, profile, f"{type(error).__name__}: {error}")
        self._track_connection(language, connection)
        # A manager with no resolvable executable returns an honest ``missing``
        # connection; its own error text is the best detail available.
        detail = ""
        if connection.state == STATE_MISSING:
            detail = str(getattr(connection, "error", "") or "")
        return self._control_result(action, profile, connection, detail=detail)

    async def _control_stop(self, action: str, profile: str, language: str) -> dict[str, Any]:
        connection = self._tracked_connection(language)
        if connection is None:
            return {
                "action": action,
                "profile": profile,
                "state": STATE_STOPPED,
                "detail": "no connection is currently tracked for this profile",
            }
        try:
            outcome = await asyncio.wait_for(connection.stop(), timeout=self._manager_stop_timeout)
        except TimeoutError:
            return {
                "action": action,
                "profile": profile,
                "state": STATE_STOPPING,
                "detail": "stop exceeded its deadline; observe the process",
            }
        except Exception as error:
            logger.warning("lsp stop failed", exc_info=True)
            return self._control_failure(action, profile, f"{type(error).__name__}: {error}")
        # The stopped server no longer knows any document; records lose their
        # generation scope with it and are refreshed on the next open/start.
        self._open_records = {
            uri: record for uri, record in self._open_records.items() if record.language != language
        }
        return self._control_result(action, profile, connection, detail=str(outcome))

    async def _control_restart(
        self,
        action: str,
        profile: str,
        language: str,
        manager: Any,
        ensure_started: Callable[..., Any],
    ) -> dict[str, Any]:
        connection = self._tracked_connection(language)
        if connection is None:
            # Restarting a server that never started is a start.
            return await self._control_start(action, profile, language, ensure_started)
        restart = getattr(manager, "restart", None)
        if not callable(restart):
            return {
                "action": action,
                "profile": profile,
                "state": STATE_MISSING,
                "detail": "language-server manager does not support restart",
            }
        try:
            connection = await asyncio.wait_for(
                _await_if_needed(restart(language)), timeout=self._control_timeout
            )
        except TimeoutError:
            return {
                "action": action,
                "profile": profile,
                "state": STATE_STOPPING,
                "detail": "restart exceeded its deadline",
            }
        except LspError as error:
            return self._control_failure(action, profile, str(error))
        except Exception as error:
            logger.warning("lsp restart failed", exc_info=True)
            return self._control_failure(action, profile, f"{type(error).__name__}: {error}")
        self._track_connection(language, connection)
        return self._control_result(action, profile, connection)

    def _control_result(
        self, action: str, profile: str, connection: Any, detail: str = ""
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "action": action,
            "profile": profile,
            "state": str(connection.state),
            "generation": int(connection.generation),
        }
        if detail:
            result["detail"] = sanitize_log_text(detail)
        return result

    def _control_failure(self, action: str, profile: str, detail: str) -> dict[str, Any]:
        return {
            "action": action,
            "profile": profile,
            "state": STATE_FAILED,
            "detail": sanitize_log_text(detail),
        }

    # ─── quick open (plan §12) ──────────────────────────────────────────────
    async def quick_open(
        self,
        query: str,
        limit: int = DEFAULT_QUICK_OPEN_LIMIT,
        *,
        epoch: int | None = None,
        rebuild: bool = False,
        wait: float = QUICK_OPEN_PARTIAL_WAIT,
    ) -> QuickOpenResult:
        """Ranked, bounded file search over the background recursive index.

        The index is built by a single-flight asyncio scan (never a blocking
        recursive walk); concurrent queries share it. When the scan is still
        running after ``wait`` seconds the partial index answers with
        ``indexing: true`` and the ``scanned`` progress count, and the caller
        re-queries for more. A workspace reopen during the scan retires the
        index with it: the answer is empty and ``stale: true``, never a mix
        of two epochs.
        """
        files = self._require_files()
        workspace_epoch = self._check_epoch(epoch)
        if not isinstance(query, str) or len(query) > MAX_QUICK_OPEN_QUERY_CHARS:
            raise WorkspaceError(
                "denied",
                f"query must be a string of at most {MAX_QUICK_OPEN_QUERY_CHARS} characters",
            )
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= MAX_QUICK_OPEN_LIMIT
        ):
            raise WorkspaceError("denied", f"limit must be between 1 and {MAX_QUICK_OPEN_LIMIT}")
        indexer = self._quick_open_indexer(files, workspace_epoch)
        index, indexing = await indexer.ensure(wait=wait, rebuild=rebuild)
        if self._epoch != workspace_epoch:
            # The workspace was reopened while the scan ran: the index and any
            # answer built against it belong to a retired epoch. Retire them.
            self._retire_quick_indexer(indexer)
            return QuickOpenResult(
                entries=(),
                total=0,
                truncated=False,
                indexing=False,
                scanned=0,
                stale=True,
                workspace_epoch=workspace_epoch,
                exclusions=indexer.exclusions,
                reasons=("stale-epoch",),
            )
        entries, total = quickopen_module.rank_entries(tuple(index.entries), query, limit)
        reasons = list(index.reasons)
        if total > len(entries):
            reasons.append(quickopen_module.REASON_RESULT_LIMIT)
        return QuickOpenResult(
            entries=tuple(entries),
            total=total,
            truncated=index.truncated or total > len(entries),
            indexing=indexing,
            scanned=index.scanned,
            stale=False,
            workspace_epoch=workspace_epoch,
            exclusions=indexer.exclusions,
            reasons=tuple(reasons),
        )

    def quick_open_cancel(self) -> dict[str, Any]:
        """Cancel the in-flight recursive index scan (no-op when none runs)."""
        indexer: quickopen_module.QuickOpenIndexer | None = getattr(self, "_quick_indexer", None)
        if indexer is None:
            return {"cancelled": False}
        return {"cancelled": indexer.cancel()}

    def _quick_open_indexer(
        self, files: WorkspaceFiles, epoch: int
    ) -> quickopen_module.QuickOpenIndexer:
        """Live single-flight indexer for the current (root, epoch) binding.

        A root or epoch change retires the previous indexer and cancels its
        in-flight scan so a stale walk never keeps running against a
        workspace that no longer exists.
        """
        indexer: quickopen_module.QuickOpenIndexer | None = getattr(self, "_quick_indexer", None)
        key = (str(files.root), epoch)
        if indexer is not None and indexer.key == key:
            return indexer
        if indexer is not None:
            indexer.cancel()
        indexer = quickopen_module.QuickOpenIndexer(
            files.root,
            key=key,
            exclusions=files.exclusions,
            on_progress=lambda scanned, done, bound_epoch=epoch: self._emit_quick_open_progress(
                bound_epoch, scanned, done
            ),
        )
        self._quick_indexer = indexer
        return indexer

    def _retire_quick_indexer(self, indexer: quickopen_module.QuickOpenIndexer) -> None:
        if getattr(self, "_quick_indexer", None) is indexer:
            indexer.cancel()
            self._quick_indexer = None

    def _emit_quick_open_progress(self, epoch: int, scanned: int, done: bool) -> None:
        """``workspace.indexProgress``; dropped when the epoch already moved on."""
        if self._identity is None or epoch != self._epoch:
            return
        self._emit(
            index_progress_event(
                self._identity.workspace_id,
                epoch,
                scanned,
                scanned if done else None,
                done,
            )
        )

    # ─── T10 read-only facts for agent awareness (plan §20) ────────────────
    def resolve_workspace_uri(self, path: str) -> str:
        """Canonical file URI for one workspace-relative path (T10 tool seam).

        Additive read-only helper: ``WorkspaceFiles.normalized_rel`` validates
        the shape (NUL/absolute/``..``/backslash forms are structured
        ``denied`` refusals) and ``_uri_to_rel`` re-runs real containment, so
        a symlink escape or an outside path can never yield a URI.
        """
        files = self._require_files()
        rel = files.normalized_rel(path)
        uri = self._rel_to_uri(rel)
        self._uri_to_rel(uri)  # real-path containment (raises on an escape)
        return uri

    def status_facts(self) -> dict[str, Any] | None:
        """Sync, truth-only facts for self-knowledge and status surfaces.

        ``None`` when no workspace is open. Never file contents, diagnostics
        text or unsaved buffer text: the root *label* (basename only, never
        the full path), trust/epoch, the open-time profile resolution
        snapshot merged with the attached manager's synchronously-readable
        per-language state, and the open-mirror count. A manager whose status
        is only awaitable reports ``unknown`` here; the async ``lsp_status``
        RPC read stays the live path for clients.
        """
        identity = self._identity
        if identity is None:
            return None
        live_states: dict[str, str] = {}
        generations: dict[str, int] = {}
        manager = self._lsp_manager
        if manager is not None:
            status = getattr(manager, "status", None)
            if callable(status) and not inspect.iscoroutinefunction(status):
                try:
                    payload = status()
                except Exception:
                    logger.debug("lsp manager status failed", exc_info=True)
                    payload = None
                if not inspect.isawaitable(payload):
                    rows = payload.get("servers") if isinstance(payload, Mapping) else payload
                    if isinstance(rows, (list, tuple)):
                        for row in rows:
                            if not isinstance(row, Mapping):
                                continue
                            language = row.get("languageId")
                            state = row.get("state")
                            if not isinstance(language, str) or not isinstance(state, str):
                                continue
                            live_states[language] = state
                            generation = row.get("generation")
                            if isinstance(generation, int) and not isinstance(generation, bool):
                                generations[language] = generation
        profiles: dict[str, dict[str, Any]] = {}
        for language in LSP_PROFILE_LANGUAGES:
            resolved = bool(self._profiles.get(language))
            if language in live_states:
                state = live_states[language]
            elif resolved and manager is not None:
                state = STATE_STOPPED  # resolved at open, not started yet
            else:
                state = STATE_MISSING
            profiles[language] = {
                "resolved": resolved,
                "state": state,
                "generation": generations.get(language, 0),
            }
        mirrors = len(self._documents.uris()) if self._documents is not None else 0
        return {
            "available": True,
            "root_label": identity.root_label,
            "trusted": identity.trusted,
            "epoch": identity.epoch,
            "mirrors": mirrors,
            "profiles": profiles,
        }


# ─── active workspace registry (T10, plan §20; one workspace per process) ───
_active_workspace_service: WorkspaceService | None = None


def active_workspace_service() -> WorkspaceService | None:
    """The service an open workspace is bound to in this process, else ``None``.

    ``WorkspaceService.open`` registers the instance and ``close`` clears it,
    so self-knowledge, ``harness_status`` and read-only tools reach the SAME
    instance the gateway opened — never a second service or manager. v1 keeps
    a single workspace per process: the most recently opened instance wins,
    and a service only clears the registry while it is still the active one.
    """
    return _active_workspace_service


def set_active_workspace_service(service: WorkspaceService | None) -> None:
    """Register *service* as the process's active workspace (or ``None``)."""
    global _active_workspace_service
    _active_workspace_service = service


def clear_active_workspace_service(service: WorkspaceService | None = None) -> None:
    """Clear the registry; identity-checked unless called with ``None``."""
    global _active_workspace_service
    if service is None or _active_workspace_service is service:
        _active_workspace_service = None
