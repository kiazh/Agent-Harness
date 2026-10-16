"""Authorized workspace file I/O (plan §11).

Security model (senior-security; plan §11/§24F):

* Authorization is **never** a textual path prefix. The root is resolved once
  with ``os.path.realpath``; every entry point re-resolves the candidate and
  requires the result to stay inside that root (this catches ``..``, absolute
  paths, symlinked files and symlinked ancestor directories that point
  outside). Symlinks that stay inside the root may be followed for reads.
* Reads are fd-verified: the descriptor's ``(dev, ino)`` must match the
  lstat of the resolved path, and the descriptor must be a regular file.
  Non-blocking opens prevent FIFO/device swaps from freezing the loop.
* Writes walk the parent directory from the trusted root with
  ``openat`` + ``O_NOFOLLOW|O_DIRECTORY`` (mirroring ``machina/tools/file.py``)
  so a concurrently swapped ancestor cannot redirect the write; the probe,
  temp creation and replacement are all descriptor-anchored. Hard-linked
  targets (``st_nlink > 1``) and read-only targets are refused, modes are
  preserved, and commits are same-directory temp + fsync + ``renameat``.
* Platforms without ``dir_fd`` support (notably Windows) take a documented
  pathname fallback that re-checks every ancestor for symlinks before
  ``os.replace``. The guarantees are weaker there and stated honestly.

All refusals are :class:`~machina.workspace.models.WorkspaceError` values with
the frozen protocol reasons. Bytes are never guessed: NUL bytes / invalid
UTF-8 are refused as ``binary``, UTF-16/32 BOMs as ``unsupported-encoding``.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import stat
import tempfile
from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from machina.workspace.models import (
    CONFLICT_FILE_EXISTS,
    CONFLICT_MISSING_ON_DISK,
    CONFLICT_REVISION_MISMATCH,
    DEFAULT_EXCLUSIONS,
    DEFAULT_LIST_LIMIT,
    MAX_LIST_LIMIT,
    MAX_LIST_SCAN,
    MAX_PATH_CHARS,
    MAX_READ_BYTES,
    MAX_SAVE_BYTES,
    ListEntry,
    ListPage,
    ReadResult,
    SaveConflict,
    SaveResult,
    WorkspaceError,
    detect_line_ending,
    language_id_for,
    revision_from_bytes,
)

# Indirection for deterministic race tests (mirrors machina/tools/file.py):
# tests monkeypatch these seams, never real user files.
_os_open = os.open

_UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")
_UTF32_BOMS = (b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")


class RevisionRegistry:
    """In-process registry of the last revision observed per resolved path.

    Cooperative writers (editor saves, agent file writes later) share one
    revision contract. The registry is an observation record used for
    diagnostics and same-path conflict reporting; the authoritative conflict
    check always re-hashes the bytes on disk under the per-path write lock.
    """

    def __init__(self) -> None:
        self._revisions: dict[str, str] = {}

    def get(self, path: str | os.PathLike[str]) -> str | None:
        return self._revisions.get(os.fspath(path))

    def record(self, path: str | os.PathLike[str], revision: str) -> None:
        self._revisions[os.fspath(path)] = revision

    def forget(self, path: str | os.PathLike[str]) -> None:
        self._revisions.pop(os.fspath(path), None)


class _PathWriteCoordinator:
    """Per-path asyncio locks keyed by resolved path (one shared instance).

    Module-level (single implementation for every workspace in this
    process) so two workspace identities over the same root still serialize.
    Entries are refcounted per event loop and removed when idle, so tests
    with many short-lived loops do not accumulate locks. The lock is held
    across the synchronous bounded disk work; cancellation of the awaiting
    task cannot interleave a second writer because the whole commit runs in
    the critical section without await points. This is an asyncio primitive
    for the gateway's single event loop, not a cross-thread lock.
    """

    def __init__(self) -> None:
        self._entries: dict[
            tuple[int, str], tuple[asyncio.AbstractEventLoop, asyncio.Lock, int]
        ] = {}

    @asynccontextmanager
    async def hold(self, path: str | os.PathLike[str]) -> Iterator[None]:
        key = os.fspath(path)
        loop = asyncio.get_running_loop()
        entry_key = (id(loop), key)
        entry = self._entries.get(entry_key)
        if entry is None:
            entry = (loop, asyncio.Lock(), 0)
        self._entries[entry_key] = (entry[0], entry[1], entry[2] + 1)
        try:
            async with entry[1]:
                yield
        finally:
            current = self._entries.get(entry_key)
            if current is not None:
                loop_ref, lock_ref, refs = current
                if refs <= 1:
                    self._entries.pop(entry_key, None)
                else:
                    self._entries[entry_key] = (loop_ref, lock_ref, refs - 1)


#: Process-wide cooperative writer coordinator and revision registry.
write_coordinator = _PathWriteCoordinator()
revision_registry = RevisionRegistry()


def _dir_fd_supported() -> bool:
    """Whether this platform supports dir_fd-anchored file operations."""
    try:
        supported = os.supports_dir_fd
    except AttributeError:
        return False
    return all(function in supported for function in (os.open, os.rename, os.stat))


def _nofollow() -> int:
    return getattr(os, "O_NOFOLLOW", 0)


def _nonblock() -> int:
    return getattr(os, "O_NONBLOCK", 0)


def _dir_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _nofollow()


def _fsync_dir_best_effort(directory: Path) -> None:
    """Persist a rename on POSIX; unavailable directory fsync is not fatal."""
    try:
        dir_fd = _os_open(directory, _dir_flags())
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        try:
            os.close(dir_fd)
        except OSError:
            pass


class WorkspaceFiles:
    """Bounded, containment-checked I/O for one authorized workspace root."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        exclusions: tuple[str, ...] = DEFAULT_EXCLUSIONS,
    ) -> None:
        try:
            resolved = os.path.realpath(os.fspath(root), strict=True)
        except OSError as error:
            raise WorkspaceError("missing", f"workspace root is not accessible: {error}") from error
        path = Path(resolved)
        if not path.is_dir():
            raise WorkspaceError("not-regular", "workspace root is not a directory")
        self.root = path
        self.exclusions = tuple(exclusions)

    # ─── path resolution ────────────────────────────────────────────────────
    def normalized_rel(self, rel: str) -> str:
        """Validate a root-relative POSIX path; raise a structured refusal.

        Rejects NUL bytes, absolute/Windows-drive forms, backslashes,
        ``..`` components and over-long input. Returns the normalized
        (``.``/empty-component-free) relative path; empty string is the root.
        """
        if not isinstance(rel, str):
            raise WorkspaceError("denied", "path must be a string")
        if "\x00" in rel:
            raise WorkspaceError("denied", "path contains a NUL byte")
        if len(rel) > MAX_PATH_CHARS:
            raise WorkspaceError("denied", f"path exceeds {MAX_PATH_CHARS} characters")
        if rel.startswith("/") or rel.startswith("\\") or os.path.isabs(rel):
            raise WorkspaceError("denied", "path must be relative to the workspace root")
        if len(rel) >= 2 and rel[1] == ":":
            raise WorkspaceError("denied", "path must not carry a drive/volume prefix")
        if "\\" in rel:
            raise WorkspaceError("denied", "path must use POSIX separators")
        parts = [part for part in rel.split("/") if part not in ("", ".")]
        if any(part == ".." for part in parts):
            raise WorkspaceError("denied", "path must not contain '..' components")
        return "/".join(parts)

    def resolve(self, rel: str) -> Path:
        """Resolve *rel* under the root and enforce real containment.

        Returns the fully resolved path (symlinks inside the root are
        followed). The caller still treats the path as untrusted input: every
        read/write re-verifies the descriptor or re-walks the parent chain.
        """
        normalized = self.normalized_rel(rel)
        candidate = self.root if not normalized else self.root / normalized
        if not candidate.is_relative_to(self.root):
            # Defense in depth; normalized_rel already removed traversal.
            raise WorkspaceError("denied", "path escapes the authorized workspace root")
        try:
            resolved = Path(os.path.realpath(candidate))
        except OSError as error:
            raise WorkspaceError("denied", f"path is not accessible: {error}") from error
        if resolved != self.root and not resolved.is_relative_to(self.root):
            raise WorkspaceError("denied", "path escapes the authorized workspace root")
        return resolved

    # ─── reads ──────────────────────────────────────────────────────────────
    def read_text(self, rel: str, *, max_bytes: int = MAX_READ_BYTES) -> ReadResult:
        """Bounded UTF-8 read with fd verification and structured refusals."""
        resolved = self.resolve(rel)
        try:
            st_path = os.lstat(resolved)
        except FileNotFoundError as error:
            raise WorkspaceError("missing", f"file not found: {rel}") from error
        except OSError as error:
            raise WorkspaceError("denied", f"path is not accessible: {rel}") from error
        if stat.S_ISDIR(st_path.st_mode):
            raise WorkspaceError("not-regular", "path is a directory, not a file")

        flags = os.O_RDONLY | _nofollow() | _nonblock()
        try:
            fd = _os_open(resolved, flags)
        except FileNotFoundError as error:
            raise WorkspaceError("missing", f"file not found: {rel}") from error
        except PermissionError as error:
            raise WorkspaceError("denied", f"read permission denied: {rel}") from error
        except OSError as error:
            raise WorkspaceError("denied", f"path is not accessible (symlink?): {rel}") from error
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise WorkspaceError("not-regular", "path is not a regular file")
            self._verify_fd(fd, resolved, display=rel)
            if st.st_size > max_bytes:
                raise WorkspaceError(
                    "too-large",
                    f"file is {st.st_size} bytes; the default editable-file limit is {max_bytes}",
                )
            chunks: list[bytes] = []
            remaining = max_bytes + 1
            while remaining > 0:
                chunk = os.read(fd, min(1 << 16, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            read_only = st.st_nlink > 1 or stat.S_IMODE(st.st_mode) & 0o222 == 0
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

        if len(data) > max_bytes:
            raise WorkspaceError("too-large", f"file exceeds the {max_bytes}-byte read limit")
        # A UTF-16/32 BOM is a positive identification of a known text
        # encoding we do not support; it must be reported before the NUL scan
        # because those encodings legitimately contain NUL bytes.
        if data.startswith(_UTF32_BOMS) or data.startswith(_UTF16_BOMS):
            raise WorkspaceError(
                "unsupported-encoding",
                "file uses a UTF-16/UTF-32 byte-order mark; only UTF-8 text is supported",
            )
        if b"\x00" in data:
            raise WorkspaceError(
                "binary", "file contains NUL bytes; refusing to guess its encoding"
            )
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise WorkspaceError(
                "binary", "file is not valid UTF-8 text; refusing to guess its encoding"
            ) from error

        revision = revision_from_bytes(data)
        revision_registry.record(resolved, revision)
        return ReadResult(
            uri="",  # the service fills the canonical client URI
            text=text,
            revision=revision,
            language_id=language_id_for(resolved.name),
            line_ending=detect_line_ending(data),
            read_only=read_only,
            size_bytes=len(data),
        )

    @staticmethod
    def _verify_fd(fd: int, candidate: Path, *, display: str) -> None:
        """fstat/lstat dev+ino equality (TOCTOU guard, no /proc dependency)."""
        try:
            st_fd = os.fstat(fd)
            st_path = os.lstat(candidate)
        except OSError as error:
            raise WorkspaceError(
                "denied", f"path changed during access (TOCTOU): {display}"
            ) from error
        if stat.S_ISLNK(st_path.st_mode):
            raise WorkspaceError("denied", f"path is a symlink: {display}")
        if (st_fd.st_ino, st_fd.st_dev) != (st_path.st_ino, st_path.st_dev):
            raise WorkspaceError("denied", f"path changed during access (TOCTOU): {display}")

    # ─── listing ────────────────────────────────────────────────────────────
    def list_dir(
        self,
        rel: str = "",
        *,
        cursor: str | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
        include_hidden: bool = False,
    ) -> ListPage:
        """One bounded directory page: dirs first, deterministic ties.

        Sort order is ``(kind, casefold(name), name)`` so identical
        case-folded names still have a deterministic order. The index cursor
        is stable for an unchanged directory snapshot. Exclusion names are
        reported on entries (flagged) and disclosed in ``exclusions``;
        hidden names are omitted unless ``include_hidden`` is set.
        """
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= MAX_LIST_LIMIT
        ):
            raise WorkspaceError("denied", f"limit must be between 1 and {MAX_LIST_LIMIT}")
        offset = 0
        if cursor is not None:
            if not (isinstance(cursor, str) and cursor.isascii() and cursor.isdigit()):
                raise WorkspaceError("denied", "cursor must be a non-negative integer string")
            offset = int(cursor)
            if offset > MAX_LIST_SCAN:
                raise WorkspaceError("denied", "cursor is out of range")

        normalized = self.normalized_rel(rel)
        resolved = self.resolve(normalized)
        try:
            st = os.stat(resolved)
        except FileNotFoundError as error:
            raise WorkspaceError("missing", f"directory not found: {rel or '.'}") from error
        except PermissionError as error:
            raise WorkspaceError("denied", f"permission denied: {rel or '.'}") from error
        except OSError as error:
            raise WorkspaceError("denied", f"directory is not accessible: {rel or '.'}") from error
        if not stat.S_ISDIR(st.st_mode):
            raise WorkspaceError("not-regular", "path is not a directory")

        scanned = 0
        scan_truncated = False
        entries: list[ListEntry] = []
        try:
            with os.scandir(resolved) as iterator:
                for raw in iterator:
                    scanned += 1
                    if scanned > MAX_LIST_SCAN:
                        scan_truncated = True
                        break
                    name = raw.name
                    if name.startswith(".") and not include_hidden:
                        continue
                    entries.append(self._entry_from_scandir(raw, normalized))
        except PermissionError as error:
            raise WorkspaceError(
                "denied", f"permission denied while listing: {rel or '.'}"
            ) from error
        except FileNotFoundError as error:
            raise WorkspaceError("missing", f"directory not found: {rel or '.'}") from error
        except OSError as error:
            raise WorkspaceError("denied", f"directory is not accessible: {rel or '.'}") from error

        entries.sort(key=lambda e: (0 if e.kind == "directory" else 1, e.name.casefold(), e.name))
        page = entries[offset : offset + limit]
        truncated = scan_truncated or offset + limit < len(entries)
        next_cursor = str(offset + limit) if truncated else None
        return ListPage(
            entries=tuple(page),
            next_cursor=next_cursor,
            exclusions=tuple(sorted(self.exclusions)),
            truncated=truncated,
        )

    def _entry_from_scandir(self, raw: os.DirEntry[str], parent_rel: str) -> ListEntry:
        """One listing row; symlinks are flagged and never followed outside."""
        name = raw.name
        path = f"{parent_rel}/{name}" if parent_rel else name
        excluded = name in self.exclusions
        is_symlink = raw.is_symlink()
        kind = "file"
        size: int | None = None
        if is_symlink:
            # Only follow an inside-root symlink for display metadata; an
            # outside-root target is reported as a flagged, unsized file row
            # and is refused by resolve() if opened.
            try:
                target = Path(os.path.realpath(raw.path))
            except OSError:
                target = None
            if target is not None and (target == self.root or target.is_relative_to(self.root)):
                try:
                    st = os.stat(raw.path)
                    if stat.S_ISDIR(st.st_mode):
                        kind = "directory"
                    elif stat.S_ISREG(st.st_mode):
                        size = st.st_size
                except OSError:
                    pass
        else:
            try:
                st = raw.stat(follow_symlinks=False)
                if stat.S_ISDIR(st.st_mode):
                    kind = "directory"
                elif stat.S_ISREG(st.st_mode):
                    size = st.st_size
            except OSError:
                # Unreadable metadata (e.g. permission denied): report the row
                # conservatively rather than dropping it silently.
                try:
                    if raw.is_dir(follow_symlinks=False):
                        kind = "directory"
                except OSError:
                    pass
        return ListEntry(
            name=name,
            path=path,
            kind=kind,
            size_bytes=size,
            excluded=excluded,
            symlink=is_symlink,
        )

    # ─── writes ─────────────────────────────────────────────────────────────
    async def save_text(
        self,
        rel: str,
        text: str,
        expected_revision: str | None = None,
        *,
        ui_version: int | None = None,
    ) -> SaveResult:
        """Cooperative compare-and-swap save; never silently overwrites.

        ``expected_revision`` is the revision the caller read. ``None``/``""``
        means "expect the file to be missing" (create). A mismatch returns a
        structured :class:`SaveConflict` with the current disk revision. The
        per-path write lock serializes cooperating writers; the expected
        revision is re-checked immediately before the atomic commit.
        """
        try:
            data = text.encode("utf-8")
        except UnicodeEncodeError as error:
            raise WorkspaceError(
                "unsupported-encoding", "text contains code points that are not valid UTF-8"
            ) from error
        if len(data) > MAX_SAVE_BYTES:
            raise WorkspaceError(
                "too-large", f"text is {len(data)} bytes; the save limit is {MAX_SAVE_BYTES}"
            )
        normalized = self.normalized_rel(rel)
        if not normalized:
            raise WorkspaceError("denied", "cannot save to the workspace root")
        resolved = self.resolve(normalized)
        if resolved == self.root:
            raise WorkspaceError("denied", "cannot save to the workspace root")

        base = expected_revision or None
        async with write_coordinator.hold(resolved):
            disk_revision = self._disk_revision(resolved, display=normalized)
            if base != disk_revision:
                if disk_revision is None:
                    reason = CONFLICT_MISSING_ON_DISK
                elif base is None:
                    reason = CONFLICT_FILE_EXISTS
                else:
                    reason = CONFLICT_REVISION_MISMATCH
                return SaveResult(
                    ok=False,
                    conflict=SaveConflict(
                        disk_revision=disk_revision or "",
                        base_revision=base or "",
                        reason=reason,
                    ),
                )
            self._write_target(resolved, data, display=normalized)
            revision = revision_from_bytes(data)
            revision_registry.record(resolved, revision)
            return SaveResult(ok=True, revision=revision, saved_ui_version=ui_version)

    def _disk_revision(self, resolved: Path, *, display: str) -> str | None:
        """Current on-disk revision, or ``None`` when the file is missing."""
        try:
            fd = _os_open(resolved, os.O_RDONLY | _nofollow() | _nonblock())
        except FileNotFoundError:
            return None
        except PermissionError as error:
            raise WorkspaceError("denied", f"permission denied: {display}") from error
        except OSError as error:
            raise WorkspaceError("denied", f"path is not accessible: {display}") from error
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise WorkspaceError("not-regular", f"target is not a regular file: {display}")
            digest = hashlib.sha256()
            size = 0
            while True:
                chunk = os.read(fd, 1 << 20)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
            return f"{digest.hexdigest()[:32]}:{size}"
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    def _write_target(self, resolved: Path, data: bytes, *, display: str) -> None:
        if _dir_fd_supported():
            self._write_target_anchored(resolved, data, display=display)
        else:
            # Documented weaker fallback (e.g. Windows): ancestor symlinks are
            # re-checked before os.replace, but the check/open sequence is not
            # descriptor-anchored on such platforms.
            self._write_target_fallback(resolved, data, display=display)

    def _write_target_anchored(self, resolved: Path, data: bytes, *, display: str) -> None:
        try:
            rel_parent = resolved.parent.relative_to(self.root)
        except ValueError as error:
            raise WorkspaceError("denied", "path escapes the authorized workspace root") from error
        try:
            dir_fd = _os_open(self.root, _dir_flags())
        except OSError as error:
            raise WorkspaceError(
                "denied", f"workspace root is not accessible: {display}"
            ) from error
        try:
            for part in rel_parent.parts:
                if part in ("", "."):
                    continue
                try:
                    next_fd = _os_open(part, _dir_flags(), dir_fd=dir_fd)
                except FileNotFoundError as error:
                    raise WorkspaceError(
                        "missing", f"parent directory does not exist: {display}"
                    ) from error
                except OSError as error:
                    raise WorkspaceError(
                        "denied", f"path traverses a symlink or non-directory: {display}"
                    ) from error
                os.close(dir_fd)
                dir_fd = next_fd
            self._commit_at(dir_fd, resolved.name, data, display=display)
        finally:
            try:
                os.close(dir_fd)
            except OSError:
                pass

    def _commit_at(self, dir_fd: int, leaf: str, data: bytes, *, display: str) -> None:
        """Probe + atomic replace relative to a validated directory descriptor."""
        try:
            st_before = os.stat(leaf, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            st_before = None
        except OSError as error:
            raise WorkspaceError("denied", f"target is not accessible: {display}") from error
        mode = self._check_writable_target(st_before, display=display)

        nofollow = _nofollow()
        tmp_fd: int | None = None
        tmp_name: str | None = None
        for _ in range(8):
            candidate = f".{leaf}.{secrets.token_hex(8)}.tmp"
            try:
                tmp_fd = _os_open(
                    candidate,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow,
                    0o600,
                    dir_fd=dir_fd,
                )
            except FileExistsError:
                continue
            except OSError as error:
                raise WorkspaceError("denied", f"cannot write {display}: {error}") from error
            tmp_name = candidate
            break
        if tmp_fd is None or tmp_name is None:
            raise WorkspaceError("denied", f"cannot write {display}: temp file unavailable")
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(tmp_fd, mode)
            else:  # pragma: no cover - platform without fchmod
                os.chmod(tmp_name, mode, dir_fd=dir_fd)
            handle = os.fdopen(tmp_fd, "wb")
            tmp_fd = None  # fdopen owns the descriptor
            with handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.rename(tmp_name, leaf, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            tmp_name = None
        except OSError as error:
            raise WorkspaceError("denied", f"cannot write {display}: {error}") from error
        finally:
            if tmp_fd is not None:
                try:
                    os.close(tmp_fd)
                except OSError:
                    pass
            if tmp_name is not None:
                try:
                    os.unlink(tmp_name, dir_fd=dir_fd)
                except OSError:
                    pass
        try:
            os.fsync(dir_fd)
        except OSError:
            pass

    @staticmethod
    def _check_writable_target(st_before: Any, *, display: str) -> int:
        """Refuse symlink/irregular/read-only/hard-linked targets fail-closed."""
        if st_before is None:
            return 0o644  # new file; umask does not apply to openat here
        if stat.S_ISLNK(st_before.st_mode):
            raise WorkspaceError("denied", f"target is a symlink: {display}")
        if not stat.S_ISREG(st_before.st_mode):
            raise WorkspaceError("not-regular", f"target is not a regular file: {display}")
        if st_before.st_nlink > 1:
            raise WorkspaceError(
                "denied",
                f"target has multiple hard links; refusing to overwrite shared content: {display}",
            )
        if stat.S_IMODE(st_before.st_mode) & 0o222 == 0:
            raise WorkspaceError("denied", f"target is read-only: {display}")
        return stat.S_IMODE(st_before.st_mode)

    def _write_target_fallback(self, resolved: Path, data: bytes, *, display: str) -> None:
        """Pathname fallback for platforms without dir_fd support (Windows)."""
        try:
            rel_parent = resolved.parent.relative_to(self.root)
        except ValueError as error:
            raise WorkspaceError("denied", "path escapes the authorized workspace root") from error
        current = self.root
        for part in rel_parent.parts:
            if part in ("", "."):
                continue
            current = current / part
            try:
                st = os.lstat(current)
            except FileNotFoundError as error:
                raise WorkspaceError(
                    "missing", f"parent directory does not exist: {display}"
                ) from error
            except OSError as error:
                raise WorkspaceError("denied", f"parent is not accessible: {display}") from error
            if stat.S_ISLNK(st.st_mode):
                raise WorkspaceError("denied", f"path traverses a symlink: {display}")
            if not stat.S_ISDIR(st.st_mode):
                raise WorkspaceError("not-regular", f"path traverses a non-directory: {display}")
        try:
            st_before = os.lstat(resolved)
        except FileNotFoundError:
            st_before = None
        except OSError as error:
            raise WorkspaceError("denied", f"target is not accessible: {display}") from error
        mode = self._check_writable_target(st_before, display=display)

        tmp_fd: int | None = None
        tmp_path: str | None = None
        try:
            tmp_fd, tmp_path = tempfile.mkstemp(
                prefix=f".{resolved.name}.", suffix=".tmp", dir=str(resolved.parent)
            )
            if hasattr(os, "fchmod"):
                os.fchmod(tmp_fd, mode)
            else:  # pragma: no cover - platform without fchmod
                os.chmod(tmp_path, mode)
            handle = os.fdopen(tmp_fd, "wb")
            tmp_fd = None
            with handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, resolved)
            tmp_path = None
        except OSError as error:
            raise WorkspaceError("denied", f"cannot write {display}: {error}") from error
        finally:
            if tmp_fd is not None:
                try:
                    os.close(tmp_fd)
                except OSError:
                    pass
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        _fsync_dir_best_effort(resolved.parent)
