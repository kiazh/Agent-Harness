"""File tools — read_file, write_file, list_files."""

from __future__ import annotations

import asyncio
import logging
import os
import stat
import tempfile
from pathlib import Path

from machina.core.config import config
from machina.core.exceptions import ToolError
from machina.tools.base import registry

logger = logging.getLogger(__name__)

# Base directory for all file operations. Defaults to the current working
# directory; override via the machina_home config key.
_BASE_DIR = Path(config.get("machina_home") or os.getcwd()).resolve()
_IMPORT_BASE_DIR = _BASE_DIR
_PRIVATE_PARTS = {".git", ".aws", ".ssh", ".machina", "__pycache__"}
_PRIVATE_FILES = {".env", ".env.local", ".npmrc", ".pypirc", "id_rsa", "id_ed25519"}


def _base_dir() -> Path:
    from machina.core.execution_context import execution_context

    override = Path(_BASE_DIR) if Path(_BASE_DIR) != _IMPORT_BASE_DIR else None
    return execution_context(workspace_override=override).workspace


def _realpath_inside_base(path: Path) -> Path:
    """Return realpath of *path* or raise if it escapes the base."""
    base = _base_dir()
    try:
        real = Path(os.path.realpath(path)).resolve()
    except OSError:
        real = path.resolve()
    if not real.is_relative_to(base):
        raise ValueError(f"Path '{path}' escapes the allowed base directory")
    return real


def _reject_symlink_components(candidate: Path) -> None:
    """Reject paths whose ancestor chain contains a symlink escaping the base.

    Handles ancestor symlinks, not only the final component (AH-003). Each
    component from the base to the candidate is lstat-checked; if any is a
    symlink, the fully-resolved realpath must still stay inside the base.
    """
    base = _base_dir()
    try:
        rel = candidate.relative_to(base)
    except ValueError:
        raise ValueError(f"Path '{candidate}' escapes the allowed base directory") from None
    cur = base
    for part in rel.parts:
        cur = cur / part
        try:
            if cur.is_symlink():
                # Resolve the full candidate (follows the ancestor link) and
                # require it to stay inside the base.
                _realpath_inside_base(candidate)
                return
        except OSError:
            # Unreadable component: fail closed only if it exists as symlink?
            # lstat failure on missing leaf is fine (new file creation).
            continue


def _verify_fd_matches_path(fd: int, candidate: Path, *, check_base: bool = True) -> None:
    """Verify an opened descriptor still refers to *candidate* (TOCTOU guard).

    Uses fstat dev/ino comparison on POSIX (no /proc needed, so macOS and
    Linux without procfs work — AH-004). Falls back to realpath containment
    when stat info is unavailable (e.g. Windows).
    """
    try:
        st_fd = os.fstat(fd)
        st_path = os.lstat(candidate)
        # If the path is a symlink, O_NOFOLLOW open would already have
        # failed; a mismatch here means the file was swapped.
        import stat as _stat

        if _stat.S_ISLNK(st_path.st_mode):
            raise ValueError(f"Path '{candidate}' is a symlink")
        if (st_fd.st_ino, st_fd.st_dev) != (st_path.st_ino, st_path.st_dev):
            raise ValueError(f"Path '{candidate}' changed during access (TOCTOU)")
    except ValueError:
        raise
    except OSError:
        pass
    if not check_base:
        return
    # Best-effort realpath containment (covers ancestor swaps that keep ino
    # comparison blind on platforms without meaningful ino, e.g. Windows).
    try:
        _realpath_inside_base(candidate)
    except ValueError:
        raise


def _full_mode() -> bool:
    from machina.core.execution_context import execution_context

    return execution_context().mode == "full"


def resolve_path(path: str, *, allow_outside: bool = False) -> Path:
    """Resolve *path* relative to the base directory and verify it stays inside.

    In full mode (explicit session grant) with ``allow_outside=True``, absolute
    host paths outside the workspace are permitted — private files
    (.ssh/.aws/.env/credentials) stay blocked everywhere. Returns the
    resolved :class:`~pathlib.Path` on success.
    """
    base = _base_dir()
    raw = Path(path)
    if allow_outside and _full_mode() and raw.is_absolute():
        candidate = raw.resolve()
    else:
        # Resolve the candidate path (handles .., symlinks, etc.)
        candidate = (base / path).resolve()

        # Ensure the resolved path is within the allowed root
        if not candidate.is_relative_to(base):
            raise ValueError(f"Path '{path}' escapes the allowed base directory '{base}'")

    if not allow_outside or not _full_mode():
        parts = candidate.relative_to(base).parts
    else:
        try:
            parts = candidate.relative_to(base).parts
        except ValueError:
            parts = candidate.parts
    if any(part.lower() in _PRIVATE_PARTS for part in parts) or any(
        part.lower() in _PRIVATE_FILES or part.lower().startswith(".env.")
        for part in parts
        if part.lower() != ".env.example"
    ):
        raise ValueError(f"Path '{path}' is private")

    outside = not candidate.is_relative_to(base)
    if not outside:
        # Ancestor-symlink check (no /proc dependency — AH-004).
        _reject_symlink_components(candidate)

    # Re-stat with O_NOFOLLOW semantics where possible to catch TOCTOU swaps.
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow and candidate.exists() and not candidate.is_dir():
        try:
            fd = os.open(candidate, os.O_RDONLY | nofollow)
        except OSError as e:
            raise ValueError(f"Path '{path}' is not accessible (symlink?)") from e
        try:
            # Cross-platform verification: fstat dev/ino match (+ realpath
            # containment for workspace paths; full-mode outside paths skip
            # containment since the broker already authorized them).
            try:
                _verify_fd_matches_path(fd, candidate, check_base=not outside)
            except ValueError as ve:
                raise ValueError(str(ve).replace(str(candidate), path)) from ve
        finally:
            os.close(fd)

    return candidate


