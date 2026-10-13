"""FM6 F6 — research plans must not collapse the anchor under the "domain" label.

RED-first: with ``ecc-deep-research`` as the top match, the ``strong[0]``
domain fallback consumed it before the research/verify stages ran, so the
research anchor appeared as "domain" and its proper stage labels vanished.
"""

from __future__ import annotations

from machina.skills.registry import SkillRegistry


def test_research_anchor_keeps_its_stage_label(tmp_path, monkeypatch):
    from machina.skills.workflow import plan_workflow

    monkeypatch.setenv("MACHINA_SKILLS_DIR", str(tmp_path))
    plan = plan_workflow(
        "research the literature on transformer architectures", skills=SkillRegistry()
    )
    assert plan.kind == "research"
    assert ("domain", "ecc-deep-research") not in plan.stages
    assert ("research", "ecc-deep-research") in plan.stages or (
        "verify sources",
        "ecc-deep-research",
    ) in plan.stages
    assert "ecc-deep-research" in {s.name for s in plan.skills}


def test_domain_fallback_still_applies_to_non_anchor_top_match(tmp_path, monkeypatch):
    from machina.skills.workflow import plan_workflow

    monkeypatch.setenv("MACHINA_SKILLS_DIR", str(tmp_path))
    plan = plan_workflow("investigate the literature on retrieval methods", skills=SkillRegistry())
    assert plan.kind == "research"
    assert ("domain", "ecc-iterative-retrieval") in plan.stages
