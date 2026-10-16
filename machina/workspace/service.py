"""WorkspaceService — identity, file access, mirrors and intelligence dispatch.

One service instance per gateway (plan §19). ``open(root, trust)`` mints a
backend-issued workspace identity bound to the resolved root: ``workspaceId``,
``clientId`` and a monotonic ``epoch``. The epoch accompanies every mirror
and intelligence result so responses from an old workspace can never be
mistaken for the new one.

The optional language-server manager (``machina/workspace/lsp.py``, owned by
the T7 task) is injected with :meth:`set_lsp_manager`. It is deliberately not
imported here: when it is absent — or present but unable to answer — the
service returns truthful ``missing``/``failed`` states instead of fabricated
results (plan §13/§19: never a fake LSP result, never a shell tunnel).

Workspace events are a separate family from the chat stream: they carry
``workspaceId``/``epoch`` and never ``sessionId``/``turnId``. The injected
``send_event`` callback receives the bare event payload; the gateway feature
layer wraps it in the JSON-RPC notification frame.
"""

from __future__ import annotations

import inspect
import logging
import os
import secrets
import urllib.parse
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from machina.workspace.documents import DocumentMirrorStore
from machina.workspace.files import WorkspaceFiles
from machina.workspace.models import (
    DEFAULT_LIST_LIMIT,
    INTELLIGENCE_OPERATIONS,
    MAX_READ_BYTES,
    DocumentChangeResult,
    DocumentOpenResult,
    IntelligenceResult,
    ListPage,
    ReadResult,
    SaveResult,
    WorkspaceError,
    WorkspaceIdentity,
    file_changed_event,
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


async def _await_if_needed(value: Any) -> Any:
    """Await a manager call only when it is actually awaitable."""
    if inspect.isawaitable(value):
        return await value
    return value


class WorkspaceService:
    """Bounds and structured outcomes for one authorized workspace identity."""

    def __init__(self, *, send_event: SendEvent | None = None) -> None:
        self._send_event: SendEvent = send_event if send_event is not None else (lambda _e: None)
        self._lsp_manager: Any | None = None
        self._files: WorkspaceFiles | None = None
        self._documents: DocumentMirrorStore | None = None
        self._identity: WorkspaceIdentity | None = None
        self._epoch = 0
        self._generation = 0

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
        """Inject the optional language-server manager (T7 integration point)."""
        self._lsp_manager = manager

    def open(self, root: str, trust: bool) -> WorkspaceIdentity:
        """Open (or reopen) the workspace; explicit trust decision required.

        Reopening mints a fresh identity and epoch and drops all mirrors, so a
        stale client can no longer write documents or reads into the new
        workspace binding.
        """
        if not isinstance(trust, bool):
            raise WorkspaceError("denied", "an explicit boolean trust decision is required")
        files = WorkspaceFiles(root)
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
        return self._identity

    def close(self) -> dict[str, Any]:
        """Retire mirrors and the file binding; the manager is not ours to stop."""
        self._documents = None
        self._files = None
        self._identity = None
        self._generation += 1
        return {"closed": True}

    def _require_files(self) -> WorkspaceFiles:
        if self._files is None or self._identity is None:
            raise WorkspaceError("denied", "no workspace is open; call workspace.open first")
        return self._files

    def _require_documents(self) -> DocumentMirrorStore:
        self._require_files()
        assert self._documents is not None  # guarded by _require_files
        return self._documents

    def _check_epoch(self, epoch: int | None) -> int:
        effective = self._epoch if epoch is None else epoch
        if (
            not isinstance(effective, int)
            or isinstance(effective, bool)
            or effective != self._epoch
        ):
            raise WorkspaceError("denied", "stale workspace epoch; the workspace was reopened")
        return effective

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
        # Canonicalize platform symlinks in the path prefix (e.g. macOS
        # /tmp -> /private/tmp) so a URI the client builds from the path it
        # requested still maps into the resolved root. This is URI
        # canonicalization only — authority remains with resolve() at the
        # I/O boundary, and an outside target still fails containment below.
        try:
            path = os.path.realpath(path)
        except OSError as error:
            raise WorkspaceError("denied", "uri is not accessible") from error
        root_str = str(files.root)
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
    ) -> SaveResult:
        files = self._require_files()
        rel = self._uri_to_rel(uri)
        result = await files.save_text(rel, text, expected_revision, ui_version=ui_version)
        if result.ok and result.revision is not None and self._identity is not None:
            self._emit(
                file_changed_event(
                    self._identity.workspace_id,
                    self._epoch,
                    self._rel_to_uri(rel),
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
    ) -> DocumentOpenResult:
        store = self._require_documents()
        canonical = self._rel_to_uri(self._uri_to_rel(uri))
        result = store.open_document(
            canonical, language_id, version, text, epoch=self._check_epoch(epoch)
        )
        return replace(result, state=self._document_state(language_id))

    def document_change(
        self,
        uri: str,
        base_version: int,
        version: int,
        changes: list[dict[str, Any]],
        *,
        epoch: int | None = None,
    ) -> DocumentChangeResult:
        store = self._require_documents()
        canonical = self._rel_to_uri(self._uri_to_rel(uri))
        return store.change_document(
            canonical, base_version, version, changes, epoch=self._check_epoch(epoch)
        )

    def document_close(self, uri: str, *, epoch: int | None = None) -> dict[str, Any]:
        store = self._require_documents()
        canonical = self._rel_to_uri(self._uri_to_rel(uri))
        closed = store.close_document(canonical, epoch=self._check_epoch(epoch))
        return {"closed": closed, "uri": canonical}

    def document_snapshot(self, uri: str) -> dict[str, Any] | None:
        """Transport-state inspection for tests and later LSP synchronization."""
        if self._documents is None:
            return None
        canonical = self._rel_to_uri(self._uri_to_rel(uri))
        return self._documents.snapshot(canonical)

    def _document_state(self, language_id: str) -> str:
        """Truthful server state for a document; never a fabricated ``ready``."""
        if self._lsp_manager is None:
            return "missing"
        state_for = getattr(self._lsp_manager, "state_for_language", None)
        if not callable(state_for):
            return "degraded"
        try:
            value = state_for(language_id)
        except Exception:
            logger.warning("lsp manager state_for_language failed", exc_info=True)
            return "degraded"
        return str(value) if value else "degraded"

    # ─── intelligence and language-server lifecycle ─────────────────────────
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
        """Closed-allowlist intelligence dispatch (no raw LSP method tunnel)."""
        self._require_files()
        if operation not in INTELLIGENCE_OPERATIONS:
            raise WorkspaceError("denied", f"unsupported intelligence operation: {operation}")
        workspace_epoch = self._check_epoch(epoch)

        canonical_uri = ""
        source = "disk"
        document_version: int | None = version
        if uri:
            canonical_uri = self._rel_to_uri(self._uri_to_rel(uri))
            mirror = self.document_snapshot(canonical_uri) if self._documents else None
            if mirror is not None:
                source = "editor-buffer"
                if version is None:
                    document_version = int(mirror["version"])

        def result_for(
            state: str,
            freshness: str,
            payload: Any = None,
            generation: Any = None,
            truncated: bool = False,
        ) -> IntelligenceResult:
            return IntelligenceResult(
                workspace_id=self._identity.workspace_id if self._identity else "",
                source=source,
                uri=canonical_uri,
                document_version=document_version,
                server_generation=generation if isinstance(generation, int) else None,
                workspace_epoch=workspace_epoch,
                operation=operation,
                state=state,
                freshness=freshness,
                truncated=truncated,
                result=payload,
            )

        manager = self._lsp_manager
        if manager is None:
            # Honest unavailability: no manager, no fabricated result.
            return result_for("missing", "unavailable")
        dispatch = getattr(manager, "intelligence", None)
        if not callable(dispatch):
            return result_for("missing", "unavailable")
        request = {
            "operation": operation,
            "uri": canonical_uri or None,
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
        generation = payload.get("serverGeneration")
        return IntelligenceResult(
            workspace_id=self._identity.workspace_id if self._identity else "",
            source=resolved_source,
            uri=canonical_uri,
            document_version=document_version,
            server_generation=generation if isinstance(generation, int) else None,
            workspace_epoch=workspace_epoch,
            operation=operation,
            state=state,
            freshness=freshness,
            truncated=bool(payload.get("truncated")),
            result=payload.get("result"),
        )

    async def lsp_status(self) -> dict[str, Any]:
        """Server rows when a manager is attached; empty list otherwise."""
        self._require_files()
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
        if isinstance(payload, dict) and isinstance(payload.get("servers"), list):
            return {"servers": payload["servers"]}
        if isinstance(payload, list):
            return {"servers": payload}
        return {"servers": []}

    async def lsp_control(self, action: str, profile: str) -> dict[str, Any]:
        """start/stop/restart through the injected manager; never installation."""
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
                "state": "missing",
                "detail": "no language-server manager is configured; setup is explicit",
            }
        control = getattr(manager, "control", None)
        if not callable(control):
            return {
                "action": action,
                "profile": profile,
                "state": "missing",
                "detail": "language-server manager does not support control requests",
            }
        try:
            payload = await _await_if_needed(control(action, profile))
        except Exception as error:
            logger.warning("lsp manager control failed", exc_info=True)
            return {
                "action": action,
                "profile": profile,
                "state": "failed",
                "detail": f"{type(error).__name__}: {error}"[:300],
            }
        result: dict[str, Any] = {"action": action, "profile": profile}
        if isinstance(payload, dict):
            result.update(payload)
        return result
