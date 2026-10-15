"""Final review 2026-10-15 — file tool fixes (F01, F13, code-review P2 #3/#4).

Evidence: ``docs/verification/proofs/final_review_2026-10-15/fix_file.md`` and
the probes under ``docs/verification/proofs/final_review_2026-10-15/logs/``.

RED (captured pre-fix):

- **F01** — an ancestor swapped to an outside-directory symlink between the
  pathname containment check and ``lstat``/``open`` redirected a workspace
  write outside the base (``probe_f01_before.log``: ESCAPE).
- **F13** — ``resolve_path``/``read_file`` blocked forever on a writer-less
  FIFO, freezing the event loop (``probe_f13_before.log``: HANG).
- **code-review #4** — ``_read_page`` read a 100k-line file to EOF for a
  10-line page (``probe_readpage_before.log``: 100001 readline calls).

GREEN: the write chain walks from the trusted root with ``openat`` +
``O_NOFOLLOW|O_DIRECTORY`` (creating levels with ``mkdirat``); non-regular
files are rejected before and after nonblocking opens; the total-line scan is
bounded; and ``_atomic_write_file`` delegates to ``_atomic_write_file_at``.
"""

from __future__ import annotations

import asyncio
import os
import stat as statmod
import threading

import pytest

import machina.tools.file as fmod
from machina.core.exceptions import ToolError

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _require_symlinks(tmp_path) -> None:
    probe = tmp_path / ".symlink_probe"
    target = tmp_path / ".symlink_target"
    target.mkdir(exist_ok=True)
    try:
        probe.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    finally:
        try:
            if os.path.islink(probe):
                os.unlink(probe)
        except OSError:
            pass


