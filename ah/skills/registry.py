"""Skill system — registry, SKILL.md parser, trigger matching."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


@dataclass
class Skill:
    name: str
    description: str
    triggers: list[str]
    content: str
    file_path: str
    version: str = "1.0.0"
    enabled: bool = True
    usage_count: int = 0


class SkillParser:
    """Parse SKILL.md files with YAML frontmatter."""

    @staticmethod
    def parse(file_path: str | Path) -> Skill:
        """Parse a SKILL.md file."""
        path = Path(file_path)
        text = path.read_text(encoding="utf-8")

        # Extract YAML frontmatter
        frontmatter_match = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
        if not frontmatter_match:
            return Skill(
                name=path.parent.name,
                description="",
                triggers=[],
                content=text,
                file_path=str(path),
            )

        yaml_text = frontmatter_match.group(1)
        content = frontmatter_match.group(2).strip()

        # Parse YAML frontmatter with PyYAML
        try:
            metadata: dict[str, Any] = yaml.safe_load(yaml_text) or {}
        except yaml.YAMLError as exc:
            # Graceful fallback: log and use defaults
            import logging

            logging.getLogger(__name__).warning(
                "Failed to parse YAML frontmatter in %s: %s", path, exc
            )
            metadata = {}

        return Skill(
            name=metadata.get("name", path.parent.name),
            description=metadata.get("description", ""),
            triggers=metadata.get("triggers", []),
            content=content,
            file_path=str(path),
            version=metadata.get("version", "1.0.0"),
        )


class SkillRegistry:
    """Load, match, and manage skills."""

    def __init__(self, skills_dir: str | Path = "skills") -> None:
        self.skills_dir = Path(skills_dir)
        self._skills: dict[str, Skill] = {}

    def load_all(self) -> None:
        """Load all skills from the skills directory."""
        if not self.skills_dir.exists():
            return
        for skill_dir in self.skills_dir.iterdir():
            if skill_dir.is_dir():
                skill_file = skill_dir / "SKILL.md"
                if skill_file.exists():
                    skill = SkillParser.parse(skill_file)
                    self._skills[skill.name] = skill

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def list_skills(self) -> list[Skill]:
        return list(self._skills.values())

    def match_triggers(self, query: str) -> list[Skill]:
        """Match query against skill triggers (simple keyword matching)."""
        query_lower = query.lower()
        matched = []
        for skill in self._skills.values():
            for trigger in skill.triggers:
                if trigger.lower() in query_lower:
                    matched.append(skill)
                    break
        return matched

    def get_skill_content(self, name: str) -> str | None:
        """Get the full content of a skill (for injection into prompt)."""
        skill = self._skills.get(name)
        return skill.content if skill else None

    @classmethod
    def reset(cls) -> None:
        """Reset the global SkillRegistry singleton to a fresh instance."""
        global skill_registry
        skill_registry = cls()


# Global registry
skill_registry = SkillRegistry()
