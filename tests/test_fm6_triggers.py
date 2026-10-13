"""FM6 F5 — trigger matching must use symmetric token boundaries.

RED-first: ``\\bc\\+\\+\\b`` failed for "use c++ now" (trailing \\b cannot sit
after '+') yet matched inside "c++builder" (\\b between '+' and a letter), the
exact opposite of whole-token semantics.
"""

from __future__ import annotations

from machina.skills.registry import SkillRegistry


def _cpp_registry(tmp_path) -> SkillRegistry:
    skills = SkillRegistry(tmp_path)
    skills.create_skill(
        "edge-modern",
        "Modern language guidance",
        "TEAM RULE: prefer modern syntax.",
        triggers=["c++"],
    )
    return skills


def test_non_word_ending_trigger_matches_as_whole_token(tmp_path):
    skills = _cpp_registry(tmp_path)
    assert [s.name for s in skills.match_triggers("use c++ now")] == ["edge-modern"]
    assert [s.name for s in skills.match_triggers("I write c++")] == ["edge-modern"]


def test_non_word_ending_trigger_does_not_match_inside_identifier(tmp_path):
    skills = _cpp_registry(tmp_path)
    assert [s.name for s in skills.match_triggers("c++builder internals")] == []
    assert [s.name for s in skills.match_triggers("xc++")] == []


def test_word_ending_trigger_boundaries_are_unchanged(tmp_path):
    skills = SkillRegistry(tmp_path)
    skills.create_skill("python-rules", "Python guidance", "Steps", triggers=["python"])
    assert [s.name for s in skills.match_triggers("python code")] == ["python-rules"]
    assert [s.name for s in skills.match_triggers("use python!")] == ["python-rules"]
    assert [s.name for s in skills.match_triggers("pythonista tooling")] == []


def test_workflow_strong_match_is_symmetric_for_cpp(tmp_path):
    from machina.skills.workflow import _strong_match

    skills = _cpp_registry(tmp_path)
    skill = skills.get("edge-modern")
    assert _strong_match(skill, "Review my c++ code") is True
    assert _strong_match(skill, "c++builder internals") is False


def test_rank_activates_cpp_trigger_skill(tmp_path):
    from machina.skills.runtime import rank_skills

    skills = _cpp_registry(tmp_path)
    assert [s.name for s in rank_skills(skills, "use c++ now")] == ["edge-modern"]
    assert [s.name for s in rank_skills(skills, "c++builder internals")] == []


def test_workflow_selects_skill_by_non_word_ending_trigger(tmp_path, monkeypatch):
    from machina.skills.workflow import plan_workflow

    monkeypatch.setenv("MACHINA_SKILLS_DIR", str(tmp_path))
    skills = _cpp_registry(tmp_path)
    plan = plan_workflow("Review my c++ code", skills=skills)
    assert "edge-modern" in {s.name for s in plan.skills}
