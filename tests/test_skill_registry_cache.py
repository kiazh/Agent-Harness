"""Regression tests for SkillRegistry caching.

``load_all()`` is invoked on every skill tool call (``skill_list``,
``skill_read``) and by every CLI/gateway skill handler. Re-reading the whole
skills directory plus telemetry.json each time was pure waste within a turn,
so the parsed result is cached and invalidated on mutation.
"""

from __future__ import annotations

from pathlib import Path

from ah.skills.registry import SkillRegistry

SKILL_MD = """---
name: {name}
description: A test skill for caching behaviour
triggers:
  - test
version: 1.0.0
enabled: true
---

Body for {name}.
"""


def _write_skill(root: Path, name: str) -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(SKILL_MD.format(name=name), encoding="utf-8")


def _parse_count(monkeypatch) -> list[int]:
    """Count SkillParser.parse calls."""
    from ah.skills import registry as reg

    calls: list[int] = []
    original = reg.SkillParser.parse

    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(reg.SkillParser, "parse", staticmethod(counted))
    return calls


def test_load_all_is_cached(tmp_path, monkeypatch):
    """Repeated load_all() calls with no on-disk change must not re-parse."""
    _write_skill(tmp_path, "alpha")
    _write_skill(tmp_path, "beta")
    registry = SkillRegistry(skills_dir=tmp_path)
    calls = _parse_count(monkeypatch)

    registry.load_all()
    assert len(calls) == 2, "first load should parse both skills"

    registry.load_all()
    registry.load_all()
    assert len(calls) == 2, "unchanged loads must not re-parse"

    assert len(registry.list_skills()) == 2


def test_out_of_band_edit_is_still_picked_up(tmp_path, monkeypatch):
    """Reload semantics are preserved: a changed SKILL.md invalidates the cache."""
    _write_skill(tmp_path, "alpha")
    registry = SkillRegistry(skills_dir=tmp_path)
    calls = _parse_count(monkeypatch)

    registry.load_all()
    assert len(calls) == 1

    # Modify the file on disk, the way a curator or a human edit would.
    path = tmp_path / "alpha" / "SKILL.md"
    path.write_text(SKILL_MD.format(name="alpha") + "\nNew guidance.\n", encoding="utf-8")
    registry.load_all()
    assert len(calls) == 2, "a changed file must be re-parsed"
    assert "New guidance." in registry.get("alpha").content


def test_refresh_true_forces_a_reload(tmp_path, monkeypatch):
    registry = SkillRegistry(skills_dir=tmp_path)
    _write_skill(tmp_path, "alpha")
    calls = _parse_count(monkeypatch)

    registry.load_all()
    assert len(calls) == 1

    registry.load_all(refresh=True)
    assert len(calls) == 2


def test_create_skill_invalidates_cache(tmp_path, monkeypatch):
    registry = SkillRegistry(skills_dir=tmp_path)
    _write_skill(tmp_path, "alpha")
    registry.load_all()
    assert {s.name for s in registry.list_skills()} == {"alpha"}

    registry.create_skill("delta", "A newly created skill", "Step one.\nStep two.\n")

    # Without invalidation the stale cache would hide the new skill.
    registry.load_all()
    assert {s.name for s in registry.list_skills()} == {"alpha", "delta"}


def test_delete_skill_invalidates_cache(tmp_path):
    _write_skill(tmp_path, "alpha")
    _write_skill(tmp_path, "beta")
    registry = SkillRegistry(skills_dir=tmp_path)
    registry.load_all()
    assert {s.name for s in registry.list_skills()} == {"alpha", "beta"}

    assert registry.delete_skill("beta") is True

    registry.load_all()
    assert {s.name for s in registry.list_skills()} == {"alpha"}


def test_record_use_invalidates_cache(tmp_path):
    _write_skill(tmp_path, "alpha")
    registry = SkillRegistry(skills_dir=tmp_path)
    registry.load_all()

    registry.record_use("alpha")

    registry.load_all()
    assert registry.get("alpha").usage_count == 1


def test_missing_directory_loads_empty_and_is_cached(tmp_path, monkeypatch):
    registry = SkillRegistry(skills_dir=tmp_path / "does-not-exist")
    calls = _parse_count(monkeypatch)

    registry.load_all()
    registry.load_all()
    assert calls == []
    assert registry.list_skills() == []
