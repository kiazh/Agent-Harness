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
async def read_file(path: str, offset: int = 1, limit: int = 2000, max_size: int = 1_048_576) -> str:
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
    if file_size > max_size:
        raise ToolError(
            f"File '{path}' is too large ({file_size} bytes, max {max_size} bytes)"
        )

    try:

        def _read():
            with open(file_path, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            end = offset + limit - 1
            return "".join(lines[offset - 1 : end])

        return await asyncio.to_thread(_read)
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
            files = list(dir_path.glob(pattern))
            if not files:
                return f"No files found matching '{pattern}' in {path}"
            lines = []
            for f in sorted(files):
                if f.is_file():
                    size = f.stat().st_size
                    lines.append(f"{f.relative_to(dir_path)} ({size} bytes)")
                elif f.is_dir():
                    lines.append(f"{f.relative_to(dir_path)}/ (dir)")
            return "\n".join(lines)

        return await asyncio.to_thread(_list)
    except Exception as e:
        raise ToolError(f"Error listing files: {e}") from e