def _open_secure_read(candidate: Path, *, check_base: bool = True):
    """Open *candidate* for reading, returning (fd, real_verified_path).

    Uses O_NOFOLLOW where available and verifies the descriptor matches the
    path to close the check-then-open race (AH-003). Callers must os.close
    the fd (or fdopen it, which takes ownership).
    """
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    flags = os.O_RDONLY
    if nofollow:
        flags |= nofollow
    try:
        fd = _os_open(candidate, flags)
    except OSError as e:
        raise ValueError(f"Path '{candidate}' is not accessible (symlink?)") from e
    try:
        _verify_fd_matches_path(fd, candidate, check_base=check_base)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    return fd


# Indirection for deterministic race tests (swap hooks monkeypatch these,
# never real user files).
_os_open = os.open


def _secure_mkdir_parents(file_path: Path, *, inside_base: bool) -> None:
    """Create missing parents one level at a time without following symlinks.

    os.mkdir fails on symlink-to-dir with FileExistsError; every level is
    lstat-verified afterwards. Raises ValueError on any symlink component.
    """
    root = _base_dir() if inside_base else Path(file_path.anchor)
    try:
        rel = file_path.parent.relative_to(root)
    except ValueError:
        rel = None
    cur = root
    parts = list(rel.parts) if rel is not None else []
    for part in parts:
        cur = cur / part
        try:
            st = os.lstat(cur)
            import stat as _stat

            if _stat.S_ISLNK(st.st_mode):
                raise ValueError(f"Path '{file_path}' traverses a symlink")
            if not _stat.S_ISDIR(st.st_mode):
                raise ValueError(f"Path '{file_path}' traverses a non-directory")
        except FileNotFoundError:
            try:
                os.mkdir(cur)
            except FileExistsError:
                # Raced creation: re-verify rather than assuming success.
                st2 = os.lstat(cur)
                import stat as _stat2

                if _stat2.S_ISLNK(st2.st_mode):
                    raise ValueError(f"Path '{file_path}' traverses a symlink") from None
            except OSError as e:
                raise ValueError(f"Cannot create directory '{cur}': {e}") from e
        except OSError as e:
            raise ValueError(f"Cannot access '{cur}': {e}") from e


