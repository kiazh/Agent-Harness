"""Feature operations shared by the CLI and the gateway.

Each function does the work and returns data (or raises ``ServiceError`` with a
user-facing message); callers decide how to present it.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from ah.core.compression import CompressionConfig, CompressionResult, ContextCompressor
from ah.core.config import config
from ah.core.context import context_manager
from ah.core.models import Session
from ah.db.connection import db

__all__ = [
    "ServiceError",
    "compress_session",
    "export_markdown",
    "learn_skill",
    "status_summary",
]


class ServiceError(Exception):
    """A failure with a message that is safe to show the user."""


async def export_markdown(session: Session) -> str:
    """Render a session's conversation as Markdown."""
    chunks = await context_manager.get_chunks(session.id, limit=1000)
    lines = [
        f"# Session: {session.title or '(untitled)'}",
        "",
        f"**ID:** {session.id}",
        f"**Status:** {session.status}",
        f"**Agent:** {session.agent_id}",
        f"**Goal:** {session.goal or '(none)'}",
        f"**Created:** {session.created_at.strftime('%Y-%m-%d %H:%M:%S')}",
        f"**Last Activity:** {session.last_activity.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## Conversation",
        "",
    ]
    for chunk in reversed(chunks):
        payload = chunk.payload
        if chunk.chunk_type == "user_message":
            lines += ["### User", "", str(payload.get("content", "")), ""]
        elif chunk.chunk_type == "assistant_message":
            lines += ["### Assistant", "", str(payload.get("content", "")), ""]
        elif chunk.chunk_type == "compression_summary":
            lines += ["### Summary of earlier context", "", str(payload.get("content", "")), ""]
        elif chunk.chunk_type == "tool_call":
            lines.append(f"**Tool:** `{payload.get('tool', 'unknown')}({payload.get('args', {})})`")
            preview = payload.get("result_preview", "")
            if preview:
                lines.append(f"**Result:** {str(preview)[:200]}")
            lines.append("")
    return "\n".join(lines)


def _compression_config() -> CompressionConfig:
    return CompressionConfig(
        enabled=config.get("compression_enabled"),
        threshold=config.get("compression_threshold"),
        target_ratio=config.get("compression_target_ratio"),
        preserve_recent=config.get("compression_preserve_recent"),
        llm_summarize=config.get("compression_llm_summarize"),
    )


async def compress_session(
    session: Session,
    *,
    model: str | None = None,
    provider: str | None = None,
) -> CompressionResult | None:
    """Compress a session's context in place.

    Returns ``None`` when there is nothing to compress. Uses LLM summarization
    when enabled and a provider can be built, otherwise truncation.
    """
    chunks = await context_manager.get_chunks(session.id, limit=1000)
    if not chunks:
        return None

    comp_config = _compression_config()
    llm_provider = None
    if comp_config.llm_summarize:
        from ah.core.provider import get_provider

        try:
            llm_provider = get_provider(
                provider=provider or config.get("provider"), model=model or config.get("model")
            )
        except Exception:
            llm_provider = None  # no key / provider: fall back to truncation

    result = ContextCompressor(config=comp_config).compress(
        chunks=chunks,
        session_id=session.id,
        agent_id=session.agent_id,
        llm_provider=llm_provider,
    )
    if result.original_count == 0:
        return None
    await context_manager.replace_chunks(session.id, result.compressed_chunks)
    return result


def learn_skill(
    source: str,
    *,
    name: str | None = None,
    description: str | None = None,
    triggers: list[str] | None = None,
):
    """Create a skill from a file path, an http(s) URL, or an existing skill name."""
    from ah.skills.registry import skill_registry
    from ah.tools.builtins import _is_safe_url

    skill_registry.load_all()
    path = Path(source).expanduser()

    if path.is_file():
        try:
            content = path.resolve().read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise ServiceError(f"Cannot learn from binary or non-UTF-8 file: {path.name}") from None
        except OSError as e:
            raise ServiceError(f"Cannot read file: {e}") from None
        default_name, default_desc = path.stem, f"Skill learned from {path.name}"
        default_triggers: list[str] = []
    elif source.startswith(("http://", "https://")):
        if not _is_safe_url(source):
            raise ServiceError(
                f"URL rejected by security policy (private/internal address or invalid protocol): {source}"
            )
        import httpx

        try:
            resp = httpx.get(source, timeout=30)
            resp.raise_for_status()
        except Exception as e:
            raise ServiceError(f"Failed to fetch URL: {e}") from None
        content = resp.text
        default_name = source.rstrip("/").split("/")[-1].split(".")[0] or "web-skill"
        default_desc = f"Skill learned from {source}"
        default_triggers = []
    else:
        existing = skill_registry.get(source)
        if existing is None:
            raise ServiceError(
                f"Source not found: {source}. Provide a file path, URL, or existing skill name."
            )
        content = existing.content
        default_name, default_desc = (
            f"{existing.name}-learned",
            f"Skill derived from {existing.name}",
        )
        default_triggers = existing.triggers[:]

    try:
        return skill_registry.create_skill(
            name=name or default_name,
            description=description or default_desc,
            content=content,
            triggers=triggers if triggers is not None else default_triggers,
            source=source,
            source_type="learned",
        )
    except (ValueError, FileExistsError) as e:
        raise ServiceError(f"Skill rejected: {e}") from None


async def status_summary() -> dict[str, Any]:
    """Database, counts and configuration health. Requires a connected ``db``."""
    from ah.tools.base import registry

    version = await db.fetchval("SELECT version()")
    return {
        "postgres": version.split(",")[0],
        "sessions": await db.fetchval("SELECT COUNT(*) FROM sessions"),
        "contextChunks": await db.fetchval("SELECT COUNT(*) FROM context_chunks"),
        "memories": await db.fetchval("SELECT COUNT(*) FROM memories"),
        "pendingMemories": await db.fetchval(
            "SELECT COUNT(*) FROM pending_memories WHERE status = 'pending'"
        ),
        "tools": registry.list_tools(),
        "openrouterKeySet": bool(config.get("openrouter_api_key")),
        "model": config.get("model"),
        "provider": config.get("provider"),
    }

def parse_session_id(value: Any) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        raise ServiceError(f"Invalid session ID: {value}") from None
