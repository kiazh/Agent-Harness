"""FM6 F2 — skill telemetry must merge concurrent instance updates and write atomically.

RED-first: two registries on the same skills dir used to overwrite each
other's increments, and the telemetry file was written in place (a crash
could truncate it).
"""

from __future__ import annotations

from machina.skills.registry import SkillRegistry


def _write_skill(registry: SkillRegistry, name: str = "deploy") -> None:
    registry.create_skill(name, "Deploy steps", "Steps to deploy safely", triggers=[name])


def test_two_instances_merge_usage_and_view_increments(tmp_path):
    first = SkillRegistry(tmp_path)
    _write_skill(first)
    second = SkillRegistry(tmp_path)

    first.load_all()
    second.load_all()
    first.record_use("deploy")
    second.record_use("deploy")
    first.record_view("deploy")
    second.record_view("deploy")

    fresh = SkillRegistry(tmp_path)
    fresh.load_all()
    skill = fresh.get("deploy")
    assert skill.usage_count == 2
    assert skill.view_count == 2


def test_two_instances_merge_with_pre_existing_telemetry(tmp_path):
    seed = SkillRegistry(tmp_path)
    _write_skill(seed)
    seed.record_use("deploy")
    seed.record_use("deploy")

    first = SkillRegistry(tmp_path)
    second = SkillRegistry(tmp_path)
    first.load_all()
    second.load_all()
    first.record_use("deploy")
    second.record_use("deploy")

    fresh = SkillRegistry(tmp_path)
    fresh.load_all()
    assert fresh.get("deploy").usage_count == 4


def test_telemetry_write_is_atomic(tmp_path, monkeypatch):
    """A crash during persistence must leave the previous file intact."""
    import machina.skills.registry as registry_module

    registry = SkillRegistry(tmp_path)
    _write_skill(registry)
    registry.record_use("deploy")
    previous = (tmp_path / "telemetry.json").read_text(encoding="utf-8")
    assert '"usage_count": 1' in previous

    def exploding_replace(src, dst):
        raise OSError("simulated crash during replace")

    monkeypatch.setattr(registry_module.os, "replace", exploding_replace)
    registry.record_use("deploy")  # must not truncate the live file

    assert (tmp_path / "telemetry.json").read_text(encoding="utf-8") == previous
    # The pending increment is not lost: after the failure is repaired a fresh
    # registration still sees both uses (merge-on-write, no in-place truncation).
    monkeypatch.undo()
    registry.record_view("deploy")
    fresh = SkillRegistry(tmp_path)
    fresh.load_all()
    assert fresh.get("deploy").usage_count == 2
