"""Regression tests for bounded terminal tool output."""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from machina.tools import terminal as terminal_module


@pytest.mark.asyncio
async def test_terminal_stops_capturing_after_output_limit(monkeypatch):
    monkeypatch.setenv("MACHINA_TERMINAL_SANDBOX", "local")
    monkeypatch.setattr(terminal_module, "MAX_OUTPUT_CHARS", 8, raising=False)

    output = await terminal_module.terminal("git --version")

    assert output.startswith("git vers")
    assert "[output truncated]" in output
    assert len(output) < 80


def test_output_limit_stops_a_child_that_never_finishes(monkeypatch):
    monkeypatch.setattr(terminal_module, "MAX_OUTPUT_CHARS", 512)

    output = terminal_module._run_bounded(
        [sys.executable, "-c", "while True: print('x' * 1000, flush=True)"],
        timeout=3,
        cwd=None,
    )

    assert output.startswith("x" * 512)
    assert output.endswith("[output truncated]")


def test_bounded_runner_preserves_timeout():
    with pytest.raises(subprocess.TimeoutExpired):
        terminal_module._run_bounded(
            [sys.executable, "-c", "import time; time.sleep(3)"],
            timeout=1,
            cwd=None,
        )


# 2026-10-14 review R07: an exited launcher must not orphan descendants.
@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group semantics")
def test_deadline_kills_descendant_after_launcher_exit(tmp_path):
    """The documented repro: the launcher exits immediately, the background
    child keeps running past the deadline and used to write late.txt after
    the tool had already returned success."""
    marker = tmp_path / "late.txt"
    command = "(sleep 2; exec >/dev/null 2>&1; sleep 2; echo survived > late.txt) &"
    with pytest.raises(subprocess.TimeoutExpired):
        terminal_module._run_bounded(["/bin/sh", "-c", command], timeout=1, cwd=str(tmp_path))
    time.sleep(2.5)
    assert not marker.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group semantics")
def test_deadline_kills_descendant_that_closed_stdout(tmp_path):
    """The pipe can close while descendants keep running; completion is
    defined by the process group, not by EOF on the launcher's pipe."""
    marker = tmp_path / "late.txt"
    command = "(exec >/dev/null 2>&1; sleep 4; echo survived > late.txt) &"
    with pytest.raises(subprocess.TimeoutExpired):
        terminal_module._run_bounded(["/bin/sh", "-c", command], timeout=1, cwd=str(tmp_path))
    time.sleep(2.0)
    assert not marker.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group semantics")
def test_fast_background_child_completes_without_false_timeout(tmp_path):
    """Short-lived background work drains before return; no spurious timeout
    and no effect landing after the call."""
    output = terminal_module._run_bounded(
        ["/bin/sh", "-c", "echo bg > done.txt &"],
        timeout=5,
        cwd=str(tmp_path),
    )
    assert not output.is_error
    time.sleep(0.3)
    assert (tmp_path / "done.txt").read_text(encoding="utf-8").strip() == "bg"
