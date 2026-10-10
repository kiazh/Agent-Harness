"""Only administrator-listed scripts run; dependency changes invalidate review."""

import json
from unittest.mock import AsyncMock

import pytest

from ah.core import job_scripts


def policy(root, scripts):
    (root / ".script-policy.json").write_text(
        json.dumps({"version": 1, "scripts": scripts}), encoding="utf-8"
    )


async def test_script_without_administrator_allowlist_is_refused(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_HARNESS_SCRIPTS_DIR", str(tmp_path))
    target = tmp_path / "entry.py"
    target.write_text("print('unlisted')", encoding="utf-8")
    with pytest.raises(ValueError, match="allowlist"):
        await job_scripts.run_job_script("entry.py")


async def test_unlisted_script_cannot_execute(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_HARNESS_SCRIPTS_DIR", str(tmp_path))
    (tmp_path / "entry.py").write_text("print('unlisted')", encoding="utf-8")
    policy(tmp_path, {"other.py": []})
    with pytest.raises(ValueError, match="allowlist"):
        await job_scripts.run_job_script("entry.py")


async def test_changed_imported_dependency_fails_before_execution(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_HARNESS_SCRIPTS_DIR", str(tmp_path))
    (tmp_path / "entry.py").write_text("import helper\nprint(helper.VALUE)", encoding="utf-8")
    dependency = tmp_path / "helper.py"
    dependency.write_text("VALUE = 'reviewed'", encoding="utf-8")
    policy(tmp_path, {"entry.py": ["helper.py"]})
    reviewed = job_scripts.review_script("entry.py")
    dependency.write_text("VALUE = 'changed'", encoding="utf-8")
    with pytest.raises(ValueError, match="dependencies changed"):
        await job_scripts.run_job_script(
            "entry.py",
            approved_content=reviewed.content,
            approved_path=reviewed.target,
            approved_review=reviewed,
        )


async def test_dependency_mutation_during_approval_cannot_execute(monkeypatch, tmp_path):
    from ah.core import turns
    from ah.core.scheduler import JobRunner
    from ah.permissions import store
    from ah.permissions.broker import clear_execution_context, set_approval_handler
    from tests.test_deep_scheduler import _job

    monkeypatch.setenv("AGENT_HARNESS_SCRIPTS_DIR", str(tmp_path))
    (tmp_path / "job.py").write_text(
        "import helper\nfrom pathlib import Path\nPath('effect.txt').write_text(helper.VALUE)",
        encoding="utf-8",
    )
    dependency = tmp_path / "helper.py"
    dependency.write_text("VALUE = 'reviewed'", encoding="utf-8")
    policy(tmp_path, {"job.py": ["helper.py"]})
    monkeypatch.setattr(turns, "try_begin_turn", AsyncMock(return_value="script-owner"))
    monkeypatch.setattr(turns, "end_turn", AsyncMock(return_value=True))
    request_ids = []

    async def approve(card):
        request_ids.append(card["request_id"])
        await store.resolve_decision(card["request_id"], "approved", principal="tui")
        dependency.write_text("VALUE = 'unreviewed'", encoding="utf-8")
        return "approved"

    set_approval_handler(approve)
    try:
        with pytest.raises(ValueError, match="dependencies changed"):
            await JobRunner()._execute(_job(no_agent=True))
        assert not (tmp_path / "effect.txt").exists()
        assert (await store.get_approval(request_ids[0]))["status"] == "failed"
    finally:
        set_approval_handler(None)
        clear_execution_context()
        for rid in request_ids:
            store._mem.approvals.pop(rid, None)


@pytest.mark.parametrize("dependency", ["../outside.py", "/absolute.py", ".env"])
def test_dependency_manifest_rejects_escape_and_private_files(monkeypatch, tmp_path, dependency):
    monkeypatch.setenv("AGENT_HARNESS_SCRIPTS_DIR", str(tmp_path))
    (tmp_path / "entry.py").write_text("pass", encoding="utf-8")
    policy(tmp_path, {"entry.py": [dependency]})
    with pytest.raises(ValueError):
        job_scripts.review_script("entry.py")