def _require_fifo(tmp_path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("mkfifo unavailable on this platform")
    try:
        p = tmp_path / ".fifo_probe"
        os.mkfifo(p)
        os.unlink(p)
    except OSError:
        pytest.skip("mkfifo unsupported")


def _call_within(fn, timeout: float = 5.0):
    """Run *fn* in a daemon thread; fail (never hang) if it does not return."""
    box: dict = {}

    def runner() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 — recorded for the assertion
            box["error"] = exc

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join(timeout)
    assert not thread.is_alive(), "call blocked past the timeout (F13 regression?)"
    return box


def _setup_ancestor_tree(tmp_path):
    base = tmp_path / "ws"
    (base / "a" / "b").mkdir(parents=True)
    (base / "a" / "b" / "file.txt").write_text("INSIDE")
    outside = tmp_path / "outside"
    (outside / "b").mkdir(parents=True)
    (outside / "b" / "file.txt").write_text("ORIGINAL")
    return base, outside


def _swap_a_to_outside(base, outside) -> None:
    os.rename(base / "a", base / ".parking")
    os.symlink(outside, base / "a", target_is_directory=True)


# ---------------------------------------------------------------------------
# F01 — ancestor swap cannot redirect the workspace write outside
# ---------------------------------------------------------------------------


def test_ancestor_swap_before_component_open_fails_closed(tmp_path, monkeypatch):
    """The exact F01 window: ancestor becomes an outside symlink before the
    parent directory is opened. The descriptor walk rejects it (no escape)."""
    _require_symlinks(tmp_path)
    base, outside = _setup_ancestor_tree(tmp_path)
    monkeypatch.setattr(fmod, "_BASE_DIR", base.resolve())

    real_open = fmod._os_open
    state = {"fired": False}

    def hook(path, flags, *args, **kwargs):
        name = path if isinstance(path, str) else getattr(path, "name", "")
        if not state["fired"] and kwargs.get("dir_fd") is not None and name == "a":
            state["fired"] = True
            _swap_a_to_outside(base, outside)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(fmod, "_os_open", hook)
    with pytest.raises(Exception):
        asyncio.run(fmod.write_file("a/b/file.txt", "ATTACKER"))

    assert state["fired"], "swap seam (component open) was never reached"
    assert (outside / "b" / "file.txt").read_text() == "ORIGINAL"
    assert sorted(os.listdir(outside)) == ["b"]


def test_ancestor_swap_after_open_stays_inside(tmp_path, monkeypatch):
    """The ancestor is swapped only after its descriptor is held: the write is
    anchored to the renamed (still inside-base) directory, not the symlink."""
    _require_symlinks(tmp_path)
    base, outside = _setup_ancestor_tree(tmp_path)
    monkeypatch.setattr(fmod, "_BASE_DIR", base.resolve())

    real_open = fmod._os_open
    state = {"fired": False}

    def hook(path, flags, *args, **kwargs):
        name = path if isinstance(path, str) else getattr(path, "name", "")
        if (
            not state["fired"]
            and kwargs.get("dir_fd") is not None
            and isinstance(name, str)
            and name.startswith(".file.txt.")
        ):
            state["fired"] = True
            _swap_a_to_outside(base, outside)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(fmod, "_os_open", hook)
    result = asyncio.run(fmod.write_file("a/b/file.txt", "ATTACKER"))

    assert state["fired"], "swap seam (temp creation) was never reached"
    assert "Successfully wrote" in result
    assert (outside / "b" / "file.txt").read_text() == "ORIGINAL"
    assert sorted(os.listdir(outside)) == ["b"]
    # Anchored to the renamed directory, which is still inside the base.
    assert (base / ".parking" / "b" / "file.txt").read_text() == "ATTACKER"


# ---------------------------------------------------------------------------
# F13 — non-regular files never block the event loop
# ---------------------------------------------------------------------------


def test_resolve_path_rejects_fifo_without_blocking(tmp_path, monkeypatch):
    _require_fifo(tmp_path)
    base = tmp_path / "ws"
    base.mkdir()
    os.mkfifo(base / "pipe")
    monkeypatch.setattr(fmod, "_BASE_DIR", base.resolve())

    box = _call_within(lambda: fmod.resolve_path("pipe"))
    assert isinstance(box.get("error"), ValueError)
    assert "not a regular file" in str(box["error"])


def test_read_file_fifo_raises_typed_error_without_blocking(tmp_path, monkeypatch):
    _require_fifo(tmp_path)
    base = tmp_path / "ws"
    base.mkdir()
    os.mkfifo(base / "pipe")
    monkeypatch.setattr(fmod, "_BASE_DIR", base.resolve())

    box = _call_within(lambda: asyncio.run(fmod.read_file("pipe", max_size=1024)))
    assert isinstance(box.get("error"), ToolError)


def test_open_secure_read_rejects_fifo(tmp_path):
    _require_fifo(tmp_path)
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)

    with pytest.raises(ValueError, match="not a regular file"):
        fmod._open_secure_read(fifo)


def test_open_secure_read_still_reads_regular_files(tmp_path):
    target = tmp_path / "note.txt"
    target.write_text("content")
    fd = fmod._open_secure_read(target, check_base=False)
    try:
        assert statmod.S_ISREG(os.fstat(fd).st_mode)
    finally:
        os.close(fd)


async def test_list_files_skips_fifo_without_blocking(tmp_path, monkeypatch):
    _require_fifo(tmp_path)
    base = tmp_path / "ws"
    base.mkdir()
    os.mkfifo(base / "pipe")
    (base / "ok.txt").write_text("hi")
    monkeypatch.setattr(fmod, "_BASE_DIR", base.resolve())

    box = _call_within(lambda: asyncio.run(fmod.list_files(".")))
    assert "error" not in box, box
    assert "ok.txt" in box["value"]
    assert "pipe" not in box["value"]


# ---------------------------------------------------------------------------
# outside/full-mode fallback keeps working (explicitly retained)
# ---------------------------------------------------------------------------


def test_write_file_outside_full_mode_still_writes(tmp_path, monkeypatch):
    base = tmp_path / "ws"
    base.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setattr(fmod, "_BASE_DIR", base.resolve())
    monkeypatch.setattr(fmod, "_full_mode", lambda: True)

    target = outside / "note.txt"
    result = asyncio.run(fmod.write_file(str(target), "outside-content"))

    assert "Successfully wrote" in result
    assert target.read_text() == "outside-content"


