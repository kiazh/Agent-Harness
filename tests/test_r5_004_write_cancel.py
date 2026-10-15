"""R5 finding 4 (HELD-44) — cancelled file writes must not clobber later edits.

RED (pre-fix, evidence ``docs/verification/proofs/codebase_review_round5_2026-10-15/HELD-44_write_cancel.md``):
``write_file`` ran its worker through ``asyncio.to_thread``; cancelling the
await (``asyncio.wait_for`` hard timeout, or turn cancellation) never stopped
the thread. The old turn's final ``os.replace`` therefore landed *after* a
newer edit and after cancellation had been reported.

GREEN: the worker future is created explicitly and shielded, and cancellation
is joined to the worker's completion before ``CancelledError`` propagates, so
execution ownership is held until the filesystem effects finish.

The gate below does not mock writer logic: it only delays the first real
atomic-write call inside the worker thread; temp creation and replacement stay
real syscalls.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

import machina.tools.file as fmod
from machina.tools.base import registry

_RELEASE_DELAY = 0.3


def _prepare(tmp_path: Path, monkeypatch) -> Path:
    base = tmp_path / "ws"
    base.mkdir()
    target = base / "victim.txt"
    target.write_text("ORIGINAL")
    monkeypatch.setattr(fmod, "_BASE_DIR", base.resolve())
    return target


class _Gate:
    """Park the first real atomic write inside its worker thread."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.calls = 0

    def install(self, monkeypatch) -> _Gate:
        real = fmod._atomic_write_file_at

        def gated(dir_fd, leaf, content, **kwargs):
            self.calls += 1
            if self.calls == 1:
                self.entered.set()
                self.release.wait(timeout=5)
            result = real(dir_fd, leaf, content, **kwargs)
            self.finished.set()
            return result

        monkeypatch.setattr(fmod, "_atomic_write_file_at", gated)
        return self

    async def wait_entered(self) -> None:
        assert await asyncio.to_thread(self.entered.wait, 5), "worker never reached the gate"

    def arm_release(self, delay: float = _RELEASE_DELAY) -> threading.Timer:
        timer = threading.Timer(delay, self.release.set)
        timer.daemon = True
        timer.start()
        return timer


@pytest.mark.asyncio
async def test_cancelled_write_joins_worker_before_releasing_ownership(tmp_path, monkeypatch):
    target = _prepare(tmp_path, monkeypatch)
    gate = _Gate().install(monkeypatch)
    task = asyncio.create_task(fmod.write_file("victim.txt", "OLD"))
    try:
        await gate.wait_entered()
        timer = gate.arm_release()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        timer.join(timeout=5)
        # Ownership was retained: the worker's replace completed before the
        # cancellation returned to the caller.
        assert gate.finished.is_set()
        await fmod.write_file("victim.txt", "NEW")
        await asyncio.sleep(0.3)
        assert target.read_text() == "NEW"
    finally:
        gate.release.set()


@pytest.mark.asyncio
async def test_tool_timeout_joins_worker_and_preserves_newer_edit(tmp_path, monkeypatch):
    """agent.py:1033 pattern — ``asyncio.wait_for(registry.execute(...))``."""
    target = _prepare(tmp_path, monkeypatch)
    gate = _Gate().install(monkeypatch)

    async def call_old():
        return await asyncio.wait_for(
            registry.execute("write_file", path="victim.txt", content="OLD"),
            timeout=0.2,
        )

    task = asyncio.create_task(call_old())
    try:
        await gate.wait_entered()
        timer = gate.arm_release(0.35)
        with pytest.raises(asyncio.TimeoutError):
            await task
        timer.join(timeout=5)
        # The hard timeout did not detach the write; the worker finished first.
        assert gate.finished.is_set()
        await registry.execute("write_file", path="victim.txt", content="NEW")
        await asyncio.sleep(0.3)
        assert target.read_text() == "NEW"
    finally:
        gate.release.set()


@pytest.mark.asyncio
async def test_turn_cancel_gather_waits_for_worker(tmp_path, monkeypatch):
    """agent.py:1181-1188 pattern — cancel the tool tasks, then join them."""
    target = _prepare(tmp_path, monkeypatch)
    gate = _Gate().install(monkeypatch)
    tasks = [asyncio.create_task(registry.execute("write_file", path="victim.txt", content="OLD"))]
    try:
        await gate.wait_entered()
        timer = gate.arm_release()
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        timer.join(timeout=5)
        assert gate.finished.is_set()
        await registry.execute("write_file", path="victim.txt", content="NEW")
        await asyncio.sleep(0.3)
        assert target.read_text() == "NEW"
    finally:
        gate.release.set()


@pytest.mark.asyncio
async def test_cancellation_join_is_bounded_by_worker(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch)
    gate = _Gate().install(monkeypatch)
    task = asyncio.create_task(fmod.write_file("victim.txt", "OLD"))
    loop = asyncio.get_running_loop()
    try:
        await gate.wait_entered()
        timer = gate.arm_release(0.2)
        started = loop.time()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        elapsed = loop.time() - started
        timer.join(timeout=5)
        assert gate.finished.is_set()
        # Joined the worker, but did not hang: bounded by the worker's own
        # completion rather than an arbitrary wait.
        assert elapsed < 5.0, f"cancellation join not bounded: {elapsed:.2f}s"
    finally:
        gate.release.set()


@pytest.mark.asyncio
async def test_worker_failure_during_cancellation_still_reports_cancellation(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch)
    entered = threading.Event()
    release = threading.Event()

    def boom(dir_fd, leaf, content, **kwargs):
        entered.set()
        release.wait(timeout=5)
        raise ValueError("injected worker failure")

    monkeypatch.setattr(fmod, "_atomic_write_file_at", boom)
    task = asyncio.create_task(fmod.write_file("victim.txt", "OLD"))
    assert await asyncio.to_thread(entered.wait, 5)
    timer = threading.Timer(0.2, release.set)
    timer.daemon = True
    timer.start()
    try:
        task.cancel()
        # The worker's own failure must not replace the cancellation outcome,
        # and must not prevent the join from completing.
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        timer.join(timeout=5)


@pytest.mark.asyncio
async def test_normal_write_unaffected(tmp_path, monkeypatch):
    target = _prepare(tmp_path, monkeypatch)
    result = await fmod.write_file("victim.txt", "hello")
    assert "Successfully wrote" in result
    assert target.read_text() == "hello"
