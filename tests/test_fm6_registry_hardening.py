"""FM6 F11/F14 — hub publish/install guards and minimum skill-name length.

RED-first:
* publishing a disabled skill wrote ``content: null``, and installing it
  escaped as a raw ``TypeError`` from ``len(None)``;
* ``update_skill(description=...)`` skipped the 1000-char ceiling;
* 1-2 character names were accepted and substring-matched every query.
"""

from __future__ import annotations

import json

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