def _fsync_dir_best_effort(directory: Path) -> None:
    """Persist a rename on POSIX; unavailable directory fsync is not fatal."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        dir_fd = os.open(directory, flags)
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


def _atomic_write_file(file_path: Path, content: str, *, mode: int | None = None) -> None:
    """Write *content* to *file_path* through temp + fsync + replace.

    A mid-write failure (ENOSPC, crash, cancellation) cannot destroy the
    original: the complete content is written and fsynced on a random-name
    temp file in the same directory (mkstemp, O_EXCL) and only renamed over
    the target once it is whole. The caller's mode bits are preserved.
    """
    directory = file_path.parent
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{file_path.name}.", suffix=".tmp", dir=str(directory)
    )
    try:
        if mode is not None:
            os.fchmod(fd, mode)
        handle = os.fdopen(fd, "w", encoding="utf-8")
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    try:
        with handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, file_path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    _fsync_dir_best_effort(directory)


# read_file paging bounds: the byte gate applies to the selected lines (not
# the whole file) so large files can be read a page at a time. Lines are
# consumed in bounded pieces so a pathological single-line file is never
# buffered whole.
_READ_LINE_CHUNK = 1 << 16
_READ_MAX_PAGE_BYTES = 5_242_880


def _read_page(
    stream, *, offset: int, limit: int, max_bytes: int, path: str
) -> tuple[str, int, int]:
    """Apply offset/limit to *stream* under per-line and per-page byte caps.

    Returns ``(page_text, shown_lines, total_lines)``. Lines outside the
    requested window are consumed piece by piece and discarded; a window
    whose encoded size exceeds *max_bytes* is rejected (fail closed) instead
    of returned.
    """
    page: list[str] = []
    page_bytes = 0
    total_lines = 0
    while True:
        piece = stream.readline(_READ_LINE_CHUNK)
        if piece == "":
            break
        total_lines += 1
        parts: list[str] | None = [piece] if offset <= total_lines < offset + limit else None
        size = len(piece)
        while not piece.endswith("\n"):
            piece = stream.readline(_READ_LINE_CHUNK)
            if piece == "":
                break
            size += len(piece)
            if parts is None:
                continue
            if size > max_bytes:
                raise ValueError(f"File '{path}' exceeds max size during read")
            parts.append(piece)
        if parts is None:
            continue
        text = "".join(parts)
        page_bytes += len(text.encode("utf-8", errors="replace"))
        if page_bytes > max_bytes:
            raise ValueError(f"File '{path}' exceeds max size during read")
        page.append(text)
    return "".join(page), len(page), total_lines


@registry.register(
    name="read_file",
    description=(
        "Read the contents of a file. Returns the file content as text. Large files "
        "can be paged with offset/limit; a result truncated at limit ends with a "
        "marker reporting the total line count and the next offset."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file to read"},
            "offset": {
                "type": "integer",
                "minimum": 1,
                "description": "Line number to start reading from (1-indexed, default 1)",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "description": (
                    "Maximum number of lines to return (default 2000); when more "
                    "lines follow, the result ends with a truncation marker"
                ),
            },
            "max_size": {
                "type": "integer",
                "minimum": 1,
                "description": (
                    "Maximum bytes for the returned page (default: 1048576 = 1 MB, "
                    "hard cap 5242880 = 5 MB); applies to the selected lines, so "
                    "larger files can still be paged"
                ),
            },
        },
        "required": ["path"],
    },
)
async def read_file(
    path: str, offset: int = 1, limit: int = 2000, max_size: int = 1_048_576
) -> str:
    """Read a file with optional offset and limit.

    Offset/limit are applied before the size gate: only the selected page
    counts against ``max_size``, so an oversized file can be paged instead of
    rejected. When the page is cut off at ``limit`` and more lines follow, a
    final marker reports the total line count and the next offset.

    Args:
        path: Path to the file to read.
        offset: Line number to start reading from (1-indexed).
        limit: Maximum number of lines to read.
        max_size: Maximum number of bytes in the returned page.
    """
    if offset < 1:
        raise ToolError(f"offset must be >= 1, got {offset}")
    if limit < 1:
        raise ToolError(f"limit must be >= 1, got {limit}")
    if max_size < 1:
        raise ToolError(f"max_size must be >= 1, got {max_size}")
    effective_max = min(max_size, _READ_MAX_PAGE_BYTES)
    try:
        # Full mode: broker-authorized absolute host paths permitted.
        file_path = resolve_path(path, allow_outside=True)
    except ValueError as e:
        raise ToolError(f"{e}") from e

    if not file_path.exists():
        raise ToolError(f"File not found: {path}")
    if not file_path.is_file():
        raise ToolError(f"Not a file: {path}")

    try:

        def _read() -> str:
            # Secure open via descriptor (AH-003): do not reopen the
            # pathname after validation — read from the verified fd.
            outside = not file_path.is_relative_to(_base_dir())
            fd = _open_secure_read(file_path, check_base=not outside)
            try:
                with os.fdopen(fd, encoding="utf-8", errors="replace") as f:
                    page, shown, total = _read_page(
                        f, offset=offset, limit=limit, max_bytes=effective_max, path=path
                    )
            except BaseException:
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise
            if not page:
                return ""
            if total > offset + shown - 1:
                next_offset = offset + shown
                return page + (
                    f"\n[read_file: showing lines {offset}-{next_offset - 1} of "
                    f"{total}; use offset={next_offset} to continue]"
                )
            return page

        return await asyncio.to_thread(_read)
    except ToolError:
        raise
    except Exception as e:
        raise ToolError(f"Error reading file: {e}") from e


@registry.register(
    name="write_file",
    description="Write content to a file. Creates the file if it doesn't exist.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file to write"},
            "content": {"type": "string", "description": "Content to write to the file"},
        },
        "required": ["path", "content"],
    },
)
async def write_file(path: str, content: str) -> str:
    """Write content to a file."""
    try:
        file_path = resolve_path(path, allow_outside=True)
    except ValueError as e:
        raise ToolError(f"{e}") from e

    try:

        def _write():
            inside = file_path.is_relative_to(_base_dir())
            if inside:
                # Verify ancestors (symlink-aware) before creating parents.
                _reject_symlink_components(file_path)
            # Secure parent creation: level-by-level, never through symlinks.
            _secure_mkdir_parents(file_path, inside_base=inside)
            if inside:
                # Re-check after mkdir (a component could have been swapped).
                _reject_symlink_components(file_path)
            nofollow = getattr(os, "O_NOFOLLOW", 0)
            # Open first WITHOUT O_TRUNC: the descriptor is validated before
            # any content changes, and the content itself is written to a
            # same-directory temp file that only replaces the target once it
            # is complete and fsynced (5.2 — rejected actions have no effects;
            # ENOSPC mid-write cannot destroy the original).
            flags = os.O_WRONLY | os.O_CREAT
            if nofollow:
                # If the target exists as a symlink, fail closed instead of
                # following the link (AH-003 truncating-write race).
                flags |= nofollow
            # Existence probe BEFORE O_CREAT: if validation below rejects the
            # open, a newly created empty file is removed so rejection has no
            # residual effects (best effort; content is never written first).
            try:
                os.lstat(file_path)
                existed_before = True
            except OSError:
                existed_before = False
            try:
                fd = _os_open(file_path, flags, 0o600)
            except OSError as e:
                # ELOOP = symlink with O_NOFOLLOW; EEXIST races, etc.
                raise ValueError(f"Path '{path}' is not accessible (symlink?)") from e
            try:
                _verify_fd_matches_path(fd, file_path, check_base=inside)
                # Hard-link escape: an inside name with nlink > 1 may point at
                # outside content via another name; refuse agent writes.
                mode = None
                try:
                    target_info = os.fstat(fd)
                    if target_info.st_nlink > 1:
                        raise ValueError(f"Path '{path}' has multiple hard links; refusing write")
                    if existed_before:
                        mode = stat.S_IMODE(target_info.st_mode)
                except ValueError:
                    raise
                except OSError:
                    pass
                # Close the probe descriptor before the rename: Windows cannot
                # replace a file that is still open.
                try:
                    os.close(fd)
                except OSError:
                    pass
                _atomic_write_file(file_path, content, mode=mode)
            except BaseException:
                try:
                    os.close(fd)
                except OSError:
                    pass
                if not existed_before:
                    # Best-effort removal of an empty file O_CREAT may have
                    # created at a rejected target; never touches content.
                    try:
                        if os.lstat(file_path).st_size == 0:
                            os.unlink(file_path)
                    except OSError:
                        pass
                raise
            return f"Successfully wrote {len(content)} characters to {path}"

        return await asyncio.to_thread(_write)
    except Exception as e:
        raise ToolError(f"Error writing file: {e}") from e


@registry.register(
    name="list_files",
    description="List files in a directory. Returns filenames with sizes.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory path to list"},
            "pattern": {
                "type": "string",
                "description": "Glob pattern to filter files (e.g., '*.py')",
            },
        },
        "required": ["path"],
    },
)
async def list_files(path: str = ".", pattern: str = "*") -> str:
    """List files in a directory."""
    # Sanitize glob pattern: reject traversal, absolute, drive/namespace, backslash.
    if (
        ".." in pattern
        or ":" in pattern
        or "\\" in pattern
        or Path(pattern).is_absolute()
        or pattern.startswith(("/", "\\"))
    ):
        raise ToolError(f"Invalid glob pattern: {pattern}")
    try:
        dir_path = resolve_path(path, allow_outside=True)
    except ValueError as e:
        raise ToolError(f"{e}") from e

    if not dir_path.exists():
        raise ToolError(f"Directory not found: {path}")
    if not dir_path.is_dir():
        raise ToolError(f"Not a directory: {path}")
    try:

        def _list():
            files = list(dir_path.glob(pattern))[:200]
            if not files:
                return f"No files found matching '{pattern}' in {path}"
            lines = []
            for f in sorted(files)[:200]:
                try:
                    resolve_path(str(f), allow_outside=True)
                except ValueError:
                    continue
                # Skip symlink dirs (and any symlink) to avoid escape.
                try:
                    if f.is_symlink():
                        continue
                except OSError:
                    continue
                if f.is_file():
                    size = f.stat().st_size
                    lines.append(f"{f.relative_to(dir_path)} ({size} bytes)")
                elif f.is_dir():
                    lines.append(f"{f.relative_to(dir_path)}/ (dir)")
            return "\n".join(lines)

        return await asyncio.to_thread(_list)
    except ToolError:
        raise
    except Exception as e:
        raise ToolError(f"Error listing files: {e}") from e


# Declared side-effect capabilities for the permission broker (addendum:
# extensible tools need explicit effect declarations; trusted in-process
# plugins run inside the host process and are NOT a sandbox).
registry.declare_effects(
    {
        "read_file": ("fs.read",),
        "write_file": ("fs.write",),
        "list_files": ("fs.read",),
    }
)
