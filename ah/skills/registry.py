"""Skill system — registry, SKILL.md parser, trigger matching, telemetry, curator, hub."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# Defaults are anchored to the project root, not the current directory, so the
# same skills are found no matter where `ah` is launched. Override with env vars.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def default_skills_dir() -> Path:
    return Path(os.environ.get("AGENT_HARNESS_SKILLS_DIR") or _PROJECT_ROOT / "skills")


def default_hub_dir() -> Path:
    return Path(os.environ.get("AGENT_HARNESS_SKILL_HUB_DIR") or _PROJECT_ROOT / ".skill-hub")


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
    view_count: int = 0
    last_activity_at: datetime | None = None
    # Provenance fields
    source: str = ""  # Where the skill came from (file path, URL, "builtin", etc.)
    source_type: str = "local"  # "local", "url", "builtin", "hub", "learned"
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = field(default_factory=dict)


class SkillParser:
    """Parse SKILL.md files with YAML frontmatter."""

    # Maximum allowed content size (1 MB) to prevent memory exhaustion
    MAX_CONTENT_SIZE = 1_000_000

    # Patterns that may indicate prompt injection attempts
    # These are targeted to reduce false positives on legitimate content
    _PROMPT_INJECTION_PATTERNS = [
        re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.IGNORECASE),
        re.compile(r"disregard\s+(all\s+)?prior\s+instructions", re.IGNORECASE),
        re.compile(r"forget\s+(all\s+)?previous\s+instructions", re.IGNORECASE),
        re.compile(r"system\s*:\s*(you\s+are|ignore|disregard|forget|override)", re.IGNORECASE),
        re.compile(r"<\s*system\s*>", re.IGNORECASE),
    ]

    @staticmethod
    def _validate_content(content: str, source_path: str = "") -> str:
        """Validate skill content for prompt injection attempts.

        Returns the content if safe, raises ValueError if suspicious patterns found.
        """
        if len(content) > SkillParser.MAX_CONTENT_SIZE:
            raise ValueError(
                f"Skill content exceeds maximum size of {SkillParser.MAX_CONTENT_SIZE} characters"
            )

        for pattern in SkillParser._PROMPT_INJECTION_PATTERNS:
            if pattern.search(content):
                raise ValueError(
                    f"Skill content contains potential prompt injection pattern "
                    f"(matched: {pattern.pattern[:30]}...)"
                )

        return content

    @staticmethod
    def parse(file_path: str | Path) -> Skill:
        """Parse a SKILL.md file."""
        path = Path(file_path)
        text = path.read_text(encoding="utf-8")

        # Extract YAML frontmatter
        frontmatter_match = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
        if not frontmatter_match:
            content = SkillParser._validate_content(text, str(path))
            return Skill(
                name=path.parent.name,
                description="",
                triggers=[],
                content=content,
                file_path=str(path),
            )

        yaml_text = frontmatter_match.group(1)
        content = SkillParser._validate_content(frontmatter_match.group(2).strip(), str(path))

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
            source=metadata.get("source", ""),
            source_type=metadata.get("source_type", "local"),
        )


class SkillRegistry:
    """Load, match, and manage skills."""

    def __init__(self, skills_dir: str | Path | None = None) -> None:
        self.skills_dir = Path(skills_dir) if skills_dir is not None else default_skills_dir()
        self._skills: dict[str, Skill] = {}
        self._telemetry_file = self.skills_dir / "telemetry.json"

    def _load_telemetry(self) -> None:
        """Load telemetry data from JSON file into skill objects."""
        if not self._telemetry_file.exists():
            return
        try:
            data = json.loads(self._telemetry_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return
            for name, telemetry in data.items():
                if name in self._skills:
                    skill = self._skills[name]
                    skill.usage_count = telemetry.get("usage_count", 0)
                    skill.view_count = telemetry.get("view_count", 0)
                    last_activity = telemetry.get("last_activity_at")
                    if last_activity:
                        skill.last_activity_at = datetime.fromisoformat(last_activity)
        except (json.JSONDecodeError, OSError, ValueError):
            pass

    def _save_telemetry(self) -> None:
        """Persist telemetry data to JSON file."""
        telemetry: dict[str, Any] = {}
        for name, skill in self._skills.items():
            telemetry[name] = {
                "usage_count": skill.usage_count,
                "view_count": skill.view_count,
                "last_activity_at": (
                    skill.last_activity_at.isoformat() if skill.last_activity_at else None
                ),
            }
        try:
            self._telemetry_file.write_text(
                json.dumps(telemetry, indent=2), encoding="utf-8"
            )
        except OSError:
            pass

    def load_all(self) -> None:
        """Load all skills from the skills directory.

        A single malformed or rejected SKILL.md (e.g. it trips the
        prompt-injection filter) is skipped with a warning rather than
        aborting the whole load.
        """
        if not self.skills_dir.exists():
            return
        for skill_dir in sorted(self.skills_dir.iterdir()):
            if skill_dir.is_dir():
                skill_file = skill_dir / "SKILL.md"
                if skill_file.exists():
                    try:
                        skill = SkillParser.parse(skill_file)
                    except (ValueError, OSError, UnicodeDecodeError) as e:
                        logger.warning("Skipping skill %s: %s", skill_file, e)
                        continue
                    self._skills[skill.name] = skill
        self._load_telemetry()

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def list_skills(self) -> list[Skill]:
        return list(self._skills.values())

    def match_triggers(self, query: str) -> list[Skill]:
        """Match query against skill triggers using word-boundary matching.

        A trigger matches only when it appears as a whole word in the query,
        preventing false positives like trigger "cat" matching "concatenate".
        """
        matched = []
        for skill in self._skills.values():
            for trigger in skill.triggers:
                # Use word-boundary regex for whole-word matching
                pattern = re.compile(r"\b" + re.escape(trigger.lower()) + r"\b")
                if pattern.search(query.lower()):
                    matched.append(skill)
                    break
        return matched

    def get_skill_content(self, name: str) -> str | None:
        """Get the full content of a skill (for injection into prompt)."""
        skill = self._skills.get(name)
        return skill.content if skill else None

    # ─── Telemetry methods ─────────────────────────────────────────────────

    def record_use(self, name: str) -> None:
        """Record that a skill was used (triggered and content injected)."""
        skill = self._skills.get(name)
        if skill:
            skill.usage_count += 1
            skill.last_activity_at = datetime.now(UTC)
            self._save_telemetry()

    def record_view(self, name: str) -> None:
        """Record that a skill was viewed (listed or inspected)."""
        skill = self._skills.get(name)
        if skill:
            skill.view_count += 1
            skill.last_activity_at = datetime.now(UTC)
            self._save_telemetry()

    def get_telemetry(self, name: str) -> dict[str, Any] | None:
        """Get telemetry data for a skill."""
        skill = self._skills.get(name)
        if not skill:
            return None
        return {
            "name": skill.name,
            "usage_count": skill.usage_count,
            "view_count": skill.view_count,
            "last_activity_at": skill.last_activity_at.isoformat()
            if skill.last_activity_at
            else None,
            "source": skill.source,
            "source_type": skill.source_type,
        }

    def get_all_telemetry(self) -> list[dict[str, Any]]:
        """Get telemetry data for all skills."""
        return [self.get_telemetry(s.name) for s in self._skills.values()]

    # ─── Provenance methods ────────────────────────────────────────────────

    def get_provenance(self, name: str) -> dict[str, Any] | None:
        """Get provenance data for a skill."""
        skill = self._skills.get(name)
        if not skill:
            return None
        return {
            "name": skill.name,
            "source": skill.source,
            "source_type": skill.source_type,
            "file_path": skill.file_path,
            "created_at": skill.created_at.isoformat(),
            "version": skill.version,
        }

    def list_by_source(self, source_type: str) -> list[Skill]:
        """List skills filtered by source type."""
        return [s for s in self._skills.values() if s.source_type == source_type]

    # ─── Skill creation (for /learn) ───────────────────────────────────────

    def create_skill(
        self,
        name: str,
        description: str,
        content: str,
        triggers: list[str] | None = None,
        source: str = "",
        source_type: str = "learned",
        version: str = "1.0.0",
    ) -> Skill:
        """Create a new skill and persist it to the skills directory."""
        # Validate content for prompt injection
        content = SkillParser._validate_content(content)

        # Sanitize name for directory
        safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", name.lower())
        skill_dir = self.skills_dir / safe_name
        skill_dir.mkdir(parents=True, exist_ok=True)

        # Build SKILL.md content
        triggers = triggers or []
        frontmatter = {
            "name": name,
            "description": description,
            "triggers": triggers,
            "version": version,
            "source": source,
            "source_type": source_type,
        }
        yaml_text = yaml.dump(frontmatter, default_flow_style=False, sort_keys=True)
        skill_content = f"---\n{yaml_text}---\n{content}\n"

        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text(skill_content, encoding="utf-8")

        skill = Skill(
            name=name,
            description=description,
            triggers=triggers,
            content=content,
            file_path=str(skill_file),
            version=version,
            source=source,
            source_type=source_type,
        )
        self._skills[name] = skill
        return skill

    def update_skill(
        self,
        name: str,
        description: str | None = None,
        content: str | None = None,
        triggers: list[str] | None = None,
    ) -> Skill | None:
        """Update an existing skill."""
        skill = self._skills.get(name)
        if not skill:
            return None

        if description is not None:
            skill.description = description
        if content is not None:
            skill.content = content
        if triggers is not None:
            skill.triggers = triggers

        # Rebuild SKILL.md
        frontmatter = {
            "name": skill.name,
            "description": skill.description,
            "triggers": skill.triggers,
            "version": skill.version,
            "source": skill.source,
            "source_type": skill.source_type,
        }
        yaml_text = yaml.dump(frontmatter, default_flow_style=False, sort_keys=True)
        skill_content = f"---\n{yaml_text}---\n{skill.content}\n"

        Path(skill.file_path).write_text(skill_content, encoding="utf-8")
        return skill

    def delete_skill(self, name: str) -> bool:
        """Delete a skill by name."""
        skill = self._skills.pop(name, None)
        if skill:
            skill_path = Path(skill.file_path)
            if skill_path.exists():
                skill_path.unlink()
            # Remove parent directory if empty
            parent = skill_path.parent
            if parent.exists() and not any(parent.iterdir()):
                parent.rmdir()
            return True
        return False

    @classmethod
    def reset(cls) -> None:
        """Reset the global SkillRegistry singleton to a fresh instance."""
        global skill_registry
        skill_registry = cls()


class SkillCurator:
    """Background maintenance for skills — archive stale, clean up unused."""

    def __init__(self, registry: SkillRegistry) -> None:
        self.registry = registry

    def archive_stale(self, days: int = 30) -> list[str]:
        """Archive skills that haven't been used in *days* days."""
        cutoff = datetime.now(UTC) - timedelta(days=days)
        archived = []
        for skill in self.registry.list_skills():
            if skill.last_activity_at is None:
                # Never used — check created_at
                if skill.created_at < cutoff:
                    skill.enabled = False
                    archived.append(skill.name)
            elif skill.last_activity_at < cutoff:
                skill.enabled = False
                archived.append(skill.name)
        return archived

    def get_stale_skills(self, days: int = 30) -> list[Skill]:
        """Get list of stale skills without modifying them."""
        cutoff = datetime.now(UTC) - timedelta(days=days)
        stale = []
        for skill in self.registry.list_skills():
            if skill.last_activity_at is None:
                if skill.created_at < cutoff:
                    stale.append(skill)
            elif skill.last_activity_at < cutoff:
                stale.append(skill)
        return stale

    def get_unused_skills(self) -> list[Skill]:
        """Get skills that have never been used."""
        return [s for s in self.registry.list_skills() if s.usage_count == 0]

    def get_top_skills(self, limit: int = 10) -> list[Skill]:
        """Get most frequently used skills."""
        skills = self.registry.list_skills()
        return sorted(skills, key=lambda s: s.usage_count, reverse=True)[:limit]

    def cleanup_unused(self, dry_run: bool = True) -> list[str]:
        """Remove skills that have never been used. Returns list of removed skill names."""
        unused = self.get_unused_skills()
        removed = []
        for skill in unused:
            if not dry_run:
                self.registry.delete_skill(skill.name)
            removed.append(skill.name)
        return removed

    def get_health_report(self) -> dict[str, Any]:
        """Get a health report for the skill system."""
        skills = self.registry.list_skills()
        total = len(skills)
        enabled = sum(1 for s in skills if s.enabled)
        disabled = total - enabled
        never_used = sum(1 for s in skills if s.usage_count == 0)
        total_uses = sum(s.usage_count for s in skills)
        total_views = sum(s.view_count for s in skills)
        stale = len(self.get_stale_skills())

        return {
            "total_skills": total,
            "enabled": enabled,
            "disabled": disabled,
            "never_used": never_used,
            "total_uses": total_uses,
            "total_views": total_views,
            "stale_skills": stale,
            "top_skills": [
                {"name": s.name, "usage_count": s.usage_count} for s in self.get_top_skills(5)
            ],
        }


