"""Regression tests for bounded terminal tool output."""

from __future__ import annotations

import subprocess
import sys

import pytest

from ah.tools import terminal as terminal_module


@pytest.mark.asyncio
async def test_terminal_stops_capturing_after_output_limit(monkeypatch):
    monkeypatch.setenv("AGENT_HARNESS_TERMINAL_SANDBOX", "local")
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
