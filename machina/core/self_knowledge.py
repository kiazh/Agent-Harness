"""Small implementation brief and safe current flags, shared by prompts and inspection.

No filesystem, database, provider requests or secret configuration enumeration.
Feature facts are distinct from configuration and observed learning evidence.

Style: 1-3 terse sentences per entry; the brief is bounded (prompt token pin and
``harness_status`` response cap), so trim before adding. snake_case tokens are
configuration keys or registered tool names, pinned against the real
inventories by ``tests/test_self_knowledge_completeness.py``; never add an
invented name.
"""

from __future__ import annotations

import json

from machina.core.config import config
from machina.core.models import Session
from machina.core.session_mode import MODES
from machina.memory.persona import EmotionTopology
from machina.workspace.service import (
    CODE_INTELLIGENCE_OPERATIONS,
    active_workspace_service,
)

# Keep this map lean: ``harness_status`` serializes a copy alongside live
# learning state inside its 8,500-byte budget, and the truncation ladder drops
# sections in this order: installed skill names -> this map -> workspace
# profiles -> learning notes -> persona/trial. Learning notes are the least
# recoverable section, so this map is sized to leave headroom for them instead
# of forcing their omission. Add a fact only if it displaces prose here.
FEATURES = {
    "persona": "SoulSpec unifies identity, /persona tone (persona_description), lessons and RSI guidance; agent id is role/tooling scope, not persona. persona_memory_enabled adds fact interpretations and emotion-weighted recall via persona_default_emotion.",
    "growth": "Per-agent preferences, style and verified lessons persist across sessions/models (learn_personality; /growth). empty notes mean none saved, not a missing feature.",
    "rsi": "RSI automatically adapts retrieval, skill ranks, tool order and approach from weak outcomes; persona trials need three human-rated episodes to stabilize or roll back (/growth shows evidence). No model weights, code or permission rewriting; more turns are not proof.",
    "memory": "remember/recall retrieve durable facts; session_recall searches conversations (not docs); search_documents searches indexed RAG documents. memory_approval_enabled routes new facts to pending queues (/memory pending|approve|reject; `machina memory-pending|memory-approve|memory-reject`); /profile and /profiles manage profiles.",
    "sessions": "Sessions persist conversation, model and goal; /sessions and /resume reopen one, /new starts one, /search finds titles, /recall searches past text; /rename, /goal, /fork, /delete, /export manage the rest; CLI: `machina sessions`.",
    "context": "Turns select evidence, not replay; /context shows retained context, /compress and `machina compress` summarize older context, `machina context` lists chunks. Compaction: compression_enabled, auto_compaction_enabled, compression_threshold.",
    "web": "web_search finds links without fetching; web_research saves public pages with source URLs to session RAG; web_crawl crawls sites with bounds. The TUI manages the stack (firecrawl_autostart, firecrawl_stop_on_exit, firecrawl_home).",
    "skills": "First-party and ECC skills activate by request, trigger or task evidence with bounded context; skill tools read more when offered. /skills list|learn|proposals|approve|reject|revert|delete|curator and `machina learn|hub|curator` manage them. Skills grant no permissions.",
    "agents": "Delegated subagents (ECC roles plus harness/researcher/coder) inherit the parent's authority and allowed tools; /agents list|show|save|history|delete manages definitions and sessions, /delegate picks one. A role grants no new permissions.",
    "jobs": "Jobs run interval prompts, heartbeats, UTC-cron and scripts; /jobs list|add|script|heartbeat|cron|on|off|delete manages them. Consent-needing jobs pause, never self-grant.",
    "usage": "`machina usage` and /usage show recorded tokens/requests; budgets: usage_session_token_limit, usage_session_request_limit, usage_agent_token_limit, usage_agent_request_limit (0 = unlimited).",
    "models": "Models/providers: /models picks from catalogs, /default-model saves a default, /provider switches or adds OpenAI-compatible providers, /effort sets reasoning_effort; `machina providers` and /keys manage the inventory (keys in a git-ignored .env, 0600).",
    "auth": "/auth signs in the ChatGPT account; `machina auth chatgpt|copilot|status|logout` covers subscription sign-in, Copilot device flow and token removal. Never edits harness auth files; Codex imports are one-way.",
    "theme": "/theme previews and saves one of 53 UI skins (theme); Esc restores the previous skin.",
    "modes": "Modes ask|workspace|sandbox|full: /mode and `machina mode` set them; full is a session grant revoked via /approvals. /approvals list|allow|deny|revoke manages pending consent.",
    "controls": "TUI /help lists commands; /config and /settings manage non-secret settings (--save persists), /status reports health, /init writes AGENTS.md, /review reviews changes. Slash commands are UI actions, not model tools.",
    "self_change": "When asked, the agent may change this installation within its offered tools and permissions: `machina config-set` (non-secret settings), the slash-command surface (/config, /theme, /models, /mode, /keys, /mcp, /skills, /memory, /harnesses), or permission-gated edits. Autonomous RSI never rewrites weights, code or permissions; user-owned choices are asked, not guessed.",
    "install": "repo_stage builds a digest-bound skills review plan; repo_activate installs reviewed skills. Never runs tests/installers/hooks/package managers. Skill metadata generates once at install. MCP drafts need /mcp review.",
    "execution": "Offered tools define access; approval-gated actions follow /mode and /approvals. Delegation inherits the parent's authority and returns through the parent, without sibling peer chat; share_memory shares signed memories.",
    "mcp": "TUI /mcp manages servers (save/connect/disconnect); mcp_connect/mcp_disconnect add/remove servers. mcp_list/mcp_describe cover catalogs; mcp_call requires exact approval unless trusted-read. HTTP servers support browser OAuth with PKCE; authorization grants no execution permission.",
    "harnesses": 'External agent harnesses (OpenCode, Codex, Hermes) are detected installed/running/registered; `machina harnesses status|enable|disable|setup|run` manages them and registers Machina\'s MCP server (state ~/.machina/interop.json; `interop` and /interop alias it). "connect to opencode" means `machina harnesses enable opencode`; `machina harnesses run` delegates (TUI /harnesses; chat: approval-gated). Never touches their auth files.',
    "images": "The TUI accepts dropped PNG/JPEG/GIF/WebP or clipboard images (Ctrl+V), sent inline to the provider as [image 1] markers and kept in recent context. Understanding needs a vision-capable model; markers alone do not prove an image was supplied.",
    "music": "A pinned bar shows what is playing (/music: play/pause, next/previous, on|off; nowplaying_enabled). Detection is local and best-effort (system media APIs, Spotify/Music, a front YouTube tab); permission prompts are reported, never guessed.",
    "workspace": "The terminal has an editor and optional language servers (/workspace, /editor, /explorer, /lsp, /problems, /quickopen). code_intelligence answers diagnostics, definition, references, symbols or hover for one authorized file: source analysis, not runtime or test success; server text is untrusted data. Unsaved content is shared only when attached (/attach); a visible pane grants no edit, command or file permission.",
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
        "workspace": _workspace_summary(),
    }


def _workspace_summary() -> object:
    """Live workspace facts for this process, never file or buffer text.

    Computed from the active service's truth (``status_facts``): availability,
    root *label* (basename only — full private paths stay out of the prompt),
    per-language server states and the operation names the read-only
    ``code_intelligence`` tool exposes. ``"none"`` when no workspace is open
    in this process; a failure to read live facts is reported as
    ``"unavailable"`` rather than as an absent workspace.
    """
    try:
        service = active_workspace_service()
        facts = service.status_facts() if service is not None else None
    except Exception:
        return "unavailable"
    if facts is None:
        return "none"
    return {
        "available": True,
        "root_label": facts["root_label"],
        "trusted": facts["trusted"],
        "profiles": {language: row["state"] for language, row in facts["profiles"].items()},
        "operations": list(CODE_INTELLIGENCE_OPERATIONS),
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
