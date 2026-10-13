"""FM4 m3: delegation construction failures + artifact TOCTOU.

RED before fix:
- agent construction raising after ``_record_start`` left a pending
  ``agent_messages`` row and an orphaned child session.
- the artifact contract stat'd the path after resolution (stat-then-swap
  TOCTOU window) instead of verifying an O_NOFOLLOW-opened descriptor.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from machina.core.agent_def import AgentDef
from machina.core.models import Session
from machina.core.orchestrator import Orchestrator


async def test_agent_build_failure_leaves_no_pending_message_or_orphan(monkeypatch):
    from machina.core import orchestrator as module

    child = Session(id=uuid.uuid4(), agent_id="coder")
    record_start_calls: list = []
    archived: list = []

    async def record_start(*args, **kwargs):
        record_start_calls.append((args, kwargs))
        return uuid.uuid4()

    async def archive(session_id):
        archived.append(session_id)

    def exploding_factory(_definition):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(
        module.agent_registry, "get", AsyncMock(return_value=AgentDef(name="coder"))
    )
    monkeypatch.setattr(module.session_manager, "create", AsyncMock(return_value=child))
    monkeypatch.setattr(module.session_manager, "archive", archive)
    monkeypatch.setattr(
        "machina.core.session_mode.resolve_effective_mode", AsyncMock(return_value="ask")
    )
    orch = Orchestrator(agent_factory=exploding_factory)
    monkeypatch.setattr(orch, "_record_start", record_start)

    with pytest.raises(RuntimeError, match="provider unavailable"):
        await orch.delegate("coder", "work")

    assert record_start_calls == [], (
        "a pending agent_messages row must not be recorded before the agent builds"
    )
    assert archived == [child.id], "the failed child session must not be orphaned"


async def test_artifact_present_non_empty_ok(tmp_path, monkeypatch):
    import machina.tools.file as fmod

    base = tmp_path / "ws"
    base.mkdir()
    (base / "report.md").write_text("done")
    monkeypatch.setattr(fmod, "_base_dir", lambda: base)
    ok, reason = Orchestrator._check_expected_artifact("report.md")
    assert ok is True and reason == ""


async def test_artifact_swap_between_resolve_and_verify_is_rejected(tmp_path, monkeypatch):
    """Wave-2 TOCTOU: swap lands after resolution, before verification.

    Pre-fix the orchestrator only stat'd the resolved path (so the swap was
    invisible and the check succeeded); post-fix it must run the file
    backend's O_NOFOLLOW guarded open, which the swap defeats.
    """
    import machina.tools.file as fmod

    base = tmp_path / "ws"
    base.mkdir()
    artifact = base / "report.md"
    artifact.write_text("legit")
    outside = tmp_path / "outside.md"
    outside.write_text("attacker content")

    monkeypatch.setattr(fmod, "_base_dir", lambda: base)
    # Stage 1 (resolution) already returned the candidate; the swap lands in
    # the window between resolution and verification.
    monkeypatch.setattr(fmod, "resolve_path", lambda path: artifact)
    real_open = fmod._os_open
    state = {"swapped": False}

    def hook(path, flags, *args, **kwargs):
        if not state["swapped"]:
            state["swapped"] = True
            path.unlink()
            path.symlink_to(outside)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(fmod, "_os_open", hook)
    ok, reason = Orchestrator._check_expected_artifact("report.md")
    assert state["swapped"], "artifact verification must open the resolved path"
    assert ok is False, "a path swapped to a symlink must fail the guarded open"
    assert "report.md" in reason


async def test_artifact_missing_rejected(tmp_path, monkeypatch):
    import machina.tools.file as fmod

    base = tmp_path / "ws"
    base.mkdir()
    monkeypatch.setattr(fmod, "_base_dir", lambda: base)
    ok, reason = Orchestrator._check_expected_artifact("missing.md")
    assert ok is False and "missing.md" in reason
