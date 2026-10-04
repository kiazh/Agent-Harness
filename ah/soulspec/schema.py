"""AgentHarness persona schema with import/export for Soul Spec packages."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ah.core.agent_def import AgentDef


@dataclass
class SoulSpec:
    """Open standard for agent configuration."""

    name: str
    version: str = "1.0.0"
    persona: SoulSpec.Persona | None = None
    workflow: list[SoulSpec.Workflow] = field(default_factory=list)
    skills: list[SoulSpec.Skill] = field(default_factory=list)
    config: SoulSpec.Config | None = None
    package_manifest: dict[str, Any] = field(default_factory=dict, repr=False)

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
        manifest = json.loads((root / "soul.json").read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError("soul.json must contain a JSON object")
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
        if len(manifest["description"]) > 160:
            raise ValueError("description must be at most 160 characters")
        if not isinstance(manifest["author"], dict) or not manifest["author"].get("name"):
            raise ValueError("author.name is required")
        if (
            not isinstance(manifest["tags"], list)
            or len(manifest["tags"]) > 10
            or any(not isinstance(tag, str) for tag in manifest["tags"])
        ):
            raise ValueError("tags must be a list of at most ten strings")
        files = manifest["files"]
        if not isinstance(files, dict) or not isinstance(files.get("soul"), str):
            raise ValueError("files.soul must name SOUL.md")
        soul_path = (root / files["soul"]).resolve()
        if not soul_path.is_relative_to(root) or soul_path.name != "SOUL.md":
            raise ValueError("SOUL.md must stay inside the package")
        soul_text = soul_path.read_text(encoding="utf-8")
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
        )

    def write_package(
        self,
        directory: str | Path,
        *,
        author: str,
        license: str = "MIT",
        category: str = "general",
        tags: list[str] | None = None,
    ) -> None:
        """Export the runtime persona as a minimal Soul Spec v0.5 package."""
        if not self.persona or not self.persona.system_prompt.strip():
            raise ValueError("a non-empty persona system prompt is required")
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        manifest = {
            **self.package_manifest,
            "specVersion": "0.5",
            "name": self.name,
            "displayName": self.persona.name or self.name,
            "version": self.version,
            "description": self.persona.description[:160],
            "author": {"name": author},
            "license": license,
            "tags": tags or [],
            "category": category,
            "files": {"soul": "SOUL.md"},
        }
        (root / "soul.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        (root / "SOUL.md").write_text(self.persona.system_prompt, encoding="utf-8")

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
                tools = ["__no_tools__"]
        return AgentDef(
            name=self.name,
            description=self.persona.description if self.persona else "",
            system_prompt=system_prompt,
            tools=tools,
            model=self.config.model if self.config else None,
            max_iterations=self.config.max_iterations if self.config else 10,
        )
