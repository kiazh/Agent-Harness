"""File loaders — load and parse documents for RAG ingestion."""

from __future__ import annotations

import importlib as _importlib
import io
import logging
import os
import stat
import sys as _sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# SEAM: leaf-core seam for machina.core (config, exceptions ValidationError).
# Deferred via sys.modules/importlib so this module imports with machina.core
# blocked; resolves to the same objects when core is present. No logic change.
def _core_symbol(module: str, name: str):
    mod = _sys.modules.get(module)
    if mod is None:
        mod = _importlib.import_module(module)
    return getattr(mod, name)


logger = logging.getLogger(__name__)

#: Default admission bound for one indexed document (8 MiB). Enforced from
#: ``fstat`` before the read and again while reading the descriptor, so an
#: oversized file is refused before its body is allocated.
MAX_DOCUMENT_BYTES = 8 * 1024 * 1024
#: Default page budget for PDF parsing (extraction cost is page-proportional).
MAX_PDF_PAGES = 200
#: Default bound on the extracted UTF-8 text of one PDF.
MAX_PDF_TEXT_BYTES = 8 * 1024 * 1024

# Indirection for deterministic race tests (mirrors machina/workspace/files.py
# and machina/tools/file.py): tests monkeypatch these seams, never real files.
_os_open = os.open
_os_read = os.read


@dataclass
class Document:
    """A loaded document with content and metadata."""

    content: str
    source: str
    metadata: dict[str, Any] = field(default_factory=dict)
    doc_type: str = "text"


def _default_base_dir() -> Path:
    """The one canonical workspace root (shared with the permission broker).

    Resolved per call, never cached: inside the broker's guard+execute window
    ``execution_context`` returns the request-bound workspace snapshot, so a
    relative path resolves to the same file the broker approved; outside a
    tool call it falls back to the configured workspace root. Bound through
    the ``_core_symbol`` seam so this module still imports with
    ``machina.core`` blocked.
    """
    context = _core_symbol("machina.core.execution_context", "execution_context")()
    return Path(context.workspace).resolve()


def _validation_error(message: str) -> Exception:
    return _core_symbol("machina.core.exceptions", "ValidationError")(message)


def _nofollow() -> int:
    return getattr(os, "O_NOFOLLOW", 0)


def _nonblock() -> int:
    return getattr(os, "O_NONBLOCK", 0)


def _dir_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _nofollow()


def _dir_fd_supported() -> bool:
    """Whether this platform supports dir_fd-anchored file operations."""
    try:
        supported = os.supports_dir_fd
    except AttributeError:
        return False
    return all(function in supported for function in (os.open, os.stat))


