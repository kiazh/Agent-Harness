"""Session-owned inspection and deliberate export/import of the unified persona."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path

from machina.core.agent_def import AgentDef, agent_registry
from machina.core.config import config
from machina.memory.growth import growth_store, require_growth_enabled
from machina.soulspec.adapters import MachinaAdapter
from machina.soulspec.runtime import resolve_persona
from machina.soulspec.schema import SoulSpec


async def effective_persona(agent_id: str, *, include_learned: bool = False) -> SoulSpec:
    definition = await agent_registry.get(agent_id)
    if definition is None:
        raise ValueError("agent definition unavailable")
    spec = MachinaAdapter.to_soulspec(definition)
    if spec.persona is None:
        spec.persona = SoulSpec.Persona(name=agent_id)
    if not spec.persona.system_prompt:
        from machina.core.agent import SYSTEM_PROMPT

        spec.persona.system_prompt = SYSTEM_PROMPT
    if not spec.persona.description:
        spec.persona.description = definition.description or "Machina agent persona"
    notes = (await growth_store.get(agent_id))["notes"] if include_learned else {}
    return resolve_persona(
        spec,
        requested_style=config.get("persona_description") or spec.persona.requested_style,
        notes=notes,
        growth_enabled=include_learned,
    )


async def export_persona(
    agent_id: str, directory: Path, *, include_learned: bool = False
) -> SoulSpec:
    if include_learned:
        require_growth_enabled()
    spec = await effective_persona(agent_id, include_learned=include_learned)
    # Identity ownership remains the runtime agent_id. Portable package names
    # obey the standard's kebab-case grammar, including legacy agent names.
    spec.name = re.sub(r"[^a-z0-9]+", "-", agent_id.lower()).strip("-") or "machina-agent"
    spec.write_package(directory, author="Machina user")
    return spec


async def import_persona(
    agent_id: str, directory: Path, *, restore_learned: bool = False
) -> AgentDef:
    """Human control-plane import: keep agent key, model and execution authority."""
    from machina.db.connection import db
    from machina.memory.growth import GrowthStore

    spec = SoulSpec.from_package(directory)
    notes = dict(spec.persona.learned_notes) if spec.persona else {}
    if restore_learned:
        require_growth_enabled()
    existing = await agent_registry.get(agent_id)
    if existing is None:
        raise ValueError("agent definition unavailable")
    spec = deepcopy(spec)
    spec.name = agent_id
    spec.persona.learned_notes = {}  # mutable lessons belong to the owned growth store
    definition = spec.to_agent_def()
    definition.tools = list(existing.tools)
    definition.model = existing.model
    definition.provider = existing.provider
    # Reconcile snapshot permissions with the actual definition, never treating
    # a persona import as an authority or provider-configuration grant.
    definition.soul_spec = MachinaAdapter.to_soulspec(definition).to_storage_dict()
    async with db.acquire() as conn, conn.transaction():
        from machina.core.agent_def import AgentRegistry

        # The registry and growth store share one connection for atomic restore.
        stored = await AgentRegistry().save_on_connection(definition, conn, identity_only=True)
        stored.soul_spec = MachinaAdapter.to_soulspec(stored).to_storage_dict()
        await conn.execute(
            "UPDATE agents SET soul_spec = $2::jsonb WHERE name = $1",
            agent_id,
            json.dumps(stored.soul_spec),
        )
        if restore_learned:
            await GrowthStore(conn).replace_notes_on_connection(agent_id, notes, conn)
    return stored
