"""Chunking strategies — recursive character splitting with structure awareness."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, ClassVar


@dataclass
class Chunk:
    """A text chunk with metadata.

    ``start``/``end`` are half-open character offsets into the text passed
    to :meth:`RecursiveCharacterTextSplitter.split_text` (``None`` when
    unknown — a chunk assembled by hand, or one produced by
    ``split_markdown``/``split_code``, which split per section so no
    document-level offset exists). They let
    :meth:`RecursiveCharacterTextSplitter._apply_overlap` verify that two
    chunks really abut in the source before gluing one's tail onto the other:
    overlap that bridges a dropped separator would fabricate a span that
    never existed (R5-F2).
    """

    text: str
    index: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    token_count: int = 0
    start: int | None = None
    end: int | None = None


#: File extensions and common aliases mapped to the canonical language names
#: understood by :meth:`RecursiveCharacterTextSplitter.split_code`.
_LANGUAGE_ALIASES: dict[str, str] = {
    "py": "python",
    "python": "python",
    "js": "javascript",
    "javascript": "javascript",
    "ts": "typescript",
    "typescript": "typescript",
    "java": "java",
    "go": "go",
    "rs": "rust",
    "rust": "rust",
}


def normalize_language(language: str | None) -> str:
    """Normalize an extension/alias ("py", ".js", "TS") to a splitter language.

    Unknown values are returned lowercased (and un-dotted) so the caller can
    still inspect them; only known aliases are rewritten.
    """
    value = (language or "").strip().lstrip(".").lower()
    return _LANGUAGE_ALIASES.get(value, value)


class RecursiveCharacterTextSplitter:
    """Split text into chunks using recursive character splitting.

    Tries separators in order: paragraph → sentence → word → character.
    Preserves semantic coherence while respecting chunk_size and overlap.

    For Markdown: splits on headers, keeping heading path in metadata.
    For code: splits on function/class boundaries.
    """

    # Default separators in priority order
    DEFAULT_SEPARATORS: ClassVar[list[str]] = ["\n\n", "\n", ". ", " ", ""]

    # Markdown header pattern
    _MD_HEADER = re.compile(r"^(#{1,6}\s+.+)$", re.MULTILINE)

    # One fenced-code delimiter line (three or more backticks or tildes).
    _FENCE = re.compile(r"^(`{3,}|~{3,})")

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

        # Hard char bound on base emissions (property: no emitted chunk
        # exceeds chunk_size*4). The token estimate floors (len // 4), so a
        # text just above the 4x boundary can pass the fits-check and be
        # emitted oversized (e.g. 33 chars -> est 8 at chunk_size=8).
        # Character-split such chunks: contiguous slices keep the
        # subsequence/substring properties intact.
        max_chars = self.chunk_size * 4
        if any(len(chunk.text) > max_chars for chunk in chunks):
            bounded: list[Chunk] = []
            for chunk in chunks:
                if len(chunk.text) > max_chars:
                    offset = 0
                    for piece in self._hard_split(chunk.text, max_chars):
                        start = None if chunk.start is None else chunk.start + offset
                        end = None if start is None else start + len(piece)
                        bounded.append(
                            Chunk(
                                text=piece,
                                metadata=dict(chunk.metadata),
                                token_count=self._estimate_tokens(piece),
                                start=start,
                                end=end,
                            )
                        )
                        offset += len(piece)
                else:
                    bounded.append(chunk)
            chunks = bounded

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
        """Split Markdown text on headers, preserving heading path in metadata.

        '#'-prefixed lines inside fenced code blocks (backticks or tildes,
        closed or unclosed) are content, never headings (R4-088): the fence
        tracker runs before the heading match.
        """
        if not text:
            return []

        base_metadata = metadata or {}
        chunks: list[Chunk] = []
        # Stack of (level, title) for nested heading paths.
        heading_stack: list[tuple[int, str]] = []
        current_text = ""
        fence: str | None = None

        def _heading_path() -> str:
            return " > ".join(title for _, title in heading_stack)

        for line in text.split("\n"):
            stripped = line.lstrip()
            fence_match = self._FENCE.match(stripped)
            if fence_match:
                marker = fence_match.group(1)
                rest = stripped[len(marker) :].strip()
                if fence is None:
                    fence = marker
                elif marker[0] == fence[0] and len(marker) >= len(fence) and not rest:
                    # A closing fence repeats its opening marker without an
                    # info string; anything else stays inside the block.
                    fence = None
                current_text += line + "\n"
                continue
            header_match = None if fence else self._MD_HEADER.match(line)
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

        # Re-index. Offsets from the per-section split_text calls are relative
        # to each section substring, not the document: don't advertise them.
        for i, chunk in enumerate(chunks):
            chunk.index = i
            chunk.start = None
            chunk.end = None

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
        language = normalize_language(language)

        # Simple heuristic: split on function/class definitions. Decorator
        # lines ('@...') are not symbols themselves: they attach to the
        # definition that follows, so a decorated function forms ONE chunk
        # including its decorators (R4-088) regardless of how many
        # definitions the file holds.
        if language in ("python", "javascript", "typescript", "java", "go", "rust"):
            pattern = re.compile(
                r"^(\s*(?:async\s+def\s+\w+|def\s+\w+|class\s+\w+|function\s+\w+|func\s+\w+|pub fn\s+\w+|pub async fn\s+\w+|async fn\s+\w+|@[A-Za-z_][\w.]*.*))",
                re.MULTILINE,
            )
            matches = list(pattern.finditer(text))

            # Group each decorator run with the definition it decorates. The
            # chunk starts at the first decorator; the symbol is the
            # definition's signature.
            definitions: list[tuple[int, str]] = []
            pending_decorator: int | None = None
            for match in matches:
                symbol = match.group(1).strip()
                if symbol.startswith("@"):
                    if pending_decorator is None:
                        pending_decorator = match.start()
                    continue
                start = pending_decorator if pending_decorator is not None else match.start()
                pending_decorator = None
                definitions.append((start, symbol))
            if pending_decorator is not None:
                # Trailing decorator without a definition: keep the source
                # text as its own chunk rather than dropping it.
                trailing = text[pending_decorator:].strip()
                definitions.append((pending_decorator, trailing.split("\n", 1)[0].strip()))

            if definitions:
                # Header content before the first definition (package
                # declarations, imports, module constants) is not a symbol but
                # must never be dropped: keep it as a preamble chunk.
                preamble = text[: definitions[0][0]].strip()
                if preamble:
                    preamble_meta = {**base_metadata, "language": language, "symbol": None}
                    if self._estimate_tokens(preamble) > self.chunk_size:
                        chunks.extend(self.split_text(preamble, preamble_meta))
                    else:
                        chunks.append(
                            Chunk(
                                text=preamble,
                                metadata=preamble_meta,
                                token_count=self._estimate_tokens(preamble),
                            )
                        )
                for i, (start, symbol) in enumerate(definitions):
                    end = definitions[i + 1][0] if i + 1 < len(definitions) else len(text)
                    code_block = text[start:end].strip()
                    if code_block:
                        block_meta = {
                            **base_metadata,
                            "language": language,
                            "symbol": symbol,
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

        # Offsets from the per-block split_text calls are relative to each
        # block substring, not the document: don't advertise them.
        for i, chunk in enumerate(chunks):
            chunk.index = i
            chunk.start = None
            chunk.end = None

        return chunks

    def _split_recursive(
        self,
        text: str,
        separators: list[str],
        metadata: dict[str, Any],
        base_offset: int = 0,
    ) -> list[Chunk]:
        """Recursively split text using the given separators.

        Chunks are assembled as *literal slices of ``text``* (offsets in
        ``base_offset``), never by re-joining a separator-stripped part list.
        That keeps every emission a genuine source substring and keeps
        consecutive emissions adjacent even when a too-large part is recursed
        (R5-F2/P3-1): the separator runs before AND after the recursed part
        are captured as real source gaps instead of being silently dropped,
        so :meth:`_apply_overlap` can never splice across one.
        """
        if not text:
            return []

        if not separators:
            # No more separators — force split by character
            return self._force_split(text, self.chunk_size, metadata, base_offset)

        sep = separators[0]
        remaining = separators[1:]

        if sep == "":
            return self._force_split(text, self.chunk_size, metadata, base_offset)

        # Content spans (start, end) between separators, empty spans retained
        # so the offsets stay exact (``sep.join(text.split(sep)) == text``).
        spans = self._separator_spans(text, sep)
        parts = [(start, end) for start, end in spans if end > start]

        chunks: list[Chunk] = []
        # Accumulated span [cur_start, ...) in `text`, as source offsets.
        cur_start: int | None = None

        for i, (part_start, part_end) in enumerate(parts):
            if cur_start is None:
                cand_start = part_start
            else:
                cand_start = cur_start
            # Candidate includes the true separator run before this part.
            candidate = text[cand_start:part_end]

            if self._estimate_tokens(candidate) <= self.chunk_size:
                cur_start = cand_start
                continue

            # Candidate doesn't fit. Emit everything up to (and including)
            # the real source gap preceding this part, so the emitted chunk
            # ends exactly where this part begins — adjacency preserved.
            if cur_start is not None:
                emitted = text[cur_start:part_start]
                chunks.append(
                    Chunk(
                        text=emitted,
                        metadata=dict(metadata),
                        token_count=self._estimate_tokens(emitted),
                        start=base_offset + cur_start,
                        end=base_offset + part_start,
                    )
                )

            # If the part itself is too large, recurse with next separator.
            part_text = text[part_start:part_end]
            if self._estimate_tokens(part_text) > self.chunk_size and remaining:
                sub_chunks = self._split_recursive(
                    part_text, remaining, metadata, base_offset + part_start
                )
                # Bridge the recursion back over a dropped LEADING separator
                # (P3-1): a part can begin with a character that is a
                # separator of the next level (``"a\n\n\nb".split("\n\n")``
                # yields ``"\nb"``). The inner recursion then treats it as a
                # separator, drops the empty leading span, and starts at the
                # following character — leaving that one source character
                # covered by no chunk. Extend the first sub-chunk over the
                # genuine source gap so the emission still starts at the part
                # boundary, the mirror of the forward bridge below.
                if sub_chunks:
                    first = sub_chunks[0]
                    first_start = (
                        first.start - base_offset if first.start is not None else part_start
                    )
                    leading = text[part_start:first_start]
                    if leading:
                        first.text = leading + first.text
                        first.token_count = self._estimate_tokens(first.text)
                    if first.start is not None:
                        first.start = base_offset + part_start
                # Bridge the recursion forward to the following part: extend
                # the last sub-chunk over the source gap up to the next part's
                # start (a genuine source slice, never fabricated). Start the
                # gap at the sub-chunk's real end, which may sit before
                # ``part_end`` because the recursion can drop trailing
                # separators inside the part. For the LAST part the mirror of
                # "the next part's start" is the end of this level's text:
                # otherwise a trailing separator run (``source[18:20] == ". "``
                # in the review reproduction) is silently dropped (P0-1).
                if sub_chunks:
                    last = sub_chunks[-1]
                    next_start = parts[i + 1][0] if i + 1 < len(parts) else len(text)
                    last_end = last.end - base_offset if last.end is not None else part_end
                    gap = text[last_end:next_start]
                    if gap:
                        last.text = last.text + gap
                        last.token_count = self._estimate_tokens(last.text)
                    if last.end is not None:
                        last.end = base_offset + next_start
                chunks.extend(sub_chunks)
                cur_start = None
            else:
                cur_start = part_start

        if cur_start is not None:
            # Always emit the trailing remainder (P0-1): the final part's
            # separator run is a literal source slice, and dropping it loses
            # real characters when the boundary lands on a separator (e.g.
            # ". "). No duplication: this span starts where the last emission
            # ended.
            emitted = text[cur_start : len(text)]
            chunks.append(
                Chunk(
                    text=emitted,
                    metadata=dict(metadata),
                    token_count=self._estimate_tokens(emitted),
                    start=base_offset + cur_start,
                    end=base_offset + len(text),
                )
            )

        return chunks

    def _separator_spans(self, text: str, sep: str) -> list[tuple[int, int]]:
        """Return ``(start, end)`` content spans split out of *text* by *sep*.

        For a literal separator the spans are exact because
        ``sep.join(text.split(sep)) == text``. For a regex separator the
        spans are the runs between matches (non-capturing separators).
        """
        if not self.is_separator_regex:
            spans: list[tuple[int, int]] = []
            cursor = 0
            for piece in text.split(sep):
                spans.append((cursor, cursor + len(piece)))
                cursor += len(piece) + len(sep)
            return spans

        spans = []
        last = 0
        for match in re.finditer(sep, text):
            spans.append((last, match.start()))
            last = match.end()
        spans.append((last, len(text)))
        return spans

    def _force_split(
        self,
        text: str,
        size: int,
        metadata: dict[str, Any],
        base_offset: int = 0,
    ) -> list[Chunk]:
        """Force split text into fixed-size chunks by character count."""
        if size <= 0:
            raise ValueError(f"chunk size must be positive, got {size}")
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
                    start=base_offset + i,
                    end=base_offset + i + len(chunk_text),
                )
            )
        return chunks

    def _apply_overlap(self, chunks: list[Chunk]) -> list[Chunk]:
        """Apply overlap between consecutive chunks.

        The overlap window is cut at the LAST matching separator (max idx),
        so the emitted prefix is a clean suffix of the previous chunk
        starting at the boundary nearest the end of the window. Applying
        overlap never increases the chunk count: when the merged text would
        exceed the hard limit, the overlap is trimmed to fit and the current
        chunk is preserved whole — the old overflow branch emitted duplicate
        fragments of the previous chunk instead of delivering overlap.

        Overlap is only prepended when the previous chunk and the current
        chunk genuinely abut in the source (``prev.end == cur.start``). A
        suffix of a chunk that is separated from the next chunk by dropped
        source characters must never be glued on, or the merge fabricates a
        span that never existed (R5-F2). When offsets are unknown the guard
        is skipped for backward compatibility.
        """
        if len(chunks) <= 1:
            return chunks

        result = [chunks[0]]
        overlap_chars = self.chunk_overlap * 4  # Approximate
        max_chars = self.chunk_size * 4

        for i in range(1, len(chunks)):
            prev = result[-1]
            prev_text = prev.text
            current = chunks[i]
            current_text = current.text

            # Tail slice of previous chunk as overlap (handles prev <= overlap).
            overlap_text = prev_text[max(0, len(prev_text) - overlap_chars) :]
            # Cut the overlap window at the LAST matching separator (max idx)
            # so the overlap is a boundary-aligned suffix of the previous
            # chunk rather than whatever separator comes first in priority.
            best_idx = -1
            for sep in ("\n\n", "\n", ". ", " "):
                idx = overlap_text.rfind(sep)
                if idx > best_idx:
                    best_idx = idx
            if best_idx >= 0:
                # Keep the separator at the start of the overlap.
                overlap_text = overlap_text[best_idx:]

            # Adjacency guard: refuse overlap that would bridge a gap in the
            # source (only checkable when both chunks carry offsets).
            if prev.end is not None and current.start is not None and prev.end != current.start:
                overlap_text = ""

            base_meta = dict(current.metadata)

            if len(current_text) >= max_chars:
                # Current chunk already fills the hard limit: emit it with a
                # boundary-aligned hard split. No overlap is appended (there
                # is no room) and nothing is duplicated.
                offset = 0
                for piece_idx, piece in enumerate(self._hard_split(current_text, max_chars)):
                    piece_meta = dict(base_meta)
                    piece_meta["overlap_piece"] = piece_idx
                    start = None if current.start is None else current.start + offset
                    result.append(
                        Chunk(
                            text=piece,
                            metadata=piece_meta,
                            token_count=self._estimate_tokens(piece),
                            start=start,
                            end=None if start is None else start + len(piece),
                        )
                    )
                    offset += len(piece)
                continue

            # Trim the overlap (never the current text) so the merged chunk
            # respects the hard limit without duplicating content.
            room = max_chars - len(current_text)
            if len(overlap_text) > room:
                overlap_text = overlap_text[-room:] if room > 0 else ""
            merged = overlap_text + current_text if overlap_text else current_text
            start = current.start
            if overlap_text and current.start is not None:
                start = current.start - len(overlap_text)
            result.append(
                Chunk(
                    text=merged,
                    metadata=dict(base_meta),
                    token_count=self._estimate_tokens(merged),
                    start=start,
                    end=current.end,
                )
            )

        return result

    @staticmethod
    def _hard_split(text: str, max_chars: int) -> list[str]:
        """Split *text* into pieces ≤ max_chars, preferring separator cuts.

        ``max_chars`` must be positive: a non-positive bound cannot advance
        the cursor and would loop forever (R4-088).
        """
        if max_chars <= 0:
            raise ValueError(f"max_chars must be positive, got {max_chars}")
        pieces: list[str] = []
        start = 0
        while start < len(text):
            end = min(start + max_chars, len(text))
            if end < len(text):
                cut = -1
                cut_len = 0
                for sep in ("\n\n", "\n", ". ", " "):
                    idx = text.rfind(sep, start + 1, end)
                    if idx > cut:
                        cut = idx
                        cut_len = len(sep)
                if cut > start:
                    end = cut + cut_len
            pieces.append(text[start:end])
            start = end
        return [piece for piece in pieces if piece]

    def _estimate_tokens(self, text: str) -> int:
        """Estimate token count (1 token ≈ 4 chars)."""
        return len(text) // 4