class SkillHub:
    """Community-curated skill sharing — publish, discover, install skills."""

    def __init__(self, registry: SkillRegistry, hub_dir: str | Path | None = None) -> None:
        self.registry = registry
        self.hub_dir = Path(hub_dir) if hub_dir is not None else default_hub_dir()
        self.hub_dir.mkdir(parents=True, exist_ok=True)

    def _hub_file(self, name: str) -> Path:
        """Return the hub JSON path for *name*, rejecting path traversal."""
        if (
            not name
            or any(sep in name for sep in ("/", "\\"))
            or name in (".", "..")
            or ".." in name
        ):
            raise ValueError(f"Invalid skill name: {name!r}")
        path = (self.hub_dir / f"{name}.json").resolve()
        if not path.is_relative_to(self.hub_dir.resolve()):
            raise ValueError(f"Invalid skill name: {name!r}")
        return path

    def publish(self, name: str, author: str = "", tags: list[str] | None = None) -> dict[str, Any]:
        """Publish a skill to the hub."""
        skill = self.registry.get(name)
        if not skill:
            raise ValueError(f"Skill '{name}' not found")

        hub_entry = {
            "name": skill.name,
            "description": skill.description,
            "triggers": skill.triggers,
            "content": skill.content,
            "version": skill.version,
            "author": author,
            "tags": tags or [],
            "published_at": datetime.now(UTC).isoformat(),
            "usage_count": skill.usage_count,
            "view_count": skill.view_count,
        }

        hub_file = self._hub_file(name)
        hub_file.write_text(json.dumps(hub_entry, indent=2), encoding="utf-8")
        return hub_entry

    def list_hub(self) -> list[dict[str, Any]]:
        """List all skills available in the hub."""
        skills = []
        for f in self.hub_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                skills.append(data)
            except (json.JSONDecodeError, OSError):
                continue
        return skills

    def search(self, query: str) -> list[dict[str, Any]]:
        """Search hub skills by name, description, or tags."""
        query_lower = query.lower()
        results = []
        for entry in self.list_hub():
            if (
                query_lower in entry.get("name", "").lower()
                or query_lower in entry.get("description", "").lower()
                or any(query_lower in t.lower() for t in entry.get("tags", []))
            ):
                results.append(entry)
        return results

    def install(self, name: str) -> Skill:
        """Install a skill from the hub into the local registry."""
        hub_file = self._hub_file(name)
        if not hub_file.exists():
            raise ValueError(f"Skill '{name}' not found in hub")

        data = json.loads(hub_file.read_text(encoding="utf-8"))
        return self.registry.create_skill(
            name=data["name"],
            description=data.get("description", ""),
            content=data.get("content", ""),
            triggers=data.get("triggers", []),
            source=f"hub:{name}",
            source_type="hub",
            version=data.get("version", "1.0.0"),
        )

    def get_hub_telemetry(self) -> list[dict[str, Any]]:
        """Get telemetry for all hub skills."""
        return self.list_hub()


# Global registry
skill_registry = SkillRegistry()
