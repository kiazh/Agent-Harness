"""Workspace identity, result shapes, revisions and limits (plan §11/§19).

Hand-written mirror of ``ui/src/workspace/protocol.ts``. The camelCase wire
names produced by ``as_dict()`` must match that frozen contract exactly, and
workspace payloads never carry ``sessionId``/``turnId`` — they are a separate
family from the chat event stream and never enter the transcript reducer.

This module is pure data and pure helpers: no filesystem access, so importing
it anywhere (including the gateway feature registry) is cheap and safe.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Any

# ─── Limits (plan §22: initial design limits, disclosed not silently applied) ──
DEFAULT_LIST_LIMIT = 200
MAX_LIST_LIMIT = 200
# Hard bound on directory names scanned before sorting; reaching it is
# disclosed through ``truncated`` rather than pretending the listing is complete.
MAX_LIST_SCAN = 20_000
DEFAULT_READ_BYTES = 1_048_576  # 1 MiB editable-file default
MAX_READ_BYTES = 1_048_576
MAX_SAVE_BYTES = 1_048_576
MAX_DOCUMENT_BYTES = 1_048_576  # per-file client-mirror cap
TOTAL_MIRROR_BUDGET = 16 * 1_048_576  # 16 MiB logical UTF-8 mirror budget
MAX_PATH_CHARS = 4096
MAX_URI_CHARS = 8192
MAX_REVISION_CHARS = 128
MAX_QUERY_CHARS = 2000
MAX_CHANGES_PER_REQUEST = 512
MAX_CHANGE_TEXT_BYTES = 4 * 1_048_576
# Transport guard only: a request whose text cannot fit any legal save after
# UTF-8 encoding is rejected as malformed before it reaches the service.
MAX_SAVE_PARAM_CHARS = 4 * 1_048_576
MAX_POSITION_VALUE = 1_000_000

REVISION_HEX_LEN = 32

#: Navigation exclusions are policy disclosure, not filesystem permissions
#: (plan §12): excluded names are still reported (flagged) and can be entered
#: deliberately; nothing is refused because of them.
DEFAULT_EXCLUSIONS = (".git", "node_modules", "__pycache__", ".venv", "dist", "build", ".cache")

#: Closed allowlist for ``workspace.intelligence`` (plan §19: never an
#: arbitrary raw LSP method tunnel).
INTELLIGENCE_OPERATIONS = (
    "completion",
    "hover",
    "definition",
    "references",
    "symbols",
    "diagnostics",
)

#: Frozen refusal vocabulary from protocol.ts (WorkspaceReadRefusal.reason).
REFUSAL_REASONS = frozenset(
    {"binary", "too-large", "not-regular", "unsupported-encoding", "denied", "missing"}
)

#: Save conflict reasons surfaced in the structured conflict (added to the
#: three protocol.ts-visible fields; never used to silently overwrite).
CONFLICT_REVISION_MISMATCH = "revision-mismatch"
CONFLICT_FILE_EXISTS = "file-exists"
CONFLICT_MISSING_ON_DISK = "missing-on-disk"

_LANGUAGE_BY_EXTENSION = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "typescriptreact",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascriptreact",
    ".json": "json",
    ".jsonc": "jsonc",
    ".md": "markdown",
    ".markdown": "markdown",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".toml": "toml",
    ".ini": "ini",
    ".cfg": "ini",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".kt": "kotlin",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
    ".sh": "shellscript",
    ".bash": "shellscript",
    ".zsh": "shellscript",
    ".css": "css",
    ".scss": "scss",
    ".less": "less",
    ".html": "html",
    ".htm": "html",
    ".xml": "xml",
    ".sql": "sql",
    ".lua": "lua",
    ".r": "r",
    ".pl": "perl",
    ".scala": "scala",
    ".clj": "clojure",
    ".ex": "elixir",
    ".exs": "elixir",
    ".vue": "vue",
    ".svelte": "svelte",
    ".txt": "plaintext",
    ".log": "log",
}

_LANGUAGE_BY_NAME = {
    "Dockerfile": "dockerfile",
    "Makefile": "makefile",
    "CMakeLists.txt": "cmake",
}


def language_id_for(name: str) -> str:
    """Best-effort language id hint from a file name (never authority)."""
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    if base in _LANGUAGE_BY_NAME:
        return _LANGUAGE_BY_NAME[base]
    extension = os.path.splitext(base)[1].lower()
    return _LANGUAGE_BY_EXTENSION.get(extension, "plaintext")


def revision_from_bytes(data: bytes) -> str:
    """Revision token: ``sha256`` first 32 hex + ``:`` + exact byte size."""
    return f"{hashlib.sha256(data).hexdigest()[:REVISION_HEX_LEN]}:{len(data)}"


def detect_line_ending(data: bytes) -> str:
    """Detect LF / CRLF / CR / mixed from raw bytes (never from decoded text).

    A file without any line terminator reports ``LF`` (the canonical default);
    ``mixed`` is reported whenever two or more distinct styles occur.
    """
    crlf = cr = lf = 0
    index = 0
    length = len(data)
    while index < length:
        byte = data[index]
        if byte == 0x0D:
            if index + 1 < length and data[index + 1] == 0x0A:
                crlf += 1
                index += 2
                continue
            cr += 1
        elif byte == 0x0A:
            lf += 1
        index += 1
    if crlf and not cr and not lf:
        return "CRLF"
    if cr and not crlf and not lf:
        return "CR"
    if lf and not crlf and not cr:
        return "LF"
    if crlf or cr or lf:
        return "mixed"
    return "LF"


class WorkspaceError(Exception):
    """Structured workspace refusal (never a bare crash, never a fake result).

    ``reason`` is one of :data:`REFUSAL_REASONS`; handlers render
    :meth:`as_refusal` as ``{"refused": {"reason": ..., "message": ...}}`` so
    clients can distinguish binary/too-large/missing/denied cases
    (protocol.ts ``WorkspaceReadRefusal``).
    """

    def __init__(self, reason: str, message: str) -> None:
        if reason not in REFUSAL_REASONS:
            raise ValueError(f"unknown refusal reason: {reason!r}")
        super().__init__(message)
        self.reason = reason
        self.message = message

    def as_refusal(self) -> dict[str, Any]:
        return {"refused": {"reason": self.reason, "message": self.message}}


# ─── Result shapes (camelCase wire names mirror protocol.ts exactly) ──────────
@dataclass(frozen=True)
class WorkspaceIdentity:
    workspace_id: str
    client_id: str
    epoch: int
    root_label: str
    resolved_root: str
    trusted: bool
    capabilities: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "workspaceId": self.workspace_id,
            "clientId": self.client_id,
            "epoch": self.epoch,
            "rootLabel": self.root_label,
            "resolvedRoot": self.resolved_root,
            "trusted": self.trusted,
            "capabilities": list(self.capabilities),
        }


@dataclass(frozen=True)
class ListEntry:
    name: str
    path: str  # root-relative POSIX path
    kind: str  # "file" | "directory"
    size_bytes: int | None = None
    excluded: bool = False
    symlink: bool = False

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"name": self.name, "path": self.path, "kind": self.kind}
        if self.size_bytes is not None:
            data["sizeBytes"] = self.size_bytes
        if self.excluded:
            data["excluded"] = True
        if self.symlink:
            data["symlink"] = True
        return data


@dataclass(frozen=True)
class ListPage:
    entries: tuple[ListEntry, ...]
    next_cursor: str | None
    exclusions: tuple[str, ...]
    truncated: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "entries": [entry.as_dict() for entry in self.entries],
            "nextCursor": self.next_cursor,
            "exclusions": list(self.exclusions),
            "truncated": self.truncated,
        }


@dataclass(frozen=True)
class ReadResult:
    uri: str
    text: str
    revision: str
    language_id: str
    line_ending: str
    encoding: str = "utf-8"
    read_only: bool = False
    size_bytes: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "uri": self.uri,
            "text": self.text,
            "revision": self.revision,
            "languageId": self.language_id,
            "lineEnding": self.line_ending,
            "encoding": self.encoding,
            "readOnly": self.read_only,
            "sizeBytes": self.size_bytes,
        }


@dataclass(frozen=True)
class SaveConflict:
    disk_revision: str
    base_revision: str
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "diskRevision": self.disk_revision,
            "baseRevision": self.base_revision,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class SaveResult:
    ok: bool
    revision: str | None = None
    saved_ui_version: int | None = None
    conflict: SaveConflict | None = None

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"ok": self.ok}
        if self.revision is not None:
            data["revision"] = self.revision
        if self.saved_ui_version is not None:
            data["savedUiVersion"] = self.saved_ui_version
        if self.conflict is not None:
            data["conflict"] = self.conflict.as_dict()
        return data


@dataclass(frozen=True)
class DocumentOpenResult:
    accepted_version: int
    server_generation: int
    state: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "acceptedVersion": self.accepted_version,
            "serverGeneration": self.server_generation,
            "state": self.state,
        }


@dataclass(frozen=True)
class DocumentChangeResult:
    accepted_version: int
    resync: bool = False

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"acceptedVersion": self.accepted_version}
        if self.resync:
            data["resync"] = True
        return data


@dataclass(frozen=True)
class IntelligenceResult:
    workspace_id: str
    source: str  # "editor-buffer" | "disk"
    uri: str
    document_version: int | None
    server_generation: int | None
    workspace_epoch: int
    operation: str
    state: str
    freshness: str
    truncated: bool
    result: Any = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "workspaceId": self.workspace_id,
            "source": self.source,
            "uri": self.uri,
            "documentVersion": self.document_version,
            "serverGeneration": self.server_generation,
            "workspaceEpoch": self.workspace_epoch,
            "operation": self.operation,
            "state": self.state,
            "freshness": self.freshness,
            "truncated": self.truncated,
            "result": self.result,
        }


def file_changed_event(
    workspace_id: str, epoch: int, uri: str, revision: str, origin: str = "save"
) -> dict[str, Any]:
    """``workspace.fileChanged`` payload (protocol.ts; no chat envelope)."""
    return {
        "type": "workspace.fileChanged",
        "workspaceId": workspace_id,
        "epoch": epoch,
        "uri": uri,
        "revision": revision,
        "origin": origin,
    }
