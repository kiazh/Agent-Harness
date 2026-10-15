"""Evidence map: log of (path, range) reads; covered ranges served from cache."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class _Span:
    start: int
    end: int  # end-exclusive, line numbers 1-based


@dataclass
class EvidenceMap:
    """Records every read as path + line range. ``read`` serves fully-covered
    ranges from the in-memory line cache (no file I/O); otherwise it reads the
    file once, caches its lines, and logs the span. ``reads_from_disk`` counts
    real file opens, so tests can assert repeat = zero work."""

    _lines: dict[str, list[str]] = field(default_factory=dict)
    _spans: dict[str, list[_Span]] = field(default_factory=dict)
    # Tests only (read by tests; incremented internally) (review 2026-10-14).
    reads_from_disk: int = 0

    def read(self, path: str, start: int = 1, end: int | None = None) -> list[str]:
        """Read lines [start, end) (1-based, end-exclusive; None = to EOF).

        Fully-covered ranges are served from the in-memory line cache without
        new file I/O; the span is still logged. Raises ValueError for
        start < 1, FileNotFoundError for missing paths.
        """
        if start < 1:
            raise ValueError("start must be >= 1")
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        return self._read(path, start, end)

    def _read(self, path: str, start: int, end: int | None) -> list[str]:
        lines = self._lines.get(path)
        if lines is None:
            with open(path, encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
            self._lines[path] = lines
            self.reads_from_disk += 1
        lo = max(1, start) - 1
        hi = len(lines) if end is None else min(len(lines), max(lo, end - 1))
        self._log(path, lo + 1, hi + 1)
        return lines[lo:hi]

    def _log(self, path: str, start: int, end: int) -> None:
        spans = self._spans.setdefault(path, [])
        spans.append(_Span(start, end))
        spans.sort(key=lambda s: (s.start, s.end))
        merged: list[_Span] = []
        for s in spans:
            if merged and s.start <= merged[-1].end:
                merged[-1].end = max(merged[-1].end, s.end)
            else:
                merged.append(_Span(s.start, s.end))
        self._spans[path] = merged

    def covered(self, path: str) -> list[tuple[int, int]]:
        """Merged (start, end-exclusive) spans read so far for path."""
        return [(s.start, s.end) for s in self._spans.get(path, [])]

    # Span-coverage query; repomap tests use it, no production caller. Kept (R4-106).
    def is_covered(self, path: str, start: int, end: int) -> bool:
        return any(s.start <= start and s.end >= end for s in self._spans.get(path, []))
