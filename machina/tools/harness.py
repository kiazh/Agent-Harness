"""Read-only, session-bound inspection of harness settings and learning state."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from machina.core.exceptions import ToolError
from machina.core.self_knowledge import FEATURES, configuration
from machina.core.session import session_manager
from machina.memory.growth import growth_store
from machina.memory.redaction import redact_value
from machina.observability.diagnostics import record_failure
from machina.tools.agents import active_agent_scope
from machina.tools.base import registry

_MAX_STATUS_BYTES = 8500


@registry.register(
    name="harness_status",
    description="Read current harness configuration, persona, and this agent's saved learning/RSI state. Use for live feature/settings questions; read-only, no secrets or other agents' state.",
    parameters={"type": "object", "properties": {}, "additionalProperties": False},
)
async def harness_status() -> str:
    async def snapshot() -> dict:
        agent_id, session_id = await active_agent_scope()
        session = await session_manager.get(session_id)
        if session is None or session.agent_id != agent_id:
            raise ToolError("harness_status requires an owned active session")
        from machina.core.config import config

        growth = await growth_store.get(agent_id)
        rsi = await growth_store.rsi_status(agent_id)
        installed = _installed_inventory()
        persona = await _persona_summary(agent_id, growth["notes"])
        return {
            "read_only": True,
            "session": {
                "agent_id": agent_id,
                "session_id": str(session_id),
                "mode": session.execution_mode,
            },
            "configuration": configuration(session),
            "persona_description": config.get("persona_description"),
            "effective_persona": persona,
            "learning": {
                "notes": growth["notes"],
                "note_count": len(growth["notes"]),
                "interactions": growth["interactions"],
                "human_feedback_count": growth["feedback_count"],
                "rsi": {
                    key: rsi[key]
                    for key in (
                        "enabled",
                        "revision",
                        "trial",
                        "observations",
                        "automatic_observations",
                        "human_ratings",
                    )
                    if key in rsi
                },
            },
            "features": FEATURES,
            "installed": installed,
        }

    try:
        result = await asyncio.wait_for(snapshot(), timeout=2)
    except ToolError:
        raise
    except Exception:
        # Driver failures can contain credential-bearing DSNs; never echo them.
        raise ToolError("Harness status unavailable; current state could not be verified") from None
    safe = redact_value(result)

    def _shrink() -> str:
        return json.dumps(safe, ensure_ascii=False, separators=(",", ":"))

    # Truncation ladder, cheapest context first. The final guard refuses
    # rather than emitting truncated JSON; every omitted field is marked so
    # absence is never misread as emptiness.
    serialized = _shrink()
    if len(serialized) > _MAX_STATUS_BYTES:
        # Keep JSON valid for the agent's bounded tool-message budget. Never
        # turn omitted notes into evidence that learning has no saved state.
        safe["learning"]["notes"] = {}
        safe["learning"]["notes_omitted"] = "Inspect full notes with /growth show"
        serialized = _shrink()
    if len(serialized) > _MAX_STATUS_BYTES:
        safe["persona_description"] = None
        safe["persona_description_omitted"] = "Inspect description in the /persona Add editor"
        serialized = _shrink()
    if len(serialized) > _MAX_STATUS_BYTES and safe["learning"]["rsi"].get("trial") is not None:
        safe["learning"]["rsi"]["trial"] = None
        safe["learning"]["rsi"]["trial_omitted"] = "Inspect full trial with /growth show"
        serialized = _shrink()
    if len(serialized) > _MAX_STATUS_BYTES and "installed" in safe:
        installed = safe["installed"]
        for source, entry in (installed.get("skills_by_source") or {}).items():
            if isinstance(entry, dict) and entry.pop("names", None) is not None:
                installed.setdefault("installed_omitted", []).append(source)
        serialized = _shrink()
    if len(serialized) > _MAX_STATUS_BYTES:
        # Corrupt/legacy state must not defeat the tool-message bound and yield
        # truncated JSON. Refuse safely instead of implying omitted data is empty.
        raise ToolError("Harness status exceeds the response limit; inspect /persona and /growth")
    return serialized


def _installed_inventory() -> dict:
    """Bounded skill inventory by provenance for self-knowledge questions.

    Bundled skills are reported by count only (hundreds of names would
    exhaust the tool-message budget); custom sources list names. Skills
    installed from repo links carry an install manifest: they report as
    url provenance with their source link instead of merging into local.
    A registry failure is marked unavailable rather than reported as an
    empty inventory.
    """
    try:
        import machina.skills.registry as skills_module

        registry = skills_module.skill_registry
        registry.load_all()
        by_source: dict[str, list[str]] = {}
        for skill in registry.list_skills():
            if not skill.enabled:
                continue
            by_source.setdefault(_provenance_of(registry, skill), []).append(skill.name)
        inventory: dict[str, dict] = {}
        for source, names in sorted(by_source.items()):
            entry: dict = {"count": len(names)}
            if source != "builtin-ecc":
                entry["names"] = sorted(names)[:30]
            inventory[source] = entry
        return {"skills_by_source": inventory}
    except Exception:
        return {"skills_by_source": {}, "inventory_unavailable": True}


def _provenance_of(registry, skill) -> str:
    """Resolve display provenance, consulting install manifests for locals."""
    if (skill.source_type or "local") != "local":
        return skill.source_type
    candidates = {Path(skill.file_path).parent}
    try:
        candidates.add(registry.skills_dir / skill.name.lower())
    except (OSError, ValueError):
        pass
    for directory in candidates:
        try:
            manifest = json.loads(
                (directory / ".install-manifest.json").read_text(encoding="utf-8")
            )
        except (ValueError, OSError):
            continue
        source = manifest.get("source", "")
        if isinstance(source, str) and source.startswith("https://github.com/"):
            return f"url:{source}"
    return "local"


async def _persona_summary(agent_id: str, notes: dict) -> dict:
    """Bounded, owned identity status.

    "No agent definition" and "resolution failed" are distinct typed states
    (``reason`` is machine-readable, never the exception text), so an
    unreadable persona is never reported as an absent one.
    """
    try:
        from machina.core.agent_def import agent_registry
        from machina.core.config import config
        from machina.soulspec.adapters import MachinaAdapter
        from machina.soulspec.runtime import resolve_persona

        definition = await agent_registry.get(agent_id)
        if definition is None:
            return {"available": False, "reason": "no_agent_definition"}
        spec = MachinaAdapter.to_soulspec(definition)
        effective = resolve_persona(
            spec,
            requested_style=config.get("persona_description")
            or (spec.persona.requested_style if spec.persona else ""),
            notes=notes,
            growth_enabled=bool(
                config.get("memory_enabled") and config.get("personality_growth_enabled")
            ),
        )
        p = effective.persona
        return {
            "available": True,
            "format": "SoulSpec",
            "agent_id": agent_id,
            "name": p.name[:100],
            "source": definition.source,
            "values_count": len(p.values),
            "traits_count": len(p.traits),
            "voice": {"tone": str(p.voice.tone)[:100], "style": str(p.voice.style)[:100]}
            if p.voice
            else None,
            "human_style_set": bool(p.requested_style),
            "active_learned_notes": len(p.learned_notes),
            "learning_enabled": effective.learning_enabled,
        }
    except Exception as error:
        # Resolution failure is a distinct typed state; the exception text is
        # never echoed (driver errors can carry credential-bearing details).
        record_failure("tools.harness.persona", error)
        return {"available": False, "reason": "persona_resolution_failed"}
