"""File loaders — load and parse documents for RAG ingestion."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ah.core.config import config
from ah.core.exceptions import ValidationError

logger = logging.getLogger(__name__)


@dataclass
class Document:
    """A loaded document with content and metadata."""

    content: str
    source: str
    metadata: dict[str, Any] = field(default_factory=dict)
    doc_type: str = "text"


class FileLoader:
    """Load files for RAG ingestion.

    Supports: .txt, .md, .py, .js, .ts, .json, .yaml, .yml, .csv, .html, .xml
    PDF support requires PyPDF2 (optional dependency).
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
            ".env",
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

    def __init__(self, base_dir: str | Path | None = None) -> None:
        self._base_dir = (
            Path(base_dir)
            if base_dir
            else Path(config.get("agent_harness_home") or os.getcwd()).resolve()
        )

    def load(self, path: str | Path) -> Document:
        """Load a file and return a Document.

        Raises ValueError if the file type is unsupported or the path is invalid.
        """
        file_path = self._resolve_path(path)

        if not file_path.exists():
            raise ValidationError(f"File not found: {path}")
        if not file_path.is_file():
            raise ValidationError(f"Not a file: {path}")

        ext = file_path.suffix.lower()
        if ext == ".pdf":
            return self._load_pdf(file_path)
        elif ext in self.TEXT_EXTENSIONS:
            return self._load_text(file_path)
        else:
            # Try to load as text anyway
            logger.warning("Unknown extension '%s', trying to load as text: %s", ext, path)
            return self._load_text(file_path)

    def load_directory(
        self,
        path: str | Path,
        pattern: str = "*",
        recursive: bool = True,
    ) -> list[Document]:
        """Load all matching files from a directory."""
        dir_path = self._resolve_path(path)

        if not dir_path.exists():
            raise ValidationError(f"Directory not found: {path}")
        if not dir_path.is_dir():
            raise ValidationError(f"Not a directory: {path}")

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

    def _load_text(self, file_path: Path) -> Document:
        """Load a text file."""
        ext = file_path.suffix.lower()
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            raise ValidationError(f"Failed to read {file_path}: {e}") from e

        metadata = {
            "filename": file_path.name,
            "extension": ext,
            "size_bytes": file_path.stat().st_size,
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

    def _load_pdf(self, file_path: Path) -> Document:
        """Load a PDF file using PyPDF2 (optional dependency)."""
        try:
            import PyPDF2
        except ImportError:
            raise ValidationError(
                "PDF support requires PyPDF2. Install with: pip install PyPDF2"
            ) from None

        try:
            with open(file_path, "rb") as f:
                reader = PyPDF2.PdfReader(f)
                text_parts = []
                for page in reader.pages:
                    text_parts.append(page.extract_text() or "")
                content = "\n\n".join(text_parts)
        except Exception as e:
            raise ValidationError(f"Failed to read PDF {file_path}: {e}") from e

        metadata = {
            "filename": file_path.name,
            "extension": ".pdf",
            "size_bytes": file_path.stat().st_size,
            "pages": len(reader.pages),
        }

        return Document(
            content=content,
            source=str(file_path),
            metadata=metadata,
            doc_type="pdf",
        )

    def _resolve_path(self, path: str | Path) -> Path:
        """Resolve path relative to base directory, ensuring it stays inside."""
        candidate = (self._base_dir / str(path)).resolve()
        if not candidate.is_relative_to(self._base_dir):
            raise ValidationError(
                f"Path '{path}' escapes the allowed base directory '{self._base_dir}'"
            )
        return candidate
