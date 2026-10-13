"""FM6 F9 — activation must fsync the skills directory after the rename."""

from __future__ import annotations

import json
import subprocess

import pytest

from machina.skills import repo_install
from machina.skills.repo_install import (
    RepoInstallError,
    activate_plan,
    load_staged_plan,
    parse_repo_url,
    scan_repo,
    write_staged_plan,
)

ALPHA_SKILL = """---
name: alpha
description: Alpha skill for fixtures
triggers: [alpha]
---
Help with alpha tasks.
"""


def _make_repo(root, files):
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        cwd=root,
        check=True,
    )
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()


def _stage(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    revision = _make_repo(root, {"skills/alpha/SKILL.md": ALPHA_SKILL})
    staging = tmp_path / "staging"
    monkeypatch.setattr(repo_install, "staging_root", lambda: staging)
    plan = scan_repo(root, parse_repo_url("https://github.com/o/r"), revision)
    write_staged_plan(plan, root)
    detail, found = load_staged_plan(plan.digest)
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    return detail, found, skills_dir


def test_activation_fsyncs_skills_dir_after_replace(tmp_path, monkeypatch):
    detail, staged, skills_dir = _stage(tmp_path, monkeypatch)
    synced: list = []
    real_fsync_dir = repo_install._fsync_dir

    def record(path, *, best_effort=False):
        synced.append((path, best_effort))
        return real_fsync_dir(path, best_effort=best_effort)

    monkeypatch.setattr(repo_install, "_fsync_dir", record)
    assert activate_plan(detail, staged, skills_dir) == ["alpha"]
    assert (skills_dir, False) in synced
    assert (skills_dir / "alpha" / ".install-manifest.json").is_file()


def test_skills_dir_fsync_failure_rolls_back(tmp_path, monkeypatch):
    detail, staged, skills_dir = _stage(tmp_path, monkeypatch)
    real_fsync_dir = repo_install._fsync_dir

    def failing(path, *, best_effort=False):
        if path == skills_dir:
            raise OSError("directory sync failed")
        return real_fsync_dir(path, best_effort=best_effort)

    monkeypatch.setattr(repo_install, "_fsync_dir", failing)
    with pytest.raises(RepoInstallError):
        activate_plan(detail, staged, skills_dir)
    assert not (skills_dir / "alpha").exists()
    # Staging is intact, so a retry still succeeds.
    monkeypatch.setattr(repo_install, "_fsync_dir", real_fsync_dir)
    assert activate_plan(detail, staged, skills_dir) == ["alpha"]
    manifest = json.loads((skills_dir / "alpha" / ".install-manifest.json").read_text())
    assert manifest["revision"] == detail["revision"]
