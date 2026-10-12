"""Chunking strategies — recursive character splitting with structure awareness."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Chunk:
    """A text chunk with metadata."""

    text: str
    index: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    token_count: int = 0


class RecursiveCharacterTextSplitter:
    """Split text into chunks using recursive character splitting.

    Tries separators in order: paragraph → sentence → word → character.
    Preserves semantic coherence while respecting chunk_size and overlap.

    For Markdown: splits on headers, keeping heading path in metadata.
    For code: splits on function/class boundaries.
    """

    # Default separators in priority order
    DEFAULT_SEPARATORS = ["\n\n", "\n", ". ", " ", ""]

    # Markdown header pattern
    _MD_HEADER = re.compile(r"^(#{1,6}\s+.+)$", re.MULTILINE)

    # Code block pattern (fenced)
    _CODE_BLOCK = re.compile(r"```[\s\S]*?```")

    def __init__(
        self,
        chunk_size: int = 512,
        chunk_overlap: int = 76,
        separators: list[str] | None = None,
        is_separator_regex: bool = False,
    ) -> None:
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.separators = separators or self.DEFAULT_SEPARATORS
        self.is_separator_regex = is_separator_regex

    def split_text(self, text: str, metadata: dict[str, Any] | None = None) -> list[Chunk]:
        """Split text into chunks with overlap.

        Strategy:
        1. Try each separator in order
        2. If a split produces chunks ≤ chunk_size, accept
        3. Otherwise, recurse with the next separator
        4. Apply overlap between consecutive chunks
        """
        if not text:
            return []

        base_metadata = metadata or {}
        chunks = self._split_recursive(text, self.separators, base_metadata)

        # Apply overlap
        if self.chunk_overlap > 0 and len(chunks) > 1:
            chunks = self._apply_overlap(chunks)

        # Assign indices and token counts
        result = []
        for i, chunk in enumerate(chunks):
            chunk.index = i
            chunk.token_count = self._estimate_tokens(chunk.text)
            result.append(chunk)

        return result

    def split_markdown(self, text: str, metadata: dict[str, Any] | None = None) -> list[Chunk]:
        """Split Markdown text on headers, preserving heading path in metadata."""
        if not text:
            return []

        base_metadata = metadata or {}
        chunks: list[Chunk] = []
        # Stack of (level, title) for nested heading paths.
        heading_stack: list[tuple[int, str]] = []
        current_text = ""

        def _heading_path() -> str:
            return " > ".join(title for _, title in heading_stack)

        for line in text.split("\n"):
            header_match = self._MD_HEADER.match(line)
            if header_match:
                # Save previous section
                if current_text.strip():
                    section_meta = {**base_metadata, "heading": _heading_path()}
                    sub_chunks = self.split_text(current_text.strip(), section_meta)
                    chunks.extend(sub_chunks)
                header_text = header_match.group(1).strip()
                level = len(header_text) - len(header_text.lstrip("#"))
                title = header_text.lstrip("#").strip()
                # Pop stack to parent level, then push new heading.
                while heading_stack and heading_stack[-1][0] >= level:
                    heading_stack.pop()
                heading_stack.append((level, title))
                current_text = line + "\n"
            else:
                current_text += line + "\n"

        # Last section
        if current_text.strip():
            section_meta = {**base_metadata, "heading": _heading_path()}
            sub_chunks = self.split_text(current_text.strip(), section_meta)
            chunks.extend(sub_chunks)

        # Re-index
        for i, chunk in enumerate(chunks):
            chunk.index = i

        return chunks

    def split_code(
        self, text: str, language: str = "python", metadata: dict[str, Any] | None = None
    ) -> list[Chunk]:
        """Split code on function/class boundaries.

        Falls back to recursive splitting if no code structure is found.
        """
        if not text:
            return []

        base_metadata = metadata or {}
        chunks: list[Chunk] = []

        # Simple heuristic: split on function/class definitions
        if language in ("python", "javascript", "typescript", "java", "go", "rust"):
            pattern = re.compile(
                r"^(\s*(?:async\s+def\s+\w+|def\s+\w+|class\s+\w+|function\s+\w+|func\s+\w+|pub fn\s+\w+|pub async fn\s+\w+|async fn\s+\w+|@[A-Za-z_][\w.]*.*))",
                re.MULTILINE,
            )
            matches = list(pattern.finditer(text))

            if len(matches) > 1:
                for i, match in enumerate(matches):
                    start = match.start()
                    end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
                    code_block = text[start:end].strip()
                    if code_block:
                        block_meta = {
                            **base_metadata,
                            "language": language,
                            "symbol": match.group(1).strip(),
                        }
                        # If block is too large, recursively split
                        if self._estimate_tokens(code_block) > self.chunk_size:
                            sub_chunks = self.split_text(code_block, block_meta)
                            chunks.extend(sub_chunks)
                        else:
                            chunks.append(
                                Chunk(
                                    text=code_block,
                                    metadata=block_meta,
                                    token_count=self._estimate_tokens(code_block),
                                )
                            )

        if not chunks:
            # Fallback to recursive splitting
            chunks = self.split_text(text, base_metadata)

        for i, chunk in enumerate(chunks):
            chunk.index = i

        return chunks

    def _split_recursive(
        self,
        text: str,
        separators: list[str],
        metadata: dict[str, Any],
    ) -> list[Chunk]:
        """Recursively split text using the given separators."""
        if not text:
            return []

        if not separators:
            # No more separators — force split by character
            return self._force_split(text, self.chunk_size, metadata)

        sep = separators[0]
        remaining = separators[1:]

        if sep == "":
            return self._force_split(text, self.chunk_size, metadata)

        # Split on the separator
        if self.is_separator_regex:
            parts = re.split(sep, text)
        else:
            parts = text.split(sep)

        chunks: list[Chunk] = []
        current_text = ""

        for part in parts:
            if not part:
                continue

            candidate = current_text + (sep if current_text else "") + part

            if self._estimate_tokens(candidate) <= self.chunk_size:
                current_text = candidate
            else:
                # Current part doesn't fit
                if current_text:
                    chunks.append(
                        Chunk(
                            text=current_text,
                            metadata=dict(metadata),
                            token_count=self._estimate_tokens(current_text),
                        )
                    )

                # If the part itself is too large, recurse with next separator
                if self._estimate_tokens(part) > self.chunk_size and remaining:
                    sub_chunks = self._split_recursive(part, remaining, metadata)
                    chunks.extend(sub_chunks)
                    current_text = ""
                else:
                    current_text = part

        if current_text:
            chunks.append(
                Chunk(
                    text=current_text,
                    metadata=dict(metadata),
                    token_count=self._estimate_tokens(current_text),
                )
            )

        return chunks

    def _force_split(self, text: str, size: int, metadata: dict[str, Any]) -> list[Chunk]:
        """Force split text into fixed-size chunks by character count."""
        chunks = []
        # Approximate: 1 token ≈ 4 chars
        char_size = size * 4
        for i in range(0, len(text), char_size):
            chunk_text = text[i : i + char_size]
            chunks.append(
                Chunk(
                    text=chunk_text,
                    metadata=dict(metadata),
                    token_count=self._estimate_tokens(chunk_text),
                )
            )
        return chunks

    def _apply_overlap(self, chunks: list[Chunk]) -> list[Chunk]:
        """Apply overlap between consecutive chunks."""
        if len(chunks) <= 1:
            return chunks

        result = [chunks[0]]
        overlap_chars = self.chunk_overlap * 4  # Approximate
        max_chars = self.chunk_size * 4

        for i in range(1, len(chunks)):
            prev_text = result[-1].text
            current_text = chunks[i].text

            # Tail slice of previous chunk as overlap (handles prev <= overlap).
            overlap_text = prev_text[max(0, len(prev_text) - overlap_chars) :]
            # Find the last good break point so overlap starts at a boundary.
            for sep in ["\n\n", "\n", ". ", " "]:
                idx = overlap_text.rfind(sep)
                if idx >= 0:
                    # Keep the separator at the start of the overlap.
                    overlap_text = overlap_text[idx:]
                    break
            merged = overlap_text + current_text if overlap_text else current_text

            # Post-merge size check: split if merged exceeds chunk_size
            # Preserve metadata boundaries: copy source metadata with overlap offset.
            base_meta = dict(chunks[i].metadata)
            base_meta["overlap_offset"] = max(0, len(prev_text) - len(overlap_text))
            if len(merged) > max_chars:
                # Split the merged text into pieces that fit within max_chars
                for j in range(0, len(merged), max_chars):
                    piece = merged[j : j + max_chars]
                    if piece:
                        piece_meta = dict(base_meta)
                        piece_meta["overlap_piece"] = j // max_chars
                        result.append(
                            Chunk(
                                text=piece,
                                metadata=piece_meta,
                                token_count=self._estimate_tokens(piece),
                            )
                        )
            else:
                result.append(
                    Chunk(
                        text=merged,
                        metadata=dict(base_meta),
                        token_count=self._estimate_tokens(merged),
                    )
                )

        return result

    def _estimate_tokens(self, text: str) -> int:
        """Estimate token count (1 token ≈ 4 chars)."""
        return len(text) // 4
