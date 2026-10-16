"""Workspace package — file service, document mirrors and gateway surface.

``machina/workspace/lsp.py`` (language-server process infrastructure) is owned
by a separate task and is intentionally NOT imported here: the package must
import cleanly before that module exists, and ``WorkspaceService`` only ever
touches an injected manager.
"""

from __future__ import annotations

from machina.workspace.documents import DocumentMirrorStore
from machina.workspace.files import WorkspaceFiles, revision_registry, write_coordinator
from machina.workspace.models import (
    DEFAULT_EXCLUSIONS,
    DEFAULT_LIST_LIMIT,
    DEFAULT_READ_BYTES,
    INTELLIGENCE_OPERATIONS,
    MAX_DOCUMENT_BYTES,
    MAX_LIST_LIMIT,
    MAX_READ_BYTES,
    MAX_SAVE_BYTES,
    REFUSAL_REASONS,
    TOTAL_MIRROR_BUDGET,
    DocumentChangeResult,
    DocumentOpenResult,
    IntelligenceResult,
    ListEntry,
    ListPage,
    ReadResult,
    SaveConflict,
    SaveResult,
    WorkspaceError,
    WorkspaceIdentity,
    detect_line_ending,
    file_changed_event,
    language_id_for,
    revision_from_bytes,
)
from machina.workspace.service import WorkspaceService

__all__ = [
    "DEFAULT_EXCLUSIONS",
    "DEFAULT_LIST_LIMIT",
    "DEFAULT_READ_BYTES",
    "DocumentChangeResult",
    "DocumentMirrorStore",
    "DocumentOpenResult",
    "INTELLIGENCE_OPERATIONS",
    "IntelligenceResult",
    "ListEntry",
    "ListPage",
    "MAX_DOCUMENT_BYTES",
    "MAX_LIST_LIMIT",
    "MAX_READ_BYTES",
    "MAX_SAVE_BYTES",
    "REFUSAL_REASONS",
    "ReadResult",
    "SaveConflict",
    "SaveResult",
    "TOTAL_MIRROR_BUDGET",
    "WorkspaceError",
    "WorkspaceFiles",
    "WorkspaceIdentity",
    "WorkspaceService",
    "detect_line_ending",
    "file_changed_event",
    "language_id_for",
    "revision_from_bytes",
    "revision_registry",
    "write_coordinator",
]