def _close_quietly(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


class FileLoader:
    """Load files for RAG ingestion.

    Supports: .txt, .md, .py, .js, .ts, .json, .yaml, .yml, .csv, .html, .xml
    PDF support requires PyPDF2 (optional dependency).

    Containment is descriptor-anchored on platforms with ``dir_fd`` support
    (macOS/Linux): the workspace root is opened once, every ancestor is
    opened with ``O_NOFOLLOW|O_DIRECTORY`` relative to its parent descriptor,
    and the leaf is opened ``O_NOFOLLOW`` from that descriptor. The bytes are
    read from the verified descriptor, never by re-opening the checked
    pathname, so a concurrent final/ancestor swap cannot redirect ingestion
    outside the authorized root (MCH-007). Platforms without ``dir_fd``
    (notably Windows) use a documented weaker pathname fallback: the same
    ``O_NOFOLLOW`` descriptor checks apply, but the open is not
    descriptor-anchored across ancestors.

    Reads are bounded by ``max_document_bytes`` before and during the read;
    PDF parsing also enforces page and extracted-text budgets (MCH-008).
    """

    # Supported text extensions
    TEXT_EXTENSIONS = frozenset(
        {
            ".txt",
            ".md",
            ".markdown",
            ".rst",
            ".py",
            ".js",
            ".ts",
            ".jsx",
            ".tsx",
            ".json",
            ".yaml",
            ".yml",
            ".toml",
            ".ini",
            ".cfg",
            ".csv",
            ".tsv",
            ".html",
            ".htm",
            ".xml",
            ".svg",
            ".css",
            ".scss",
            ".less",
            ".sh",
            ".bash",
            ".zsh",
            ".sql",
            ".r",
            ".rb",
            ".go",
            ".rs",
            ".java",
            ".c",
            ".cpp",
            ".h",
            ".dockerfile",
            ".makefile",
            ".cmake",
            ".log",
        }
    )

    # Code extensions for structure-aware chunking
    CODE_EXTENSIONS = frozenset(
        {
            ".py",
            ".js",
            ".ts",
            ".jsx",
            ".tsx",
            ".java",
            ".go",
            ".rs",
            ".c",
            ".cpp",
            ".h",
            ".rb",
            ".r",
        }
    )

    def __init__(
        self,
        base_dir: str | Path | None = None,
        *,
        max_document_bytes: int = MAX_DOCUMENT_BYTES,
        max_pdf_pages: int = MAX_PDF_PAGES,
        max_pdf_text_bytes: int = MAX_PDF_TEXT_BYTES,
    ) -> None:
        # An explicit base is anchored at construction. Without one, the base
        # is resolved per load through ``_default_base_dir`` (the canonical
        # execution-context workspace), so a long-lived shared loader tracks
        # the workspace the permission broker approved against instead of
        # freezing ``machina_home``/cwd (R4-026).
        self._base_dir: Path | None = Path(base_dir).resolve() if base_dir else None
        if max_document_bytes < 1:
            raise ValueError("max_document_bytes must be positive")
        self.max_document_bytes = int(max_document_bytes)
        self.max_pdf_pages = int(max_pdf_pages)
        self.max_pdf_text_bytes = int(max_pdf_text_bytes)

    def load(self, path: str | Path) -> Document:
        """Load a file and return a Document.

        Raises ValueError if the file type is unsupported or the path is invalid.
        """
        base = self._base_dir if self._base_dir is not None else _default_base_dir()
        file_path = self._resolve_path(path, base=base)

        ext = file_path.suffix.lower()
        fd = self._open_contained(base, file_path)
        try:
            try:
                st = os.fstat(fd)
            except OSError as e:
                raise _validation_error(f"Failed to read {path}: {e}") from e
            if not stat.S_ISREG(st.st_mode):
                raise _validation_error(f"Not a file: {path}")
            if ext != ".pdf" and ext not in self.TEXT_EXTENSIONS:
                raise _validation_error(f"Unsupported document extension: {ext or '(none)'}")
            if st.st_size > self.max_document_bytes:
                raise _validation_error(
                    f"File exceeds the {self.max_document_bytes}-byte document limit: {path}"
                )
            data = self._read_bounded(fd, path)
            if ext == ".pdf":
                return self._load_pdf(file_path, data)
            return self._load_text(file_path, data)
        finally:
            _close_quietly(fd)

    def load_directory(
        self,
        path: str | Path,
        pattern: str = "*",
        recursive: bool = True,
    ) -> list[Document]:
        """Load all matching files from a directory."""
        dir_path = self._resolve_path(path)

        if not dir_path.exists():
            raise _validation_error(f"Directory not found: {path}")
        if not dir_path.is_dir():
            raise _validation_error(f"Not a directory: {path}")

        documents = []
        glob_pattern = f"**/{pattern}" if recursive else pattern

        for file_path in sorted(dir_path.glob(glob_pattern)):
            if not file_path.is_file():
                continue
            ext = file_path.suffix.lower()
            if ext in self.TEXT_EXTENSIONS or ext == ".pdf":
                try:
                    doc = self.load(file_path)
                    documents.append(doc)
                except Exception as e:
                    logger.warning("Failed to load %s: %s", file_path, e)

        return documents

    def _read_bounded(self, fd: int, display: str | Path) -> bytes:
        """Read *fd* up to the admission bound, refusing a body that grew."""
        limit = self.max_document_bytes
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining > 0:
            try:
                chunk = _os_read(fd, min(1 << 16, remaining))
            except OSError as e:
                raise _validation_error(f"Failed to read {display}: {e}") from e
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > limit:
            raise _validation_error(f"File exceeds the {limit}-byte document limit: {display}")
        return data

    def _load_text(self, file_path: Path, data: bytes) -> Document:
        """Build a text document from the descriptor-verified bytes."""
        ext = file_path.suffix.lower()
        content = data.decode("utf-8", errors="replace")

        metadata = {
            "filename": file_path.name,
            "extension": ext,
            "size_bytes": len(data),
        }

        doc_type = "code" if ext in self.CODE_EXTENSIONS else "text"
        if ext in (".md", ".markdown"):
            doc_type = "markdown"

        return Document(
            content=content,
            source=str(file_path),
            metadata=metadata,
            doc_type=doc_type,
        )

    def _load_pdf(self, file_path: Path, data: bytes) -> Document:
        """Parse PDF bytes captured from the descriptor (PDF is never reopened)."""
        try:
            import PyPDF2
        except ImportError:
            raise _validation_error(
                "PDF support requires PyPDF2. Install with: pip install PyPDF2"
            ) from None

        try:
            reader = PyPDF2.PdfReader(io.BytesIO(data))
            pages = list(reader.pages)
        except Exception as e:
            raise _validation_error(f"Failed to read PDF {file_path}: {e}") from e

        page_count = len(pages)
        if page_count > self.max_pdf_pages:
            raise _validation_error(
                f"PDF has {page_count} pages; the limit is {self.max_pdf_pages}: {file_path}"
            )
        text_parts: list[str] = []
        extracted_bytes = 0
        try:
            for page in pages:
                text = page.extract_text() or ""
                extracted_bytes += len(text.encode("utf-8"))
                if extracted_bytes > self.max_pdf_text_bytes:
                    raise _validation_error(
                        "PDF extracted text exceeds the "
                        f"{self.max_pdf_text_bytes}-byte text budget: {file_path}"
                    )
                text_parts.append(text)
            content = "\n\n".join(text_parts)
        except Exception as e:
            if type(e).__name__ == "ValidationError":
                raise
            raise _validation_error(f"Failed to read PDF {file_path}: {e}") from e

        metadata = {
            "filename": file_path.name,
            "extension": ".pdf",
            "size_bytes": len(data),
            "pages": page_count,
        }

        return Document(
            content=content,
            source=str(file_path),
            metadata=metadata,
            doc_type="pdf",
        )

    # ─── descriptor-anchored containment ────────────────────────────────────
    def _open_contained(self, base: Path, resolved: Path) -> int:
        """Open *resolved* without ever re-opening the checked pathname.

        The path has already passed ``_resolve_path`` (containment and private
        names) and is fully resolved. On ``dir_fd`` platforms the walk starts
        at *base* and uses ``O_NOFOLLOW`` for every component, so a component
        swapped for a symlink after validation fails closed (ELOOP/ENOTDIR).
        """
        if _dir_fd_supported():
            try:
                rel = resolved.relative_to(base)
            except ValueError as e:
                raise _validation_error("Path escapes the allowed base directory") from e
            return self._walk_open(base, rel)

        # Documented weaker fallback (e.g. Windows): same O_NOFOLLOW checks,
        # but the open is not anchored to validated ancestor descriptors.
        try:
            return _os_open(resolved, os.O_RDONLY | _nofollow() | _nonblock())
        except FileNotFoundError as e:
            raise _validation_error(f"File not found: {resolved}") from e
        except OSError as e:
            raise _validation_error(f"Path is not accessible: {resolved}") from e

    def _walk_open(self, base: Path, rel: Path) -> int:
        parts = [part for part in rel.parts if part not in ("", ".")]
        if not parts:
            raise _validation_error("Not a file: workspace root is a directory")
        try:
            dir_fd = _os_open(base, _dir_flags())
        except OSError as e:
            raise _validation_error(f"Base directory is not accessible: {base}") from e
        try:
            for part in parts[:-1]:
                try:
                    next_fd = _os_open(part, _dir_flags(), dir_fd=dir_fd)
                except FileNotFoundError as e:
                    raise _validation_error(f"File not found: {rel}") from e
                except OSError as e:
                    raise _validation_error(
                        f"Path traverses a symlink or non-directory: {rel}"
                    ) from e
                _close_quietly(dir_fd)
                dir_fd = next_fd
            try:
                return _os_open(parts[-1], os.O_RDONLY | _nofollow() | _nonblock(), dir_fd=dir_fd)
            except FileNotFoundError as e:
                raise _validation_error(f"File not found: {rel}") from e
            except OSError as e:
                raise _validation_error(f"Path is not accessible (symlink?): {rel}") from e
        finally:
            if dir_fd >= 0:
                _close_quietly(dir_fd)

    def _resolve_path(self, path: str | Path, base: Path | None = None) -> Path:
        """Resolve path relative to base directory, ensuring it stays inside."""
        base = (
            base
            if base is not None
            else (self._base_dir if self._base_dir is not None else _default_base_dir())
        )
        candidate = (base / str(path)).resolve()
        if not candidate.is_relative_to(base):
            raise _validation_error(f"Path '{path}' escapes the allowed base directory '{base}'")
        parts = (part.lower() for part in candidate.relative_to(base).parts)
        private_dirs = {".git", ".aws", ".ssh", ".machina", "__pycache__"}
        private_files = {".env", ".env.local", ".npmrc", ".pypirc", "id_rsa", "id_ed25519"}
        if any(
            part in private_dirs
            or part in private_files
            or (part.startswith(".env.") and part != ".env.example")
            for part in parts
        ):
            raise _validation_error(f"Path '{path}' is private")
        return candidate
