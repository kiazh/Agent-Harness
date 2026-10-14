"""Small implementation brief and safe current flags, shared by prompts and inspection.

No filesystem, database, provider requests or secret configuration enumeration.
Feature facts are distinct from configuration and observed learning evidence.
"""

from __future__ import annotations

import json

from machina.core.config import config
from machina.core.models import Session
from machina.core.session_mode import MODES
from machina.memory.persona import EmotionTopology

FEATURES = {
    "persona": "SoulSpec unifies identity, /persona tone (persona_description), owned lessons and RSI guidance. Agent id means role/tooling scope, not persona. persona_memory_enabled adds fact interpretations and emotion-weighted recall via persona_default_emotion; retrieval settings, not feelings.",
    "growth": "Per-agent preferences, style and verified lessons persist across sessions/models (learn_personality; /growth). empty notes mean no notes currently saved, not a missing feature.",
    "rsi": "RSI adapts retrieval, relevant skill ranks, allowed-tool order and approach from weak automatic execution outcomes even with zero human ratings. Agent-written persona trials separately need three human-rated episodes to stabilize or roll back. /growth inspects revisions/evidence. No model weights, code rewriting or permission expansion; no fixed number of turns guarantees improvement.",
    "memory": "remember/recall retrieve facts; session_recall searches conversations, not implementation documentation. RAG searches explicitly indexed documents.",
    "web": "web_research saves fetched public pages with source URLs to session RAG for later search_documents retrieval; research is session-scoped.",
    "skills": "Relevant bundled workflows activate automatically with bounded context; skill tools discover/read more when offered. Skills do not grant permissions.",
    "install": "repo_stage shallow-clones a GitHub skills repo into a digest-bound review plan; repo_activate installs validated skills for that approved digest. Never runs tests/installers/hooks/package installs. MCP drafts need human /mcp review; third-party agents/commands are inventory only.",
    "execution": "Offered tools define access. Delegation inherits authority and returns results through the parent, without live sibling peer chat; share_memory delivers signed memories. Jobs, sessions, compaction and usage budgets are supported.",
    "controls": "TUI /persona, /growth, /profile, /sessions, /models, /provider, /jobs, /usage and /mode manage the respective features; these are UI commands, not model tools.",
    "mcp": "TUI /mcp saves servers and explicitly connects/disconnects; the agent's mcp_connect/mcp_disconnect add/remove servers through exact approval. mcp_list/mcp_describe list servers and catalogs; mcp_call requires exact approval unless trusted-read is configured. HTTP servers support browser OAuth with PKCE; authorization does not grant tool execution permission.",
    "images": "The TUI accepts dropped PNG/JPEG/GIF/WebP files and clipboard images (Ctrl+V or forwarded Cmd+V), displayed as [image 1] markers. Inline image content is sent to the provider and retained in recent conversation context. Image understanding requires a vision-capable model; markers alone do not prove an image was supplied.",
}


def configuration(session: Session) -> dict:
    """Allowlisted knobs only; enabled configuration does not prove readiness."""
    memory = bool(config.get("memory_enabled"))
    growth = memory and bool(config.get("personality_growth_enabled"))
    emotion = config.get("persona_default_emotion")
    # This string setting is not enum-validated on input. Never expose arbitrary
    # text in the always-on prompt; unknown values use the topology's neutral profile.
    emotion = emotion.strip().lower() if isinstance(emotion, str) else ""
    if emotion not in EmotionTopology.EMOTION_PROFILES:
        emotion = "unrecognized (neutral fallback)"
    mode = getattr(session, "execution_mode", None)
    mode = mode if mode in MODES else "unknown"
    return {
        "memory": memory,
        "growth": growth,
        "rsi": growth and bool(config.get("rsi_enabled")),
        "persona_lenses": bool(config.get("persona_memory_enabled")),
        "default_emotion": emotion,
        "persona_set": bool(config.get("persona_description")),
        "learning_review": bool(config.get("learning_review_enabled")),
        "rag": bool(config.get("rag_enabled")),
        "compaction": bool(config.get("compression_enabled")),
        "mode": mode,
    }


def capability_context(session: Session, *, tool_names: set[str], growth_state: str) -> str:
    """Always explain features, even with a custom prompt or failed growth store."""
    facts = "\n".join(f"{name}: {description}" for name, description in FEATURES.items())
    flags = {**configuration(session), "growth_turn": growth_state}
    inspection = (
        " Use harness_status only for requested live settings or learning state."
        if "harness_status" in tool_names
        else " Live inspection is unavailable to this agent; offer the relevant UI command."
    )
    return (
        "\n\nHarness capabilities (implementation facts):\n"
        + facts
        + "\nCurrent configuration (enabled is not proof of successful learning): "
        + json.dumps(flags, separators=(",", ":"))
        + "\nAnswer feature questions directly from this brief. Do not infer features "
        "from recalled chat or empty notes, claim unobserved improvement, or invent unavailable tools."
        + inspection
    )
