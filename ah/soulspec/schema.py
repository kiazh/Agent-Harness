"""SoulSpec schema — dataclasses for agent configuration."""
from __future__ import annotations

from dataclasses import dataclass, field
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
                p["values"] = [
                    {"name": v.name, "weight": v.weight} for v in self.persona.values
                ]
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
        return AgentDef(
            name=self.name,
            description=self.persona.description if self.persona else "",
            system_prompt=system_prompt,
            tools=tools,
            model=self.config.model if self.config else None,
            max_iterations=self.config.max_iterations if self.config else 10,
        )
