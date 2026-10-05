"""File tools — read_file, write_file, list_files."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from ah.core.config import config
from ah.core.exceptions import ToolError
from ah.tools.base import registry

logger = logging.getLogger(__name__)

# Base directory for all file operations. Defaults to the current working
# directory; override via the agent_harness_home config key.
_BASE_DIR = Path(config.get("agent_harness_home") or os.getcwd()).resolve()
_PRIVATE_PARTS = {".git", ".aws", ".ssh", ".agent-harness", "__pycache__"}
_PRIVATE_FILES = {".env", ".env.local", ".npmrc", ".pypirc", "id_rsa", "id_ed25519"}


def resolve_path(path: str) -> Path:
    """Resolve *path* relative to the base directory and verify it stays inside.

    Returns the resolved :class:`~pathlib.Path` on success.  Raises
    ``ValueError`` if the path escapes the base directory.
    """
    # Resolve the candidate path (handles .., symlinks, etc.)
    candidate = (_BASE_DIR / path).resolve()

    # Ensure the resolved path is within the allowed root
    if not candidate.is_relative_to(_BASE_DIR):
        raise ValueError(f"Path '{path}' escapes the allowed base directory '{_BASE_DIR}'")

    parts = candidate.relative_to(_BASE_DIR).parts
    if any(part.lower() in _PRIVATE_PARTS for part in parts) or any(
        part.lower() in _PRIVATE_FILES or part.lower().startswith(".env.")
        for part in parts
        if part.lower() != ".env.example"
    ):
        raise ValueError(f"Path '{path}' is private")

    # Re-stat with O_NOFOLLOW semantics where possible to catch TOCTOU swaps.
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow and candidate.exists() and not candidate.is_dir():
        try:
            fd = os.open(candidate, os.O_RDONLY | nofollow)
        except OSError as e:
            raise ValueError(f"Path '{path}' is not accessible (symlink?)") from e
        try:
            # Verify the opened file still resolves inside the base dir.
            try:
                real = Path(f"/proc/self/fd/{fd}").resolve()
                if not real.is_relative_to(_BASE_DIR):
                    raise ValueError(f"Path '{path}' escapes the allowed base directory")
            except OSError:
                pass
        finally:
            os.close(fd)

    return candidate


@registry.register(
    name="read_file",
    description="Read the contents of a file. Returns the file content as text.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file to read"},
            "offset": {
                "type": "integer",
                "description": "Line number to start reading from (1-indexed)",
            },
            "limit": {"type": "integer", "description": "Maximum number of lines to read"},
            "max_size": {
                "type": "integer",
                "description": "Maximum file size in bytes (default: 1048576 = 1 MB)",
            },
        },
        "required": ["path"],
    },
)
async def read_file(
    path: str, offset: int = 1, limit: int = 2000, max_size: int = 1_048_576
) -> str:
    """Read a file with optional offset and limit.

    Args:
        path: Path to the file to read.
        offset: Line number to start reading from (1-indexed).
        limit: Maximum number of lines to read.
        max_size: Maximum file size in bytes. Files larger than this are rejected.
    """
    if offset < 1:
        raise ToolError(f"offset must be >= 1, got {offset}")
    if limit < 1:
        raise ToolError(f"limit must be >= 1, got {limit}")
    if max_size < 1:
        raise ToolError(f"max_size must be >= 1, got {max_size}")
    effective_max = min(max_size, 5_242_880)
    try:
        file_path = resolve_path(path)
    except ValueError as e:
        raise ToolError(f"{e}") from e

    if not file_path.exists():
        raise ToolError(f"File not found: {path}")
    if not file_path.is_file():
        raise ToolError(f"Not a file: {path}")

    # Check file size before reading
    file_size = file_path.stat().st_size
    if file_size > effective_max:
        raise ToolError(f"File '{path}' is too large ({file_size} bytes, max {effective_max} bytes)")

    try:

        def _read():
            with open(file_path, encoding="utf-8", errors="replace") as f:
                data = f.read(effective_max + 1)
            if len(data.encode("utf-8", errors="replace")) > effective_max:
                # Re-check by bytes to avoid over-read on multi-byte chars.
                raw = data.encode("utf-8", errors="replace")[: effective_max + 1]
                if len(raw) > effective_max:
                    raise ValueError(f"File '{path}' exceeds max size during read")
            lines = data.splitlines(keepends=True)
            end = offset + limit - 1
            return "".join(lines[offset - 1 : end])

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
        file_path = resolve_path(path)
    except ValueError as e:
        raise ToolError(f"{e}") from e

    try:

        def _write():
            file_path.parent.mkdir(parents=True, exist_ok=True)
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)
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
        dir_path = resolve_path(path)
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
                    resolve_path(str(f))
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
