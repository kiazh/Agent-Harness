"""Feature operations shared by the CLI and the gateway.

Each function does the work and returns data (or raises ``ServiceError`` with a
user-facing message); callers decide how to present it.
"""

from __future__ import annotations

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
    """Render a session's conversation as Markdown.

    Policy (AH-002): exports are redacted — secret-looking strings are
    replaced before rendering so raw transcripts never leak via
    session.export / /rpc. The stored chunks are unchanged.
    """
    from ah.memory.redaction import redact_secrets as _redact

    chunks = await context_manager.get_all_chunks(session.id)
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
            lines += ["### User", "", _redact(str(payload.get("content", ""))).text, ""]
        elif chunk.chunk_type == "assistant_message":
            lines += ["### Assistant", "", _redact(str(payload.get("content", ""))).text, ""]
        elif chunk.chunk_type == "compression_summary":
            lines += [
                "### Summary of earlier context",
                "",
                _redact(str(payload.get("content", ""))).text,
                "",
            ]
        elif chunk.chunk_type == "tool_call":
            lines.append(
                f"**Tool:** `{_redact(str(payload.get('tool', 'unknown'))).text}({_redact(str(payload.get('args', {}))).text})`"
            )
            preview = payload.get("result_preview", "")
            if preview:
                lines.append(f"**Result:** {_redact(str(preview)[:200]).text}")
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
    # AH-008: fetch ALL chunks (paginated), not just the newest 1000, and
    # replace only the captured input IDs so concurrent inserts survive.
    chunks = await context_manager.get_all_chunks(session.id)
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

    # AH-011: await summarization directly instead of blocking the event loop
    # via Future.result() on a worker thread.
    from ah.core.compression import ContextCompressor as _CC

    compressor = _CC(config=comp_config)
    if hasattr(compressor, "acompress"):
        result = await compressor.acompress(
            chunks=chunks,
            session_id=session.id,
            agent_id=session.agent_id,
            llm_provider=llm_provider,
        )
    else:
        result = ContextCompressor(config=comp_config).compress(
            chunks=chunks,
            session_id=session.id,
            agent_id=session.agent_id,
            llm_provider=llm_provider,
        )
    if result.original_count == 0:
        return None
    # AH-008/AH-009: replace only captured IDs; originals are archived
    # transactionally inside replace_chunks_by_ids.
    try:
        await context_manager.replace_chunks_by_ids(
            session.id,
            [c.id for c in chunks],
            result.compressed_chunks,
            archive_reason="compressed",
        )
    finally:
        # Do not leak a compression-only provider client.
        close = getattr(llm_provider, "close", None)
        if callable(close):
            try:
                import inspect as _inspect

                r = close()
                if _inspect.isawaitable(r):
                    await r
            except Exception:
                pass
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

        # AH-005: DNS is validated before fetch, but httpx resolves
        # independently — a rebinding between check and connect could steer
        # the actual connection private. Mitigation: re-validate after fetch
        # (including redirect chain) and bound size. Full pinning with TLS
        # SNI preservation requires outbound network restrictions; the
        # residual risk is documented and the fetch never follows redirects
        # to private targets.
        try:
            resp = httpx.get(source, timeout=30, follow_redirects=False)
            # Reject redirects to non-safe targets instead of following them.
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("location", "")
                raise ServiceError(f"Redirect target rejected by security policy: {location!r}")
            resp.raise_for_status()
        except ServiceError:
            raise
        except Exception as e:
            raise ServiceError(f"Failed to fetch URL: {e}") from None
        # Post-fetch re-validation: DNS may have rebound during the fetch.
        if not _is_safe_url(source):
            raise ServiceError(
                f"URL failed post-fetch security re-validation (possible DNS rebinding): {source}"
            )
        content = resp.text
        if len(content.encode("utf-8", errors="replace")) > 200_000:
            raise ServiceError("Skill content from URL exceeds 200KB limit")
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
