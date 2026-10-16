"""Client document mirrors (plan §8/§19) — transport state only.

Mirrors exist so later LSP integration (T7/T8) has the editor's current text
and version without re-reading disk. They are **never a source of user edits**:
nothing in this module writes files, and a rejected/stale update is answered
with ``resync`` (or refused) instead of being guessed or silently applied.
Positions are LSP positions in UTF-16 code units over ``\\n``/``\\r\\n``/``\\r``
line breaks; conversion to Python string indices is explicit and bounded.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from machina.workspace.models import (
    MAX_CHANGES_PER_REQUEST,
    MAX_DOCUMENT_BYTES,
    TOTAL_MIRROR_BUDGET,
    DocumentChangeResult,
    DocumentOpenResult,
    WorkspaceError,
)


@dataclass
class _Mirror:
    uri: str
    language_id: str
    version: int
    text: str
    byte_size: int
    epoch: int


def _line_starts(text: str) -> list[int]:
    """Start index of every line (line breaks per LSP: \\n, \\r\\n, \\r)."""
    starts = [0]
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == "\r":
            if index + 1 < length and text[index + 1] == "\n":
                index += 1
            starts.append(index + 1)
        elif char == "\n":
            starts.append(index + 1)
        index += 1
    return starts


def _line_end(text: str, start: int) -> int:
    index = start
    length = len(text)
    while index < length and text[index] not in ("\r", "\n"):
        index += 1
    return index


def _position_to_index(text: str, line: int, character: int, *, starts: list[int]) -> int:
    """Convert an LSP (line, character) to a Python string index.

    ``character`` counts UTF-16 code units within the line. Landing inside a
    surrogate pair rounds down to the nearest code-point boundary; an offset
    past the line end clamps to the line end. A line past EOF raises
    ``ValueError`` so the caller demands a full resync instead of guessing.
    """
    if line < 0 or character < 0 or line >= len(starts):
        raise ValueError("position out of range")
    start = starts[line]
    end = _line_end(text, start)
    consumed = 0
    index = start
    while index < end and consumed < character:
        consumed += 2 if ord(text[index]) > 0xFFFF else 1
        index += 1
    if consumed > character:
        index -= 1  # never split a surrogate pair
    return index


def apply_changes(text: str, changes: list[dict[str, Any]]) -> str:
    """Apply incremental LSP changes sequentially to *text*.

    Each change's range is interpreted against the result of the previous one
    (LSP semantics). Any malformed range raises ``ValueError``; the caller
    converts that into a resync demand rather than a fabricated result.
    """
    if len(changes) > MAX_CHANGES_PER_REQUEST:
        raise ValueError("too many changes")
    updated = text
    for change in changes:
        if not isinstance(change, dict):
            raise ValueError("change must be an object")
        span = change.get("range")
        new_text = change.get("text")
        if not isinstance(span, dict) or not isinstance(new_text, str):
            raise ValueError("change requires range and text")
        start = span.get("start")
        end = span.get("end")
        if not isinstance(start, dict) or not isinstance(end, dict):
            raise ValueError("range requires start and end")
        starts = _line_starts(updated)
        try:
            start_index = _position_to_index(
                updated, int(start["line"]), int(start["character"]), starts=starts
            )
            end_index = _position_to_index(
                updated, int(end["line"]), int(end["character"]), starts=starts
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("change range is invalid") from error
        if end_index < start_index:
            raise ValueError("change range is inverted")
        updated = updated[:start_index] + new_text + updated[end_index:]
    return updated


def utf16_full_range(text: str) -> dict[str, dict[str, int]]:
    """LSP range covering an entire document, in UTF-16 code units.

    Used when a deliberate full-snapshot change has to be expressed as an
    incremental edit (sync kind 2) or when a reopen jumps versions: the range
    is computed with the same ``\\n``/``\\r\\n``/``\\r`` line rules as
    :func:`apply_changes`, and the end position counts UTF-16 code units
    exactly like an LSP client would.
    """
    starts = _line_starts(text)
    tail = text[starts[-1] :]
    return {
        "start": {"line": 0, "character": 0},
        "end": {"line": len(starts) - 1, "character": len(tail.encode("utf-16-le")) // 2},
    }


class DocumentMirrorStore:
    """Per-(uri) client mirrors with versions, epochs and byte budgets."""

    def __init__(
        self,
        *,
        epoch: int,
        generation: int = 1,
        per_file_bytes: int = MAX_DOCUMENT_BYTES,
        total_bytes: int = TOTAL_MIRROR_BUDGET,
    ) -> None:
        self.epoch = epoch
        self.per_file_bytes = per_file_bytes
        self.total_bytes = total_bytes
        self._mirrors: dict[str, _Mirror] = {}
        self._generation = generation

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def used_bytes(self) -> int:
        return sum(mirror.byte_size for mirror in self._mirrors.values())

    def _check_epoch(self, epoch: int) -> None:
        if epoch != self.epoch:
            raise WorkspaceError("denied", "stale workspace epoch; the workspace was reopened")

    def _enforce_budgets(self, uri: str, size: int, *, replacing: int) -> None:
        if size > self.per_file_bytes:
            raise WorkspaceError(
                "too-large",
                f"document is {size} bytes; the per-file mirror cap is {self.per_file_bytes}",
            )
        if self.used_bytes - replacing + size > self.total_bytes:
            raise WorkspaceError(
                "too-large",
                f"document mirror budget ({self.total_bytes} bytes) exceeded",
            )

    def open_document(
        self,
        uri: str,
        language_id: str,
        version: int,
        text: str,
        *,
        epoch: int,
    ) -> DocumentOpenResult:
        """Accept a mirror open/reopen.

        Versions must strictly increase: an open at or below the current
        version is rejected as stale and the accepted version remains the
        current one. A strictly higher version reopens (replaces) the mirror.
        """
        self._check_epoch(epoch)
        if not isinstance(version, int) or isinstance(version, bool) or version < 0:
            raise WorkspaceError("denied", "document version must be a non-negative integer")
        size = len(text.encode("utf-8"))
        existing = self._mirrors.get(uri)
        if existing is not None and version <= existing.version:
            return DocumentOpenResult(
                accepted_version=existing.version,
                server_generation=self._generation,
                state="ready",
            )
        self._enforce_budgets(uri, size, replacing=existing.byte_size if existing else 0)
        self._mirrors[uri] = _Mirror(
            uri=uri,
            language_id=language_id,
            version=version,
            text=text,
            byte_size=size,
            epoch=epoch,
        )
        return DocumentOpenResult(
            accepted_version=version,
            server_generation=self._generation,
            state="ready",
        )

    def change_document(
        self,
        uri: str,
        base_version: int,
        version: int,
        changes: list[dict[str, Any]],
        *,
        epoch: int,
    ) -> DocumentChangeResult:
        """Apply incremental changes or demand a full resync.

        Rejected without mutation: unknown URI, non-monotonic versions, a
        base version that does not match the current mirror, or a malformed
        range. Budget overflows raise a structured refusal.
        """
        self._check_epoch(epoch)
        mirror = self._mirrors.get(uri)
        if mirror is None:
            return DocumentChangeResult(accepted_version=0, resync=True)
        if (
            not isinstance(base_version, int)
            or isinstance(base_version, bool)
            or not isinstance(version, int)
            or isinstance(version, bool)
            or base_version != mirror.version
            or version <= base_version
        ):
            return DocumentChangeResult(accepted_version=mirror.version, resync=True)
        try:
            updated = apply_changes(mirror.text, changes)
        except (TypeError, ValueError):
            return DocumentChangeResult(accepted_version=mirror.version, resync=True)
        size = len(updated.encode("utf-8"))
        self._enforce_budgets(uri, size, replacing=mirror.byte_size)
        mirror.text = updated
        mirror.byte_size = size
        mirror.version = version
        return DocumentChangeResult(accepted_version=version, resync=False)

    def close_document(self, uri: str, *, epoch: int) -> bool:
        self._check_epoch(epoch)
        return self._mirrors.pop(uri, None) is not None

    def snapshot(self, uri: str) -> dict[str, Any] | None:
        """Current mirror state for transport inspection (never an edit source)."""
        mirror = self._mirrors.get(uri)
        if mirror is None:
            return None
        return {
            "uri": mirror.uri,
            "languageId": mirror.language_id,
            "version": mirror.version,
            "text": mirror.text,
            "byteSize": mirror.byte_size,
            "epoch": mirror.epoch,
        }

    def uris(self) -> tuple[str, ...]:
        return tuple(sorted(self._mirrors))
