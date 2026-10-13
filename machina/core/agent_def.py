"""Agent definitions and registry (Phase 5: multi-agent).

An :class:`AgentDef` is a named persona: a system prompt, an optional tool
allow-list, and model/provider overrides. Definitions live in the ``agents``
table; a few built-ins are always available. The registry is the single place
the rest of the system looks them up.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from machina.db.connection import db

__all__ = ["AgentDef", "AgentRegistry", "agent_registry", "BUILTIN_AGENTS"]

logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def agents_directory() -> Path:
    """Directory containing local YAML agent definitions."""
    return Path(os.environ.get("MACHINA_AGENTS_DIR") or _PROJECT_ROOT / "agents")


@dataclass
class AgentDef:
    """A named agent persona."""

    name: str
    description: str = ""
    system_prompt: str = ""
    tools: list[str] = field(default_factory=list)  # empty = every registered tool
    model: str | None = None
    provider: str | None = None
    max_iterations: int = 10
    source: str = "db"
    soul_spec: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "systemPrompt": self.system_prompt,
            "tools": list(self.tools),
            "model": self.model,
            "provider": self.provider,
            "maxIterations": self.max_iterations,
            "source": self.source,
        }


# Always-available personas. The default general agent uses the harness prompt.
BUILTIN_AGENTS: dict[str, AgentDef] = {
    "harness": AgentDef(
        name="harness",
        description="General-purpose agent with every tool.",
        system_prompt="",  # ReActAgent falls back to its SYSTEM_PROMPT
        source="builtin",
    ),
    "researcher": AgentDef(
        name="researcher",
        description="Reads and searches; never writes files or runs commands.",
        system_prompt=(
            "You are a focused research assistant. Gather information with the available "
            "read and search tools, then answer concisely with sources. You cannot modify "
            "files or run commands."
        ),
        tools=[
            "read_file",
            "list_files",
            "search_files",
            "web_search",
            "web_extract",
            "web_research",
            "search_documents",
            "recall",
            "learn_personality",
            "skill_list",
            "skill_read",
            "skill_resource",
        ],
        source="builtin",
    ),
    "coder": AgentDef(
        name="coder",
        description="Reads, edits and runs code and tests.",
        system_prompt=(
            "You are a careful software engineer. Read before you edit, make focused "
            "changes, and run the project's tests to verify. Explain what you changed."
        ),
        tools=[
            "read_file",
            "write_file",
            "list_files",
            "search_files",
            "terminal",
            "learn_personality",
            "skill_list",
            "skill_read",
            "skill_resource",
        ],
        source="builtin",
    ),
}


def _ecc_agents() -> dict[str, AgentDef]:
    """Ship reviewed specialist prompts offline; never execute upstream code."""
    directory = Path(__file__).resolve().parents[1] / "bundled_agents" / "ecc"
    tool_names = {
        "Read": "read_file",
        "Grep": "search_files",
        "Glob": "list_files",
        "Write": "write_file",
        "Edit": "write_file",
        "Bash": "terminal",
        "WebSearch": "web_search",
        "WebFetch": "web_extract",
    }
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    definitions = {}
    for name in manifest["agents"]:
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) or name in BUILTIN_AGENTS:
            raise ValueError("invalid or conflicting bundled ECC agent name")
        raw = (directory / f"{name}.md").read_text(encoding="utf-8")
        _, header, body = raw.split("---", 2)
        metadata = yaml.safe_load(header)
        if metadata["name"] != name or not body.strip():
            raise ValueError(f"invalid bundled ECC agent: {name}")
        upstream = [tool.strip() for tool in metadata["tools"].split(",")]
        tools = list(dict.fromkeys(tool_names[tool] for tool in upstream if tool in tool_names))
        if not tools:
            raise ValueError(f"bundled ECC agent has no supported tools: {name}")
        unavailable = [tool for tool in upstream if tool not in tool_names]
        adaptation = (
            "Machina integration: handle only the delegated task. Use the registered "
            f"harness tool names: {', '.join(tools)}. "
            "Read=read_file; Grep=search_files; Glob=list_files; Write/Edit=write_file; "
            "Bash=terminal; WebSearch=web_search; WebFetch=web_extract. "
            "Permission approval and inherited parent restrictions still apply. "
            "Use the selected session model/provider. Referenced ECC skills, commands, "
            "or platform-specific paths may not be installed; inspect before relying on them. "
        )
        if unavailable:
            adaptation += (
                f"These upstream tools are unavailable: {', '.join(unavailable)}. "
                "Do not claim to have called them; report missing capabilities when needed. "
            )
        definitions[name] = AgentDef(
            name=name,
            description=metadata["description"],
            system_prompt=adaptation + "\n\n" + body.strip(),
            tools=tools,
            source="ecc",
        )
    return definitions


BUILTIN_AGENTS.update(_ecc_agents())


class AgentRegistry:
    """Looks up database, YAML, and built-in definitions in that order."""

    _FILE_DEFINITIONS_TTL = 60  # seconds

    def __init__(self) -> None:
        self._file_definitions_cache: dict[str, AgentDef] | None = None
        self._file_definitions_cache_time: float = 0.0

    def _file_definitions(self) -> dict[str, AgentDef]:
        now = time.monotonic()
        if (
            self._file_definitions_cache is not None
            and now - self._file_definitions_cache_time < self._FILE_DEFINITIONS_TTL
        ):
            return self._file_definitions_cache

        directory = agents_directory()
        if not directory.is_dir():
            self._file_definitions_cache = {}
            self._file_definitions_cache_time = now
            return self._file_definitions_cache
        from machina.soulspec.schema import SoulSpec

        definitions: dict[str, AgentDef] = {}
        for path in sorted((*directory.glob("*.yaml"), *directory.glob("*.yml"))):
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("expected a YAML mapping")
                name = data.get("name", path.stem)
                tools = data.get("tools", [])
                iterations = data.get("max_iterations", 10)
                if not isinstance(name, str) or not name.strip() or len(name) > 100:
                    raise ValueError("name must be a non-empty string of at most 100 characters")
                for key in ("description", "system_prompt"):
                    if not isinstance(data.get(key, ""), str):
                        raise ValueError(f"{key} must be a string")
                if not isinstance(tools, list) or any(not isinstance(t, str) for t in tools):
                    raise ValueError("tools must be a list of strings")
                if type(iterations) is not int or not 1 <= iterations <= 50:
                    raise ValueError("max_iterations must be an integer from 1 to 50")
                for key in ("model", "provider"):
                    if data.get(key) is not None and not isinstance(data[key], str):
                        raise ValueError(f"{key} must be a string")
                if name in BUILTIN_AGENTS:
                    raise ValueError("cannot replace a built-in agent")
                if "persona" in data:
                    if any(
                        key in data
                        for key in ("tools", "model", "provider", "max_iterations", "system_prompt")
                    ):
                        raise ValueError(
                            "SoulSpec YAML must use workflow/config/persona instead of legacy fields"
                        )
                    structured = dict(data, name=name)
                    definition = SoulSpec.from_dict(structured).to_agent_def()
                    definition.source = "yaml"
                    definitions[name] = definition
                    continue
                definitions[name] = AgentDef(
                    name=name,
                    description=data.get("description", ""),
                    system_prompt=data.get("system_prompt", ""),
                    tools=tools,
                    model=data.get("model"),
                    provider=data.get("provider"),
                    max_iterations=iterations,
                    source="yaml",
                )
            except (
                OSError,
                UnicodeError,
                yaml.YAMLError,
                ValueError,
                KeyError,
                TypeError,
                AttributeError,
            ) as exc:
                logger.warning("Skipping agent definition %s: %s", path, exc)
        for manifest in sorted(directory.glob("*/soul.json")):
            try:
                spec = SoulSpec.from_package(manifest.parent)
                if spec.name in BUILTIN_AGENTS or spec.name in definitions:
                    raise ValueError("cannot replace an existing agent definition")
                definition = spec.to_agent_def()
                definition.source = "soulspec"
                definitions[definition.name] = definition
            except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError) as exc:
                logger.warning("Skipping Soul Spec package %s: %s", manifest, exc)
        self._file_definitions_cache = definitions
        self._file_definitions_cache_time = now
        return definitions

    async def get(self, name: str) -> AgentDef | None:
        row = await db.fetchrow(
            """
            SELECT name, description, system_prompt, tools, model, provider, max_iterations, source, soul_spec
            FROM agents WHERE name = $1
            """,
            name,
        )
        if row is not None:
            return self._row_to_def(row)
        found = self._file_definitions().get(name) or BUILTIN_AGENTS.get(name)
        if found is not None:
            return found
        return await self._profile_scope(name)

    @staticmethod
    async def _profile_scope(name: str) -> AgentDef | None:
        """Synthesize a general-purpose definition for a user-profile scope.

        `harness-<slug>` resolves to harness-equivalent tools when a matching
        user profile exists, so per-profile RL, growth notes, and memories
        (all keyed by agent id) work with no schema change and no stored
        definition. Anything else still resolves None, and the agent factory
        keeps failing closed on it. Profile scopes are intentionally absent
        from list(): they are not specialists to delegate to.
        """
        import re

        from machina.memory.user_profile import PROFILE_ID_RE, profile_slug, user_profile_store

        match = re.fullmatch(rf"harness-({PROFILE_ID_RE})", name or "")
        if not match:
            return None
        slug = match.group(1)
        try:
            if await user_profile_store.get_by_user_id(slug) is not None:
                user_id = slug
            else:
                rows = await user_profile_store.list_all(limit=500)
                hit = next((p for p in rows if profile_slug(p.user_id) == slug), None)
                if hit is None:
                    return None
                user_id = hit.user_id
        except Exception:
            return None
        return AgentDef(
            name=name,
            description=f"Profile scope for {user_id} (general tools).",
            source="profile",
        )

    async def list(self) -> list[AgentDef]:
        rows = await db.fetch(
            """
            SELECT name, description, system_prompt, tools, model, provider, max_iterations, source, soul_spec
            FROM agents ORDER BY name
            """
        )
        defs = {**BUILTIN_AGENTS, **self._file_definitions()}
        for row in rows:
            d = self._row_to_def(row)
            defs[d.name] = d  # a stored agent overrides a built-in of the same name
        return sorted(defs.values(), key=lambda d: d.name)

    async def save(self, agent: AgentDef) -> AgentDef:
        """Insert or update a stored agent definition."""
        return await self.save_on_connection(agent, db)

    async def save_on_connection(
        self, agent: AgentDef, connection, *, identity_only: bool = False
    ) -> AgentDef:
        query = """
            INSERT INTO agents (name, description, system_prompt, tools, model, provider, max_iterations, source, soul_spec)
            VALUES ($1, $2, $3, $4, $5, $6, $7, 'db', $8::jsonb)
            ON CONFLICT (name) DO UPDATE SET
                description = EXCLUDED.description,
                system_prompt = EXCLUDED.system_prompt,
                tools = EXCLUDED.tools,
                model = EXCLUDED.model,
                provider = EXCLUDED.provider,
                max_iterations = EXCLUDED.max_iterations,
                soul_spec = COALESCE(EXCLUDED.soul_spec, agents.soul_spec),
                updated_at = now()
            RETURNING name, description, system_prompt, tools, model, provider, max_iterations, source, soul_spec
            """
        if identity_only:
            query = """
            INSERT INTO agents (name, description, system_prompt, tools, model, provider, max_iterations, source, soul_spec)
            VALUES ($1, $2, $3, $4, $5, $6, $7, 'db', $8::jsonb)
            ON CONFLICT (name) DO UPDATE SET
                description = EXCLUDED.description,
                system_prompt = EXCLUDED.system_prompt,
                soul_spec = EXCLUDED.soul_spec,
                updated_at = now()
            RETURNING name, description, system_prompt, tools, model, provider, max_iterations, source, soul_spec
            """
        row = await connection.fetchrow(
            query,
            agent.name,
            agent.description,
            agent.system_prompt,
            json.dumps(agent.tools),
            agent.model,
            agent.provider,
            agent.max_iterations,
            json.dumps(agent.soul_spec) if agent.soul_spec is not None else None,
        )
        return self._row_to_def(row)

    async def delete(self, name: str) -> bool:
        from machina.db.connection import parse_command_count

        result = await db.execute("DELETE FROM agents WHERE name = $1", name)
        return parse_command_count(result) > 0

    def _row_to_def(self, row: Any) -> AgentDef:
        tools = row["tools"]
        if isinstance(tools, str):
            tools = json.loads(tools)
        return AgentDef(
            name=row["name"],
            description=row["description"],
            system_prompt=row["system_prompt"],
            tools=list(tools or []),
            model=row["model"],
            provider=row["provider"],
            max_iterations=row["max_iterations"],
            source=row["source"],
            soul_spec=json.loads(row["soul_spec"])
            if isinstance(row.get("soul_spec"), str)
            else row.get("soul_spec"),
        )


agent_registry = AgentRegistry()
