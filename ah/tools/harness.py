"""Read-only, session-bound inspection of harness settings and learning state."""

from __future__ import annotations

import asyncio
import json

from ah.core.exceptions import ToolError
from ah.core.self_knowledge import FEATURES, configuration
from ah.core.session import session_manager
from ah.memory.growth import growth_store
from ah.memory.redaction import redact_value
from ah.tools.agents import active_agent_scope
from ah.tools.base import registry


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
        from ah.core.config import config

        growth = await growth_store.get(agent_id)
        rsi = await growth_store.rsi_status(agent_id)
        return {
            "read_only": True,
            "session": {
                "agent_id": agent_id,
                "session_id": str(session_id),
                "mode": session.execution_mode,
            },
            "configuration": configuration(session),
            "persona_description": config.get("persona_description"),
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
        }

    try:
        result = await asyncio.wait_for(snapshot(), timeout=2)
    except ToolError:
        raise
    except Exception:
        # Driver failures can contain credential-bearing DSNs; never echo them.
        raise ToolError("Harness status unavailable; current state could not be verified") from None
    safe = redact_value(result)
    serialized = json.dumps(safe, ensure_ascii=False, separators=(",", ":"))
    if len(serialized) > 8500:
        # Keep JSON valid for the agent's bounded tool-message budget. Never
        # turn omitted notes into evidence that learning has no saved state.
        safe["learning"]["notes"] = {}
        safe["learning"]["notes_omitted"] = "Inspect full notes with /growth show"
        serialized = json.dumps(safe, ensure_ascii=False, separators=(",", ":"))
    if len(serialized) > 8500:
        safe["persona_description"] = None
        safe["persona_description_omitted"] = "Inspect description in the /persona Add editor"
        serialized = json.dumps(safe, ensure_ascii=False, separators=(",", ":"))
    if len(serialized) > 8500 and safe["learning"]["rsi"].get("trial") is not None:
        safe["learning"]["rsi"]["trial"] = None
        safe["learning"]["rsi"]["trial_omitted"] = "Inspect full trial with /growth show"
        serialized = json.dumps(safe, ensure_ascii=False, separators=(",", ":"))
    if len(serialized) > 8500:
        # Corrupt/legacy state must not defeat the tool-message bound and yield
        # truncated JSON. Refuse safely instead of implying omitted data is empty.
        raise ToolError("Harness status exceeds the response limit; inspect /persona and /growth")
    return serialized
