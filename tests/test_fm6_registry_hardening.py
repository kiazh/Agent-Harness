"""FM6 F11/F14 — hub publish/install guards and minimum skill-name length.

RED-first:
* publishing a disabled skill wrote ``content: null``, and installing it
  escaped as a raw ``TypeError`` from ``len(None)``;
* ``update_skill(description=...)`` skipped the 1000-char ceiling;
* 1-2 character names were accepted and substring-matched every query.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from machina.skills.registry import SkillHub, SkillParser, SkillRegistry


def test_publish_disabled_skill_is_refused(tmp_path):
    registry = SkillRegistry(tmp_path / "skills")
    registry.create_skill("deploy", "Deploy steps", "Steps to deploy safely")
    registry.set_enabled("deploy", False)
    hub = SkillHub(registry, tmp_path / "hub")
    with pytest.raises(ValueError):
        hub.publish("deploy")
    assert not list((tmp_path / "hub").glob("*.json"))


def test_install_null_content_raises_value_error_not_type_error(tmp_path):
    registry = SkillRegistry(tmp_path / "skills")
    hub_dir = tmp_path / "hub"
    hub = SkillHub(registry, hub_dir)
    (hub_dir / "broken.json").write_text(
        json.dumps(
            {
                "name": "broken",
                "description": "Broken entry",
                "triggers": [],
                "content": None,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        hub.install("broken")


def test_update_skill_enforces_description_ceiling(tmp_path):
    registry = SkillRegistry(tmp_path)
    registry.create_skill("deploy", "Deploy steps", "Steps to deploy safely")
    with pytest.raises(ValueError):
        registry.update_skill("deploy", description="x" * 1001)
    # The boundary itself stays legal.
    registry.update_skill("deploy", description="y" * 1000)
    assert registry.get("deploy").description == "y" * 1000


def test_skill_names_shorter_than_three_chars_are_rejected(tmp_path):
    registry = SkillRegistry(tmp_path)
    for short in ("a", "ab", "x-"):
        with pytest.raises(ValueError, match="name"):
            registry.create_skill(short, "Short name", "Steps")
    with pytest.raises(ValueError, match="name"):
        SkillParser._validate_metadata("ab", "Short name", [])
    assert registry.create_skill("abc", "Three chars", "Steps").name == "abc"


def test_short_name_in_skill_file_is_rejected(tmp_path):
    bad_dir = tmp_path / "bad"
    bad_dir.mkdir()
    bad_file = bad_dir / "SKILL.md"
    bad_file.write_text(
        "---\nname: ab\ndescription: Short\ntriggers: [ab]\n---\nSteps", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="name"):
        SkillParser.parse(bad_file)


# ─── R4-091: corrupt hub entries raise typed ValueError, never Key/TypeError ──


def test_install_hub_entry_without_name_raises_value_error_not_key_error(tmp_path):
    registry = SkillRegistry(tmp_path / "skills")
    hub = SkillHub(registry, tmp_path / "hub")
    (tmp_path / "hub" / "nameless.json").write_text(
        json.dumps({"description": "Broken entry", "content": "Steps"}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="name"):
        hub.install("nameless")


def test_install_hub_entry_with_nonstring_name_raises_value_error_not_type_error(tmp_path):
    registry = SkillRegistry(tmp_path / "skills")
    hub = SkillHub(registry, tmp_path / "hub")
    (tmp_path / "hub" / "fieldless.json").write_text(
        json.dumps({"name": 5, "description": "Broken entry", "content": "Steps"}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="name"):
        hub.install("fieldless")


def test_list_hub_skips_json_that_is_not_an_entry_object(tmp_path):
    registry = SkillRegistry(tmp_path / "skills")
    hub = SkillHub(registry, tmp_path / "hub")
    (tmp_path / "hub" / "array.json").write_text("[1, 2, 3]", encoding="utf-8")
    (tmp_path / "hub" / "good.json").write_text(
        json.dumps(
            {
                "name": "good",
                "description": "Fine entry",
                "triggers": [],
                "content": "Steps",
            }
        ),
        encoding="utf-8",
    )
    assert [entry["name"] for entry in hub.list_hub()] == ["good"]


def test_search_skips_type_corrupt_hub_entries(tmp_path):
    registry = SkillRegistry(tmp_path / "skills")
    hub = SkillHub(registry, tmp_path / "hub")
    (tmp_path / "hub" / "fieldy.json").write_text(
        json.dumps({"name": 3, "description": ["no"], "tags": {"x": 1}}),
        encoding="utf-8",
    )
    (tmp_path / "hub" / "bad-tags.json").write_text(
        json.dumps({"name": "bad-tags", "description": "Bad tags", "tags": 0}),
        encoding="utf-8",
    )
    (tmp_path / "hub" / "good.json").write_text(
        json.dumps(
            {
                "name": "good",
                "description": "Fine entry",
                "tags": ["demo"],
                "content": "Steps",
            }
        ),
        encoding="utf-8",
    )
    assert [entry["name"] for entry in hub.list_hub()] == ["good"]
    assert [entry["name"] for entry in hub.search("good")] == ["good"]


# ─── R4-091: the 1 MB parse cap is a pre-stat guard, not a post-read check ───


def test_parser_checks_file_size_before_reading_a_symlinked_huge_file(tmp_path, monkeypatch):
    target = tmp_path / "huge.md"
    with target.open("wb") as handle:
        handle.truncate(SkillParser.MAX_CONTENT_SIZE + 1)
    skill_dir = tmp_path / "huge-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").symlink_to(target)
    reads = []
    real_read_text = Path.read_text

    def spy(path, *args, **kwargs):
        reads.append(path)
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", spy)
    with pytest.raises(ValueError, match="maximum size"):
        SkillParser.parse(skill_dir / "SKILL.md")
    assert reads == []
