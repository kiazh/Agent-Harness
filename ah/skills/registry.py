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
        re.compile(
            r"reveal\s+(your\s+|the\s+)?(system|developer|hidden|secret)\s*(prompt|message|instruction)",
            re.IGNORECASE,
        ),
        re.compile(r"bypass\s+(safety|safe|guardrail|filter|restriction)", re.IGNORECASE),
        re.compile(r"jailbreak", re.IGNORECASE),
        re.compile(r"exfiltrat\w*", re.IGNORECASE),
        re.compile(r"system\s+prompt", re.IGNORECASE),
        re.compile(r"developer\s+message", re.IGNORECASE),
        re.compile(r"decode\s+base64|base64\s+decode|from\s*base64", re.IGNORECASE),
        re.compile(r"override\s+(safety|guardrail|instruction|system)", re.IGNORECASE),
    ]

    @staticmethod
    def _scan_injection(text: str) -> None:
        for pattern in SkillParser._PROMPT_INJECTION_PATTERNS:
            if pattern.search(text or ""):
                raise ValueError(
                    f"Skill content contains potential prompt injection pattern "
                    f"(matched: {pattern.pattern[:30]}...)"
                )

    @staticmethod
    def _validate_metadata(name: Any, description: Any, triggers: Any) -> None:
        """Reject malformed metadata before search or prompt catalog use."""
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", name):
            raise ValueError("skill name must be a short alphanumeric slug")
        if not isinstance(description, str) or len(description) > 1000:
            raise ValueError("skill description must be a string of at most 1000 characters")
        if (
            not isinstance(triggers, list)
            or len(triggers) > 32
            or any(not isinstance(item, str) or not 0 < len(item) <= 100 for item in triggers)
        ):
            raise ValueError("skill triggers must be a list of short strings")

    @staticmethod
    def _validate_content(content: str, source_path: str = "") -> str:
        """Validate skill content for prompt injection attempts.

        Returns the content if safe, raises ValueError if suspicious patterns found.
        Also scans the text regardless of caller; description/triggers are
        scanned separately via _scan_injection by callers.
        """
        if len(content) > SkillParser.MAX_CONTENT_SIZE:
            raise ValueError(
                f"Skill content exceeds maximum size of {SkillParser.MAX_CONTENT_SIZE} characters"
            )

        SkillParser._scan_injection(content)

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
            SkillParser._validate_metadata(path.parent.name, "", [])
            return Skill(
                name=path.parent.name,
                description="",
                triggers=[],
                content=content,
                file_path=str(path),
                created_at=datetime.fromtimestamp(path.stat().st_mtime, UTC),
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

        if not isinstance(metadata, dict):
            raise ValueError("skill frontmatter must be a mapping")
        name = metadata.get("name", path.parent.name)
        description = metadata.get("description", "")
        triggers = metadata.get("triggers", [])
        SkillParser._validate_metadata(name, description, triggers)
        # Scan description + triggers as well as content for injection.
        if isinstance(description, str):
            SkillParser._scan_injection(description)
        if isinstance(triggers, list):
            for _t in triggers:
                if isinstance(_t, str):
                    SkillParser._scan_injection(_t)
        created_at = metadata.get("created_at")
        if isinstance(created_at, str):
            try:
                created_at = datetime.fromisoformat(created_at)
            except ValueError:
                raise ValueError("skill created_at must be an ISO timestamp") from None
        if created_at is None:
            created_at = datetime.fromtimestamp(path.stat().st_mtime, UTC)
        if not isinstance(created_at, datetime):
            raise ValueError("skill created_at must be an ISO timestamp")
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        return Skill(
            name=name,
            description=description,
            triggers=triggers,
            content=content,
            file_path=str(path),
            version=metadata.get("version", "1.0.0"),
            enabled=metadata.get("enabled", True) is not False,
            source=metadata.get("source", ""),
            source_type=metadata.get("source_type", "local"),
            created_at=created_at,
        )


class SkillRegistry:
    """Load, match, and manage skills."""

    def __init__(self, skills_dir: str | Path | None = None) -> None:
        self.skills_dir = Path(skills_dir) if skills_dir is not None else default_skills_dir()
        self._skills: dict[str, Skill] = {}
        self._telemetry_file = self.skills_dir / "telemetry.json"
        # ``load_all()`` is called on every skill tool call and by every CLI /
        # gateway skill handler. Re-reading and re-parsing every SKILL.md plus
        # telemetry.json each time is pure waste, so the parsed result is
        # cached and validated against a cheap on-disk signature. That keeps
        # reload semantics (out-of-band edits are still picked up) without
        # paying for a full parse on every call.
        self._signature_cache: tuple | None = None

    def _invalidate(self) -> None:
        """Mark the parsed skill cache stale so the next load_all() re-reads."""
        self._signature_cache = None

    def _signature(self) -> tuple:
        """Cheap fingerprint of the on-disk skill set used to validate the cache.

        Stats only — no file contents are read. A changed mtime or size on any
        SKILL.md, or on telemetry.json, invalidates the cached parse.
        """
        entries: list[tuple[str, int, int]] = []
        try:
            children = sorted(self.skills_dir.iterdir())
        except OSError:
            children = []
        for child in children:
            if not child.is_dir():
                continue
            try:
                stat = (child / "SKILL.md").stat()
            except OSError:
                continue
            entries.append((child.name, stat.st_mtime_ns, stat.st_size))
        try:
            telemetry_stat = self._telemetry_file.stat()
            telemetry = (telemetry_stat.st_mtime_ns, telemetry_stat.st_size)
        except OSError:
            telemetry = None
        return (tuple(entries), telemetry)

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
            logger.warning("Failed to load telemetry", exc_info=True)

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
            self._telemetry_file.write_text(json.dumps(telemetry, indent=2), encoding="utf-8")
        except OSError:
            logger.warning("Failed to save telemetry", exc_info=True)
        self._invalidate()

    def load_all(self, *, refresh: bool = False) -> None:
        """Load all skills from the skills directory (reload semantics, cached).

        A single malformed or rejected SKILL.md (e.g. it trips the
        prompt-injection filter) is skipped with a warning rather than
        aborting the whole load.

        Repeated calls with no on-disk change are cheap no-ops: the parsed
        result is cached and validated against a stat-only signature, so
        out-of-band edits are still picked up. ``refresh=True`` forces a
        re-parse even when the signature matches.
        """
        signature = self._signature()
        if self._signature_cache is not None and signature == self._signature_cache and not refresh:
            return
        self._skills.clear()
        if self.skills_dir.exists():
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
        self._signature_cache = signature

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
            if not skill.enabled:
                continue
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
        return skill.content if skill and skill.enabled else None

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
        # Validate content for prompt injection (content + description/triggers).
        content = SkillParser._validate_content(content)
        triggers = triggers or []
        SkillParser._validate_metadata(name, description, triggers)
        SkillParser._scan_injection(description)
        for _t in triggers:
            SkillParser._scan_injection(_t)

        # Sanitize name for directory
        safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", name.lower())
        skill_dir = self.skills_dir / safe_name
        skill_dir.mkdir(parents=True, exist_ok=True)

        # Build SKILL.md content
        created_at = datetime.now(UTC)
        frontmatter = {
            "name": name,
            "description": description,
            "triggers": triggers,
            "version": version,
            "enabled": True,
            "created_at": created_at.isoformat(),
            "source": source,
            "source_type": source_type,
        }
        yaml_text = yaml.dump(frontmatter, default_flow_style=False, sort_keys=True)
        skill_content = f"---\n{yaml_text}---\n{content}\n"

        skill_file = skill_dir / "SKILL.md"
        # Exclusive creation is the final read-before-write guard when two
        # reviews (or a manual learn) target the same skill concurrently.
        try:
            with skill_file.open("x", encoding="utf-8") as output:
                output.write(skill_content)
        except FileExistsError:
            raise ValueError(f"skill '{name}' already exists") from None

        skill = Skill(
            name=name,
            description=description,
            triggers=triggers,
            content=content,
            file_path=str(skill_file),
            version=version,
            source=source,
            source_type=source_type,
            created_at=created_at,
        )
        self._skills[name] = skill
        self._invalidate()
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
            SkillParser._scan_injection(description)
            skill.description = description
        if content is not None:
            skill.content = SkillParser._validate_content(content, skill.file_path)
        if triggers is not None:
            for _t in triggers:
                SkillParser._scan_injection(_t)
            SkillParser._validate_metadata(skill.name, skill.description, triggers)
            skill.triggers = triggers

        # Rebuild SKILL.md
        frontmatter = {
            "name": skill.name,
            "description": skill.description,
            "triggers": skill.triggers,
            "version": skill.version,
            "enabled": skill.enabled,
            "created_at": skill.created_at.isoformat(),
            "source": skill.source,
            "source_type": skill.source_type,
        }
        yaml_text = yaml.dump(frontmatter, default_flow_style=False, sort_keys=True)
        skill_content = f"---\n{yaml_text}---\n{skill.content}\n"

        Path(skill.file_path).write_text(skill_content, encoding="utf-8")
        self._invalidate()
        return skill

    def set_enabled(self, name: str, enabled: bool) -> Skill | None:
        """Persist whether a skill is available for agent discovery and reading."""
        skill = self._skills.get(name)
        if skill is None:
            return None
        skill.enabled = enabled
        return self.update_skill(name)

    def delete_skill(self, name: str) -> bool:
        """Delete a skill by name."""
        skill = self._skills.pop(name, None)
        if skill:
            self._invalidate()
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
                    self.registry.set_enabled(skill.name, False)
                    archived.append(skill.name)
            elif skill.last_activity_at < cutoff:
                self.registry.set_enabled(skill.name, False)
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
