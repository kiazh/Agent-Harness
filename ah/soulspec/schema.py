"""AgentHarness persona schema with import/export for Soul Spec packages."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ah.core.agent_def import AgentDef

# Unique sentinel object for an explicitly empty tool allowlist.
# Using a dedicated object (not a string) prevents collision with real tool names.
_NO_TOOLS_SENTINEL = object()

_LICENSES = {
    "Apache-2.0",
    "MIT",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "CC-BY-4.0",
    "CC0-1.0",
    "ISC",
    "Unlicense",
}
_PACKAGE_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_SEMVER = re.compile(
    r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?\Z"
)


def _package_file(root: Path, relative: str) -> Path:
    """Resolve a declared package file without allowing traversal or symlinks out."""
    if (
        not relative
        or "\\" in relative
        or relative.startswith("/")
        or any(part in ("", ".", "..") for part in relative.split("/"))
    ):
        raise ValueError(f"invalid package path: {relative!r}")
    result = (root / relative).resolve()
    if not result.is_relative_to(root):
        raise ValueError(f"package path escapes root: {relative!r}")
    return result


def _validate_manifest(manifest: dict[str, Any]) -> dict[str, str]:
    required = (
        "specVersion",
        "name",
        "displayName",
        "version",
        "description",
        "author",
        "license",
        "tags",
        "category",
        "files",
    )
    missing = [key for key in required if key not in manifest]
    if missing:
        raise ValueError(f"soul.json missing required fields: {', '.join(missing)}")
    if manifest["specVersion"] not in {"0.3", "0.4", "0.5"}:
        raise ValueError("unsupported Soul Spec version")
    for key in ("name", "displayName", "version", "description", "license", "category"):
        if not isinstance(manifest[key], str) or not manifest[key].strip():
            raise ValueError(f"{key} must be a non-empty string")
    if not _PACKAGE_NAME.fullmatch(manifest["name"]):
        raise ValueError("name must be kebab-case")
    # Semver core is mandatory; prerelease/build suffixes are accepted.
    if not _SEMVER.fullmatch(manifest["version"]):
        raise ValueError("version must be semver")
    if len(manifest["description"]) > 160:
        raise ValueError("description must be at most 160 characters")
    if manifest["license"] not in _LICENSES:
        raise ValueError("license is not in the Soul Spec allowlist")
    if any(not _PACKAGE_NAME.fullmatch(part) for part in manifest["category"].split("/")):
        raise ValueError("category must be a path of kebab-case names")
    if (
        not isinstance(manifest["author"], dict)
        or not isinstance(manifest["author"].get("name"), str)
        or not manifest["author"]["name"].strip()
    ):
        raise ValueError("author.name is required")
    tags = manifest["tags"]
    if (
        not isinstance(tags, list)
        or len(tags) > 10
        or any(not isinstance(tag, str) or not tag.strip() for tag in tags)
    ):
        raise ValueError("tags must be a list of at most ten non-empty strings")
    files = manifest["files"]
    if not isinstance(files, dict) or not isinstance(files.get("soul"), str):
        raise ValueError("files.soul must name SOUL.md")
    if files["soul"].split("/")[-1] != "SOUL.md":
        raise ValueError("files.soul must name SOUL.md")
    references = {}
    for group in (files, manifest.get("examples", {})):
        if not isinstance(group, dict):
            raise ValueError("files and examples must be objects")
        for key, relative in group.items():
            if not isinstance(relative, str):
                raise ValueError(f"{key} must be a file path")
            references[key] = relative
    if len(references.values()) != len(set(references.values())):
        raise ValueError("package files must not alias the same path")
    for key in ("allowedTools",):
        if key in manifest and (
            not isinstance(manifest[key], list)
            or any(not isinstance(value, str) or not value for value in manifest[key])
        ):
            raise ValueError(f"{key} must be a list of strings")
    skills = manifest.get("recommendedSkills", [])
    if not isinstance(skills, list) or any(
        not isinstance(skill, dict)
        or not isinstance(skill.get("name"), str)
        or not skill["name"]
        or ("required" in skill and type(skill["required"]) is not bool)
        or ("version" in skill and not isinstance(skill["version"], str))
        for skill in skills
    ):
        raise ValueError("recommendedSkills must contain named skill objects")
    legacy_skills = manifest.get("skills", [])
    if not isinstance(legacy_skills, list) or any(
        not isinstance(skill, str) or not skill for skill in legacy_skills
    ):
        raise ValueError("legacy skills must be a list of names")
    disclosure = manifest.get("disclosure", {})
    if not isinstance(disclosure, dict) or (
        "summary" in disclosure
        and (not isinstance(disclosure["summary"], str) or len(disclosure["summary"]) > 200)
    ):
        raise ValueError("disclosure.summary must be at most 200 characters")
    if "deprecated" in manifest and type(manifest["deprecated"]) is not bool:
        raise ValueError("deprecated must be a boolean")
    if "supersededBy" in manifest and not isinstance(manifest["supersededBy"], str):
        raise ValueError("supersededBy must be a string")
    compatibility = manifest.get("compatibility", {})
    if not isinstance(compatibility, dict):
        raise ValueError("compatibility must be an object")
    for key in ("models", "frameworks"):
        if key in compatibility and (
            not isinstance(compatibility[key], list)
            or any(not isinstance(value, str) for value in compatibility[key])
        ):
            raise ValueError(f"compatibility.{key} must be a list of strings")
    if "openclaw" in compatibility and not isinstance(compatibility["openclaw"], str):
        raise ValueError("compatibility.openclaw must be a string")
    if "minTokenContext" in compatibility and (
        type(compatibility["minTokenContext"]) is not int or compatibility["minTokenContext"] < 1
    ):
        raise ValueError("compatibility.minTokenContext must be positive")
    if manifest.get("environment", "virtual") not in {"virtual", "embodied", "hybrid"}:
        raise ValueError("invalid environment")
    if manifest.get("interactionMode", "text") not in {"text", "voice", "multimodal", "gesture"}:
        raise ValueError("invalid interactionMode")
    hardware = manifest.get("hardwareConstraints", {})
    if not isinstance(hardware, dict):
        raise ValueError("hardwareConstraints must be an object")
    for key in ("hasDisplay", "hasSpeaker", "hasMicrophone", "hasCamera", "manipulator"):
        if key in hardware and type(hardware[key]) is not bool:
            raise ValueError(f"hardwareConstraints.{key} must be boolean")
    if "mobility" in hardware and hardware["mobility"] not in {"stationary", "mobile", "limited"}:
        raise ValueError("invalid hardwareConstraints.mobility")
    safety = manifest.get("safety", {})
    if not isinstance(safety, dict):
        raise ValueError("safety must be an object")
    physical = safety.get("physical", {})
    if not isinstance(physical, dict):
        raise ValueError("safety.physical must be an object")
    for key, allowed in (
        ("contactPolicy", {"no-contact", "gentle-contact", "full-contact"}),
        ("emergencyProtocol", {"stop", "alert_operator", "return_home"}),
        ("operatingZone", {"indoor", "outdoor", "both"}),
    ):
        if key in physical and physical[key] not in allowed:
            raise ValueError(f"invalid safety.physical.{key}")
    for key in ("sensors", "actuators"):
        if key in manifest and not isinstance(manifest[key], dict):
            raise ValueError(f"{key} must be an object")
    return references


@dataclass
class SoulSpec:
    """Open standard for agent configuration."""

    name: str
    version: str | None = None
    persona: SoulSpec.Persona | None = None
    workflow: list[SoulSpec.Workflow] = field(default_factory=list)
    skills: list[SoulSpec.Skill] = field(default_factory=list)
    config: SoulSpec.Config | None = None
    package_manifest: dict[str, Any] = field(default_factory=dict, repr=False)
    package_files: dict[str, bytes] = field(default_factory=dict, repr=False)

    # ─── Nested dataclasses ──────────────────────────────────────────────

    @dataclass
    class Voice:
        tone: str = "neutral"
        style: str = "concise"

    @dataclass
    class PersonaValue:
        name: str
        weight: float = 0.5

    @dataclass
    class PersonaTrait:
        name: str
        strength: float = 0.5

    @dataclass
    class Persona:
        name: str = ""
        description: str = ""
        values: list[SoulSpec.PersonaValue] = field(default_factory=list)
        traits: list[SoulSpec.PersonaTrait] = field(default_factory=list)
        voice: SoulSpec.Voice | None = None
        system_prompt: str = ""

    @dataclass
    class Workflow:
        name: str
        description: str = ""
        steps: list[str] = field(default_factory=list)
        tools: list[str] = field(default_factory=list)

    @dataclass
    class Skill:
        name: str
        triggers: list[str] = field(default_factory=list)
        path: str = ""
        progressive_disclosure_level: int = 2  # 1=always, 2=on-demand, 3=deep

    @dataclass
    class Permissions:
        allowed_tools: list[str] = field(default_factory=list)
        denied_tools: list[str] = field(default_factory=list)

    @dataclass
    class Config:
        model: str | None = None
        max_iterations: int = 10
        context_budget: int = 8000
        permissions: SoulSpec.Permissions | None = None

    # ─── Constructors ────────────────────────────────────────────────────

    @classmethod
    def from_package(cls, directory: str | Path) -> SoulSpec:
        """Read a Soul Spec package (soul.json plus SOUL.md).

        The older YAML format remains available through ``from_yaml``.
        """
        root = Path(directory).resolve()
        manifest = json.loads(_package_file(root, "soul.json").read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError("soul.json must contain a JSON object")
        references = _validate_manifest(manifest)
        package_files = {}
        for relative in references.values():
            package_files[relative] = _package_file(root, relative).read_bytes()
        soul_text = package_files[manifest["files"]["soul"]].decode("utf-8")
        if not soul_text.strip():
            raise ValueError("SOUL.md is empty")
        return cls(
            name=manifest["name"],
            version=manifest["version"],
            persona=cls.Persona(
                name=manifest["displayName"],
                description=manifest["description"],
                system_prompt=soul_text,
            ),
            package_manifest=manifest,
            package_files=package_files,
        )

    def write_package(
        self,
        directory: str | Path,
        *,
        author: str | None = None,
        license: str | None = None,
        category: str | None = None,
        tags: list[str] | None = None,
    ) -> None:
        """Export the runtime persona as a minimal Soul Spec v0.5 package."""
        if not self.persona or not self.persona.system_prompt.strip():
            raise ValueError("a non-empty persona system prompt is required")
        root = Path(directory).resolve()
        source = deepcopy(self.package_manifest)
        author_info = deepcopy(source.get("author", {}))
        if author is not None:
            author_info["name"] = author
        manifest = {
            **source,
            "specVersion": "0.5",
            "name": self.name,
            "displayName": self.persona.name or self.name,
            "version": self.version,
            "description": self.persona.description[:160],
            "author": author_info,
            "license": license if license is not None else source.get("license", "MIT"),
            "tags": tags if tags is not None else source.get("tags", []),
            "category": category if category is not None else source.get("category", "general"),
            "files": source.get("files", {"soul": "SOUL.md"}),
        }
        references = _validate_manifest(manifest)
        output_files = dict(self.package_files)
        output_files[manifest["files"]["soul"]] = self.persona.system_prompt.encode("utf-8")
        for relative in references.values():
            if relative not in output_files:
                raise ValueError(f"missing package file: {relative}")
            _package_file(root, relative)
        root.mkdir(parents=True, exist_ok=True)
        for relative in references.values():
            destination = _package_file(root, relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(output_files[relative])
        _package_file(root, "soul.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

    @classmethod
    def from_yaml(cls, yaml_str: str) -> SoulSpec:
        """Create SoulSpec from a YAML string."""
        data = yaml.safe_load(yaml_str)
        if not isinstance(data, dict):
            raise ValueError("SoulSpec YAML must be a mapping")
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SoulSpec:
        """Create SoulSpec from a plain dict."""
        persona = None
        if data.get("persona"):
            p = data["persona"]
            voice = None
            if p.get("voice"):
                voice = cls.Voice(
                    tone=p["voice"].get("tone", "neutral"),
                    style=p["voice"].get("style", "concise"),
                )
            persona = cls.Persona(
                name=p.get("name", ""),
                description=p.get("description", ""),
                values=[
                    cls.PersonaValue(name=v["name"], weight=v.get("weight", 0.5))
                    for v in p.get("values", [])
                ],
                traits=[
                    cls.PersonaTrait(name=t["name"], strength=t.get("strength", 0.5))
                    for t in p.get("traits", [])
                ],
                voice=voice,
                system_prompt=p.get("system_prompt", ""),
            )

        workflow = [
            cls.Workflow(
                name=w["name"],
                description=w.get("description", ""),
                steps=w.get("steps", []),
                tools=w.get("tools", []),
            )
            for w in data.get("workflow", [])
        ]

        skills = [
            cls.Skill(
                name=s["name"],
                triggers=s.get("triggers", []),
                path=s.get("path", ""),
                progressive_disclosure_level=s.get("progressive_disclosure", {}).get("level", 2)
                if isinstance(s.get("progressive_disclosure"), dict)
                else s.get("progressive_disclosure_level", 2),
            )
            for s in data.get("skills", [])
        ]

        config = None
        if data.get("config"):
            c = data["config"]
            permissions = None
            if c.get("permissions"):
                permissions = cls.Permissions(
                    allowed_tools=c["permissions"].get("allowed_tools", []),
                    denied_tools=c["permissions"].get("denied_tools", []),
                )
            config = cls.Config(
                model=c.get("model"),
                max_iterations=c.get("max_iterations", 10),
                context_budget=c.get("context_budget", 8000),
                permissions=permissions,
            )

        return cls(
            name=data["name"],
            version=data.get("version", "1.0.0"),
            persona=persona,
            workflow=workflow,
            skills=skills,
            config=config,
        )

    # ─── Serialization ───────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        """Convert to plain dict."""
        result: dict[str, Any] = {"name": self.name, "version": self.version}
        if self.persona:
            p: dict[str, Any] = {
                "name": self.persona.name,
                "description": self.persona.description,
            }
            if self.persona.values:
                p["values"] = [{"name": v.name, "weight": v.weight} for v in self.persona.values]
            if self.persona.traits:
                p["traits"] = [
                    {"name": t.name, "strength": t.strength} for t in self.persona.traits
                ]
            if self.persona.voice:
                p["voice"] = {
                    "tone": self.persona.voice.tone,
                    "style": self.persona.voice.style,
                }
            if self.persona.system_prompt:
                p["system_prompt"] = self.persona.system_prompt
            result["persona"] = p
        if self.workflow:
            result["workflow"] = [
                {
                    "name": w.name,
                    "description": w.description,
                    "steps": w.steps,
                    "tools": w.tools,
                }
                for w in self.workflow
            ]
        if self.skills:
            result["skills"] = [
                {
                    "name": s.name,
                    "triggers": s.triggers,
                    "path": s.path,
                    "progressive_disclosure": {"level": s.progressive_disclosure_level},
                }
                for s in self.skills
            ]
        if self.config:
            c: dict[str, Any] = {
                "model": self.config.model,
                "max_iterations": self.config.max_iterations,
                "context_budget": self.config.context_budget,
            }
            if self.config.permissions:
                c["permissions"] = {
                    "allowed_tools": self.config.permissions.allowed_tools,
                    "denied_tools": self.config.permissions.denied_tools,
                }
            result["config"] = c
        return result

    def to_yaml(self) -> str:
        """Serialize to YAML string."""
        return yaml.dump(self.to_dict(), default_flow_style=False, sort_keys=False)

    def to_agent_def(self) -> AgentDef:
        """Convert SoulSpec to AgentDef."""
        system_prompt = ""
        tools: list[str] = []
        if self.persona:
            system_prompt = self.persona.system_prompt.strip()
        if self.workflow:
            for wf in self.workflow:
                for t in wf.tools:
                    if t not in tools:
                        tools.append(t)
        if self.config and self.config.permissions:
            permissions = self.config.permissions
            if permissions.allowed_tools:
                tools = (
                    [t for t in tools if t in permissions.allowed_tools]
                    if tools
                    else list(permissions.allowed_tools)
                )
            tools = [t for t in tools if t not in permissions.denied_tools]
            if not tools and (permissions.allowed_tools or permissions.denied_tools):
                # AgentDef uses [] to mean every tool; a private sentinel keeps
                # an explicitly empty effective allowlist empty at runtime.
                tools = [_NO_TOOLS_SENTINEL]
        return AgentDef(
            name=self.name,
            description=self.persona.description if self.persona else "",
            system_prompt=system_prompt,
            tools=tools,
            model=self.config.model if self.config else None,
            max_iterations=self.config.max_iterations if self.config else 10,
        )
