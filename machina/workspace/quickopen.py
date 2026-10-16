"""Bounded, cancellable recursive file index for Quick Open (plan §12).

Quick Open never performs an unbounded blocking scan. One
:class:`QuickOpenIndexer` per workspace binding (resolved root + epoch) owns
at most one asyncio scan task — single-flight, so concurrent requests share
the same walk instead of restarting it — and the walk yields to the event
loop after every bounded slice. A scan stops at the first of:

* ``max_files`` indexed files (20 000 by default): ``truncated`` with reason
  ``file-cap``;
* ``deadline`` seconds of wall time (10 s by default): reason ``deadline``;
* ``dir_entry_cap`` raw entries in one directory (5 000 by default): the
  capped directory is recorded in ``directory_caps`` and disclosed, and the
  walk continues elsewhere — reason ``directory-entry-cap``.

Unreadable directories are counted (``unreadable``) and disclosed (reason
``unreadable``) instead of failing the whole index, and excluded names (the
same default exclusion policy as the listing service) are never traversed.

Symlinks are followed only when their real target stays inside the root, and
a ``(st_dev, st_ino)`` visited-directory guard makes cycles — symlink loops
and aliased mounts — terminate. Nothing here touches the event loop with
synchronous filesystem work for longer than one yielded slice.

The index is cached per indexer with a TTL (60 s by default); :meth:`ensure`
rebuilds after expiry or on an explicit ``rebuild=True``. Result paths are
root-relative POSIX paths ranked by :func:`rank_entries` (exact basename,
then prefix, then substring, then subsequence; ties by path length, then
lexicographically).
"""

from __future__ import annotations

import asyncio
import logging
import os
import stat
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from machina.workspace.models import (
    DEFAULT_EXCLUSIONS,
    QUICK_OPEN_CACHE_TTL,
    QUICK_OPEN_DIR_ENTRY_CAP,
    QUICK_OPEN_INDEX_MAX_FILES,
    QUICK_OPEN_SCAN_DEADLINE,
    QuickOpenEntry,
)

logger = logging.getLogger(__name__)

#: Truncation/staleness reasons surfaced verbatim through the wire result.
REASON_RESULT_LIMIT = "result-limit"
REASON_FILE_CAP = "file-cap"
REASON_DEADLINE = "deadline"
REASON_DIRECTORY_ENTRY_CAP = "directory-entry-cap"
REASON_UNREADABLE = "unreadable"

#: Directory entries processed between event-loop yields.
INDEX_YIELD_EVERY = 512
#: Indexed files between progress callbacks (plus one final callback).
INDEX_PROGRESS_EVERY = 1024


def _is_subsequence(needle: str, haystack: str) -> bool:
    """Greedy in-order character containment (case already folded)."""
    position = 0
    if not needle:
        return True
    for char in haystack:
        if char == needle[position]:
            position += 1
            if position == len(needle):
                return True
    return False


def match_score(name: str, needle: str) -> int | None:
    """Ranking tier for one basename, or ``None`` when it does not match.

    ``0`` exact, ``1`` prefix, ``2`` substring, ``3`` subsequence (all
    case-insensitive); an empty needle matches everything (tier ``4``).
    """
    if not needle:
        return 4
    folded = name.casefold()
    if folded == needle:
        return 0
    if folded.startswith(needle):
        return 1
    if needle in folded:
        return 2
    if _is_subsequence(needle, folded):
        return 3
    return None


def rank_entries(
    entries: Sequence[str], query: str, limit: int
) -> tuple[list[QuickOpenEntry], int]:
    """Rank root-relative paths for *query*; returns (bounded page, total).

    Ties are stable: score tier, then path length, then lexicographic path —
    a deterministic order that never depends on scan order.
    """
    needle = query.casefold()
    scored: list[tuple[int, int, str, QuickOpenEntry]] = []
    for path in entries:
        name = path.rsplit("/", 1)[-1]
        score = match_score(name, needle)
        if score is None:
            continue
        directory = path.rsplit("/", 1)[0] if "/" in path else ""
        scored.append(
            (
                score,
                len(path),
                path,
                QuickOpenEntry(path=path, name=name, directory=directory),
            )
        )
    scored.sort(key=lambda item: (item[0], item[1], item[2]))
    total = len(scored)
    return [item[3] for item in scored[:limit]], total


