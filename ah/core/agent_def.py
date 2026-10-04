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
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ah.db.connection import db

__all__ = ["AgentDef", "AgentRegistry", "agent_registry", "BUILTIN_AGENTS"]

logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def agents_directory() -> Path:
    """Directory containing local YAML agent definitions."""
    return Path(os.environ.get("AGENT_HARNESS_AGENTS_DIR") or _PROJECT_ROOT / "agents")


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
            "search_documents",
            "recall",
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
        tools=["read_file", "write_file", "list_files", "search_files", "terminal"],
        source="builtin",
    ),
}


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
            return {}
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
            except (OSError, UnicodeError, yaml.YAMLError, ValueError) as exc:
                logger.warning("Skipping agent definition %s: %s", path, exc)
        self._file_definitions_cache = definitions
        self._file_definitions_cache_time = now
        return definitions

    async def get(self, name: str) -> AgentDef | None:
        row = await db.fetchrow(
            """
            SELECT name, description, system_prompt, tools, model, provider, max_iterations, source
            FROM agents WHERE name = $1
            """,
            name,
        )
        if row is not None:
            return self._row_to_def(row)
        return self._file_definitions().get(name) or BUILTIN_AGENTS.get(name)

    async def list(self) -> list[AgentDef]:
        rows = await db.fetch(
            """
            SELECT name, description, system_prompt, tools, model, provider, max_iterations, source
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
        row = await db.fetchrow(
            """
            INSERT INTO agents (name, description, system_prompt, tools, model, provider, max_iterations, source)
            VALUES ($1, $2, $3, $4, $5, $6, $7, 'db')
            ON CONFLICT (name) DO UPDATE SET
                description = EXCLUDED.description,
                system_prompt = EXCLUDED.system_prompt,
                tools = EXCLUDED.tools,
                model = EXCLUDED.model,
                provider = EXCLUDED.provider,
                max_iterations = EXCLUDED.max_iterations,
                updated_at = now()
            RETURNING name, description, system_prompt, tools, model, provider, max_iterations, source
            """,
            agent.name,
            agent.description,
            agent.system_prompt,
            json.dumps(agent.tools),
            agent.model,
            agent.provider,
            agent.max_iterations,
        )
        return self._row_to_def(row)

    async def delete(self, name: str) -> bool:
        from ah.db.connection import parse_command_count

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
        )


agent_registry = AgentRegistry()
