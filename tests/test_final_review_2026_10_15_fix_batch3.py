"""Final-review (2026-10-15) BATCH-3 fixes — evidence suite.

Findings closed in this lane:

* T5 / code_review #14 (P3, cancellation consistency): skill-install write
  stages in ``machina/tools/repo.py`` ran through bare ``asyncio.to_thread``,
  so a cancelled install could detach a worker that then published after turn
  ownership was released. Fixed by routing the clone / staged-plan / install
  (and the clone cleanup) through the shared ``_run_worker_joined`` join-on-
  cancel helper (same HELD-44 semantics used by ``tools/file.py``).
* A3 / architecture_review P3-1 (P3): ``AuditPersistence`` gains an explicit
  ``reset()`` that normalizes to a clean generation — including
  ``_ever_started`` — and logs when it discards live state. ``tests/conftest.py``
  now calls ``reset()`` instead of assigning the private
  ``_owners``/``_task``/``_queue`` fields.

The companion P11 / code_review #13 rewrite (source-text SSE assertions →
behavioral provider drives) lives in
``tests/test_r5_011_013_provider_transport.py``.
"""

from __future__ import annotations

import asyncio
import logging
import threading

import pytest

from machina.core.metrics import metrics
from machina.observability.audit import AuditPersistence

_JOIN_RELEASE_DELAY = 0.2


# ---------------------------------------------------------------------------
# A3 — AuditPersistence.reset()
# ---------------------------------------------------------------------------


def test_audit_reset_normalizes_generation_and_ever_started():
    """A leaked generation normalizes to a pristine, unstarted writer."""
    writer = AuditPersistence()
    writer._owners = 3
    writer._task = object()
    writer._queue = object()
    writer._ever_started = True

    writer.reset()

    assert writer._owners == 0
    assert writer._task is None
    assert writer._queue is None
    assert writer._ever_started is False


def test_audit_reset_logs_when_it_discards_state(caplog):
    """Discarding live state is loud, not silent (a leak must stay visible)."""
    writer = AuditPersistence()
    writer._owners = 1
    with caplog.at_level(logging.WARNING, logger="machina.observability.audit"):
        writer.reset()
    assert any(
        record.levelno == logging.WARNING and "reset()" in record.getMessage()
        for record in caplog.records
    ), [record.getMessage() for record in caplog.records]


def test_audit_reset_is_silent_on_a_pristine_writer(caplog):
    """Resetting an already-clean writer is a no-op: no spurious warning."""
    writer = AuditPersistence()
    with caplog.at_level(logging.WARNING, logger="machina.observability.audit"):
        writer.reset()
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]


def test_audit_reset_restores_before_start_classification(caplog):
    """After reset a pre-start submit is 'before-start', not 'shutdown'.

    Pins the ``_ever_started`` normalization the architecture review asked for
    (the old conftest left the flag set, so a later pre-start drop was counted
    as a shutdown drop).
    """
    writer = AuditPersistence()
    writer._ever_started = True  # simulate the leaked prior generation
    writer.reset()

    before = metrics.get_counter("audit.queue.dropped_before_start")
    with caplog.at_level(logging.ERROR, logger="machina.observability.audit"):
        writer.submit({"event": "lost-before-start"})
    assert metrics.get_counter("audit.queue.dropped_before_start") == before + 1


@pytest.mark.asyncio
async def test_audit_reset_allows_a_clean_restart(monkeypatch):
    """A writer started after reset persists normally (behavior preserved)."""
    from machina.observability import audit as audit_mod

    persisted = []

    async def execute(_sql, event, _payload):
        persisted.append(event)

    monkeypatch.setattr(audit_mod.db, "execute", execute)

    writer = AuditPersistence()
    writer.start()
    leaked = writer._task
    writer.reset()
    leaked.cancel()
    await asyncio.gather(leaked, return_exceptions=True)

    writer.start()
    writer.submit({"event": "after-reset"})
    await writer.stop(timeout=1.0)
    assert persisted == ["after-reset"]


# ---------------------------------------------------------------------------
# T5 — repo install write stages join their worker on cancellation
# ---------------------------------------------------------------------------


class _FakePlan:
    """Minimal scan plan for the repo_stage join probe."""

    digest = "d" * 64

    def summary(self):
        return {
            "skills": [],
            "invalid_skills": [],
            "mcp_drafts": [],
            "agents": [],
            "commands": [],
            "plugins": [],
            "never_run": [],
        }


@pytest.mark.asyncio
async def test_repo_stage_joins_staged_plan_writer_on_cancel(tmp_path, monkeypatch):
    """Cancelling repo_stage waits for the staged-plan writer's effects."""
    import machina.tools.repo as repo_tools

    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    clone_parent = tmp_path / "clone_parent"
    repo_dir = clone_parent / "repo"
    repo_dir.mkdir(parents=True)

    monkeypatch.setattr(repo_tools.repo_install, "clone_repo", lambda source: (repo_dir, "rev1"))
    monkeypatch.setattr(repo_tools.repo_install, "scan_repo", lambda *args, **kwargs: _FakePlan())

    def gated_write(plan, root):
        entered.set()
        release.wait(timeout=5)
        finished.set()
        return root

    monkeypatch.setattr(repo_tools.repo_install, "write_staged_plan", gated_write)

    task = asyncio.create_task(repo_tools.repo_stage("https://github.com/o/r"))
    assert await asyncio.to_thread(entered.wait, 5), "writer never entered"
    timer = threading.Timer(_JOIN_RELEASE_DELAY, release.set)
    timer.daemon = True
    timer.start()
    try:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        timer.join(timeout=5)
    assert finished.is_set(), "cancellation detached the staged-plan writer"


@pytest.mark.asyncio
async def test_repo_activate_joins_install_worker_on_cancel(monkeypatch):
    """Cancelling repo_activate waits for the install worker's effects."""
    import machina.tools.repo as repo_tools

    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    monkeypatch.setattr(repo_tools.repo_install, "load_staged_plan", lambda digest: ({}, {}))
    monkeypatch.setattr(repo_tools, "skills_dir_for_install", lambda: None)

    def gated_activate(detail, staged, skills_dir):
        entered.set()
        release.wait(timeout=5)
        finished.set()
        return ["alpha"]

    monkeypatch.setattr(repo_tools.repo_install, "activate_plan", gated_activate)

    task = asyncio.create_task(repo_tools.repo_activate("d" * 64))
    assert await asyncio.to_thread(entered.wait, 5), "install worker never entered"
    timer = threading.Timer(_JOIN_RELEASE_DELAY, release.set)
    timer.daemon = True
    timer.start()
    try:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        timer.join(timeout=5)
    assert finished.is_set(), "cancellation detached the install worker"