# ---------------------------------------------------------------------------
# code-review #4 — bounded total-line scan (no full-file read for a page)
# ---------------------------------------------------------------------------


class _CountingStream:
    def __init__(self, path):
        self._f = open(path, encoding="utf-8", errors="replace")
        self.calls = 0

    def readline(self, n=-1):
        self.calls += 1
        return self._f.readline(n)


def test_read_page_bounds_total_scan_for_large_file(tmp_path):
    target = tmp_path / "big.log"
    n = 20_000
    target.write_text("".join(f"line-{i}\n" for i in range(n)), encoding="utf-8")

    stream = _CountingStream(target)
    page, shown, total, exact = fmod._read_page(
        stream, offset=1, limit=10, max_bytes=1 << 20, path="big.log"
    )

    assert shown == 10
    assert page.splitlines()[0] == "line-0"
    assert exact is False, "a page far above the scan budget must not claim an exact total"
    assert total >= 10
    # Bounded: ~one read per line up to the scan cap, never the whole file.
    assert stream.calls <= fmod._READ_TOTAL_SCAN_LINES + 20
    assert stream.calls < n


def test_read_page_reports_exact_total_for_small_file(tmp_path):
    target = tmp_path / "small.log"
    target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

    page, shown, total, exact = fmod._read_page(
        _CountingStream(target), offset=1, limit=10, max_bytes=1 << 20, path="small.log"
    )

    assert page == "alpha\nbeta\ngamma\n"
    assert (shown, total, exact) == (3, 3, True)


async def test_read_file_marker_reports_lower_bound_for_huge_file(tmp_path, monkeypatch):
    base = tmp_path / "ws"
    base.mkdir()
    monkeypatch.setattr(fmod, "_BASE_DIR", base.resolve())
    n = 20_000
    (base / "big.log").write_text("".join(f"line-{i}\n" for i in range(n)), encoding="utf-8")

    page = await fmod.read_file("big.log", limit=10)

    assert "showing lines 1-10 of at least" in page
    assert "offset=11" in page
    assert "line-0" in page and "line-11" not in page


async def test_read_file_exact_total_small_file_unchanged(tmp_path, monkeypatch):
    base = tmp_path / "ws"
    base.mkdir()
    monkeypatch.setattr(fmod, "_BASE_DIR", base.resolve())
    (base / "notes.txt").write_text("\n".join(f"line-{i}" for i in range(1, 11)) + "\n")

    page = await fmod.read_file("notes.txt", limit=4)
    assert "of 10;" in page
    assert "at least" not in page
    assert "offset=5" in page


# ---------------------------------------------------------------------------
# code-review #3 — one atomic-write implementation
# ---------------------------------------------------------------------------


def test_atomic_write_file_delegates_to_at(tmp_path, monkeypatch):
    target = tmp_path / "out.txt"
    calls = {}
    real = fmod._atomic_write_file_at

    def spy(dir_fd, leaf, content, **kwargs):
        calls["leaf"] = leaf
        calls["dir_fd"] = dir_fd
        return real(dir_fd, leaf, content, **kwargs)

    monkeypatch.setattr(fmod, "_atomic_write_file_at", spy)
    fmod._atomic_write_file(target, "hello")

    assert calls.get("leaf") == "out.txt"
    assert calls.get("dir_fd") is not None  # POSIX: anchored delegate
    assert target.read_text() == "hello"


def test_atomic_write_file_at_pathname_mode_preserved(tmp_path):
    """The non-dir_fd fallback branch (dir_fd=None) still writes atomically."""
    target = tmp_path / "p.txt"
    target.write_text("old")
    fmod._atomic_write_file_at(None, "p.txt", "new", parent=tmp_path)
    assert target.read_text() == "new"
    assert [p.name for p in tmp_path.iterdir()] == ["p.txt"], "temp file leaked"