class QuickOpenIndex:
    """One bounded in-memory index of root-relative file paths."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        exclusions: tuple[str, ...] = DEFAULT_EXCLUSIONS,
        max_files: int = QUICK_OPEN_INDEX_MAX_FILES,
        dir_entry_cap: int = QUICK_OPEN_DIR_ENTRY_CAP,
        deadline: float = QUICK_OPEN_SCAN_DEADLINE,
        yield_every: int = INDEX_YIELD_EVERY,
        progress_every: int = INDEX_PROGRESS_EVERY,
        on_progress: Callable[[int, bool], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        try:
            self.root = Path(os.path.realpath(root))
        except OSError:  # pragma: no cover - root vanished before we started
            self.root = Path(root)
        self.exclusions = tuple(exclusions)
        self.max_files = max(1, int(max_files))
        self.dir_entry_cap = max(1, int(dir_entry_cap))
        self.deadline = max(0.0, float(deadline))
        self.yield_every = max(1, int(yield_every))
        self.progress_every = max(1, int(progress_every))
        self._on_progress = on_progress
        self._clock = clock

        self.entries: list[str] = []
        self.done = False
        self.truncated = False
        self.reasons: list[str] = []
        #: Directories where the per-directory entry cap cut the listing short.
        self.directory_caps: list[str] = []
        self.unreadable = 0
        self.skipped_symlinks = 0
        self.scanned_directories = 0

    @property
    def scanned(self) -> int:
        """Files indexed so far (the disclosed progress count)."""
        return len(self.entries)

    def _mark(self, reason: str) -> None:
        self.truncated = True
        if reason not in self.reasons:
            self.reasons.append(reason)

    def _progress(self, done: bool) -> None:
        if self._on_progress is None:
            return
        try:
            self._on_progress(self.scanned, done)
        except Exception:  # a dead channel must never fail the index
            logger.debug("quick-open progress callback failed", exc_info=True)

    def _add(self, rel: str) -> bool:
        """Index one file path; ``False`` when the global file cap was hit."""
        if len(self.entries) >= self.max_files:
            self._mark(REASON_FILE_CAP)
            return False
        self.entries.append(rel)
        return True

    @staticmethod
    def _identity(path: str | os.PathLike[str]) -> tuple[int, int] | None:
        try:
            st = os.stat(path)
        except OSError:
            return None
        return (st.st_dev, st.st_ino)

    def _queue_directory(
        self,
        raw: os.DirEntry[str],
        rel: str,
        visited: set[tuple[int, int]],
        pending: list[str],
    ) -> None:
        try:
            st = raw.stat(follow_symlinks=False)
        except OSError:
            self.unreadable += 1
            self._mark(REASON_UNREADABLE)
            return
        identity = (st.st_dev, st.st_ino)
        if identity in visited:
            return  # duplicate/aliased directory: already (being) walked
        visited.add(identity)
        pending.append(rel)

    def _scan_symlink(
        self,
        raw: os.DirEntry[str],
        rel: str,
        visited: set[tuple[int, int]],
        pending: list[str],
    ) -> None:
        """Follow a symlink only when its target stays inside the root."""
        try:
            target = os.path.realpath(raw.path)
        except OSError:
            self.skipped_symlinks += 1
            return
        root = str(self.root)
        if target != root and not target.startswith(root + os.sep):
            self.skipped_symlinks += 1
            return
        try:
            st = os.stat(raw.path)  # the inside-root target (kind + cycle guard)
        except OSError:
            self.skipped_symlinks += 1
            return
        identity = (st.st_dev, st.st_ino)
        if stat.S_ISDIR(st.st_mode):
            if identity in visited:
                return
            visited.add(identity)
            pending.append(rel)
        elif stat.S_ISREG(st.st_mode):
            self._add(rel)

    async def scan(self) -> None:
        """Walk the root; yields to the event loop after every bounded slice.

        Never blocks the loop for longer than one slice: the filesystem work
        happens in synchronous bursts bounded by ``dir_entry_cap`` and
        ``yield_every``, and the scan yields before touching the filesystem at
        all so a caller's bounded wait and cancellation are honored promptly.
        """
        await asyncio.sleep(0)
        deadline = self._clock() + self.deadline
        visited: set[tuple[int, int]] = set()
        root_identity = self._identity(self.root)
        if root_identity is not None:
            visited.add(root_identity)
        pending: list[str] = [""]
        examined = 0
        stop = False
        while pending and not stop:
            rel_dir = pending.pop()
            self.scanned_directories += 1
            try:
                iterator = os.scandir(self.root if not rel_dir else self.root / rel_dir)
            except OSError:
                self.unreadable += 1
                self._mark(REASON_UNREADABLE)
                continue
            count = 0
            try:
                with iterator:
                    for raw in iterator:
                        if self._clock() >= deadline:
                            self._mark(REASON_DEADLINE)
                            stop = True
                            break
                        count += 1
                        if count > self.dir_entry_cap:
                            self.directory_caps.append(rel_dir or ".")
                            self._mark(REASON_DIRECTORY_ENTRY_CAP)
                            break  # bounded per directory; the walk goes on
                        name = raw.name
                        if name in self.exclusions:
                            continue
                        rel = f"{rel_dir}/{name}" if rel_dir else name
                        if raw.is_symlink():
                            self._scan_symlink(raw, rel, visited, pending)
                        elif raw.is_dir(follow_symlinks=False):
                            self._queue_directory(raw, rel, visited, pending)
                        elif raw.is_file(follow_symlinks=False):
                            if not self._add(rel):
                                stop = True
                                break
                        examined += 1
                        if examined % self.yield_every == 0:
                            if examined % self.progress_every == 0:
                                self._progress(False)
                            await asyncio.sleep(0)
            except OSError:
                self.unreadable += 1
                self._mark(REASON_UNREADABLE)
            await asyncio.sleep(0)
        self.done = True
        self._progress(True)


class QuickOpenIndexer:
    """Single-flight scan owner + TTL cache for one (root, epoch) binding."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        key: tuple[str, int] = ("", 0),
        exclusions: tuple[str, ...] = DEFAULT_EXCLUSIONS,
        ttl: float = QUICK_OPEN_CACHE_TTL,
        max_files: int = QUICK_OPEN_INDEX_MAX_FILES,
        dir_entry_cap: int = QUICK_OPEN_DIR_ENTRY_CAP,
        deadline: float = QUICK_OPEN_SCAN_DEADLINE,
        yield_every: int = INDEX_YIELD_EVERY,
        progress_every: int = INDEX_PROGRESS_EVERY,
        on_progress: Callable[[int, bool], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.key = key
        try:
            self.root = Path(os.path.realpath(root))
        except OSError:  # pragma: no cover - root vanished before we started
            self.root = Path(root)
        self.exclusions = tuple(exclusions)
        self.ttl = max(0.0, float(ttl))
        self._max_files = max_files
        self._dir_entry_cap = dir_entry_cap
        self._deadline = deadline
        self._yield_every = yield_every
        self._progress_every = progress_every
        self._on_progress = on_progress
        self._clock = clock
        self._task: asyncio.Task[None] | None = None
        self._index: QuickOpenIndex | None = None
        self._built_at: float | None = None

    @property
    def indexing(self) -> bool:
        return self._task is not None and not self._task.done()

    def current(self) -> QuickOpenIndex | None:
        """The live index (complete or partial), or ``None`` when retired."""
        return self._index

    def _new_index(self) -> QuickOpenIndex:
        return QuickOpenIndex(
            self.root,
            exclusions=self.exclusions,
            max_files=self._max_files,
            dir_entry_cap=self._dir_entry_cap,
            deadline=self._deadline,
            yield_every=self._yield_every,
            progress_every=self._progress_every,
            on_progress=self._on_progress,
            clock=self._clock,
        )

    def _begin_scan(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - ensure() runs inside a loop
            return
        index = self._new_index()
        self._index = index
        self._built_at = None
        self._task = loop.create_task(self._run(index), name=f"workspace-quick-open:{self.root}")

    async def _run(self, index: QuickOpenIndex) -> None:
        try:
            await index.scan()
        except asyncio.CancelledError:
            raise
        except Exception:  # a failed walk is disclosed, never a raised query
            logger.exception("quick-open index scan failed")
        finally:
            if self._index is index:
                if index.done:
                    self._built_at = self._clock()
                else:
                    self._index = None
                    self._built_at = None
            if self._task is asyncio.current_task():
                self._task = None

    def _expired(self) -> bool:
        built = self._built_at
        return built is None or (self._clock() - built) >= self.ttl

    async def ensure(self, *, wait: float, rebuild: bool = False) -> tuple[QuickOpenIndex, bool]:
        """Return the current index, starting a single-flight scan if needed.

        Concurrent callers share one scan. When it is still running after
        ``wait`` seconds the current partial index is returned with
        ``indexing=True``; the caller discloses progress and re-queries later.
        A completed index is reused until its TTL expires or ``rebuild`` is
        requested.
        """
        if rebuild:
            # An explicit rebuild retires the cached index even when its scan
            # already settled; ``cancel`` alone would leave a completed index.
            self.invalidate()
        index = self._index
        if index is not None and index.done and not self._expired():
            return index, False
        task = self._task
        if task is None or task.done():
            self._begin_scan()
            task = self._task
        if task is not None and not task.done():
            await asyncio.wait({task}, timeout=max(0.0, float(wait)))
        index = self._index
        if index is None:
            # A concurrent cancel or a failed scan retired it: start fresh.
            self._begin_scan()
            index = self._index
            if index is None:  # pragma: no cover - defensive
                index = self._new_index()
        return index, not index.done

    def cancel(self) -> bool:
        """Cancel an in-flight scan; ``True`` only when one was cancelled.

        The retired index is dropped (never served as current); the next
        :meth:`ensure` starts a fresh scan.
        """
        task = self._task
        if task is None or task.done():
            return False
        self._task = None
        self._index = None
        self._built_at = None
        task.cancel()
        return True

    def invalidate(self) -> None:
        """Explicit rebuild: retire any live scan and drop the cached index."""
        self.cancel()
        self._index = None
        self._built_at = None
