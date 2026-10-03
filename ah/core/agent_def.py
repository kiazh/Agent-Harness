"""Agent definitions and registry (Phase 5: multi-agent).

An :class:`AgentDef` is a named persona: a system prompt, an optional tool
allow-list, and model/provider overrides. Definitions live in the ``agents``
table; a few built-ins are always available. The registry is the single place
the rest of the system looks them up.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ah.db.connection import db

__all__ = ["AgentDef", "AgentRegistry", "agent_registry", "BUILTIN_AGENTS"]


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
    """Looks up agent definitions from the ``agents`` table and the built-ins."""

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
        return BUILTIN_AGENTS.get(name)

    async def list(self) -> list[AgentDef]:
        rows = await db.fetch(
            """
            SELECT name, description, system_prompt, tools, model, provider, max_iterations, source
            FROM agents ORDER BY name
            """
        )
        defs = {name: d for name, d in BUILTIN_AGENTS.items()}
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
