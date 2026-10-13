"""FM6 F4 — continuation offsets must survive token cuts that split a codepoint.

RED-first: ``automatic_skill_context`` derived the continuation offset from
``len(truncate_to_tokens(content, budget))``. When tiktoken cuts inside an
emoji the decoded excerpt ends with U+FFFD, which is not a real prefix of the
content, so the printed offset dropped a character on resume.
"""

from __future__ import annotations

import re

import pytest

from machina.skills.registry import SkillRegistry


def _registry_with_emoji_body(tmp_path) -> tuple[SkillRegistry, str]:
    skills = SkillRegistry(tmp_path)
    content = "Intro line.\n\n" + "😀" * 60 + "\nTAIL MARKER\n" + "z" * 2000
    skills.create_skill("deploy", "Deploy steps safely", content, triggers=["deploy"])
    return skills, content


@pytest.mark.parametrize("budget", list(range(160, 1201, 7)))
def test_excerpt_plus_continuation_reconstructs_content(budget, tmp_path, monkeypatch):
    import machina.skills.registry as skills_module
    from machina.skills.runtime import automatic_skill_context
    from machina.tools.skills import skill_read

    skills, content = _registry_with_emoji_body(tmp_path)
    monkeypatch.setattr(skills_module, "skill_registry", skills)

    text = automatic_skill_context("help me deploy", skills=skills, budget=budget)
    if "offset=" not in text:
        return
    match = re.search(r"continue skill_read at offset=(\d+)", text)
    assert match is not None
    offset = int(match.group(1))
    excerpt = text.split("### deploy\n", 1)[1].split("\n[Instruction excerpt ends", 1)[0]

    read = skill_read("deploy", offset=offset, limit=8000)
    body = read.split("]:\n", 1)[1]
    # The served excerpt and the continuation must tile the content exactly.
    assert excerpt + body == content
    assert excerpt == content[:offset]
