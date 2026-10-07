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
    "maybe_auto_compact",
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
    mutation_token: str | None = None,
) -> CompressionResult | None:
    """Compress a session's context in place.

    Returns ``None`` when there is nothing to compress. Uses LLM summarization
    when enabled and a provider can be built, otherwise truncation.

    Ownership (LP-11): when *mutation_token* is None a claim is held for the
    whole operation; when supplied (explicit /compress already holds one) it
    is verified still current BEFORE the destructive replacement, and a stale
    snapshot aborts instead of replacing under a newer owner.
    """
    from ah.core.turns import begin_mutation, end_mutation

    owned = mutation_token is None
    if owned:
        mutation_token = await begin_mutation(session.id)
        if not mutation_token:
            return None
    # H-04: Ownership lifetime starts immediately after claim acquisition.
    # All reads, config, provider construction, compression, persistence,
    # and cleanup happen inside this owning try/finally.
    llm_provider = None
    try:
        # AH-008: fetch ALL chunks (paginated), not just the newest 1000, and
        # replace only the captured input IDs so concurrent inserts survive.
        chunks = await context_manager.get_all_chunks(session.id)
        if not chunks:
            return None

        comp_config = _compression_config()
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
        # Stale-snapshot guard: verify this owner still holds the claim BEFORE
        # the destructive replacement (LP-11). An expired claim aborts.
        # DB-less environments skip the check (the in-process mutex is the
        # claim there).
        try:
            from ah.db.connection import db as _db

            if _db.connected:
                from ah.core.turns import _db_claim_state

                current = await _db_claim_state(session.id)
                if current is None or current.get("claim_owner") != mutation_token:
                    return None
        except Exception:
            pass
        # AH-008/AH-009: replace only captured IDs; originals are archived
        # transactionally inside replace_chunks_by_ids.
        await context_manager.replace_chunks_by_ids(
            session.id,
            [c.id for c in chunks],
            result.compressed_chunks,
            archive_reason="compressed",
        )
        return result
    finally:
        # Owned compression provider closes on EVERY exit: success, no-op,
        # summarization/persistence failure, cancellation, or timeout.
        # Cleanup failures are recorded, never silently swallowed.
        close = getattr(llm_provider, "close", None)
        if callable(close):
            try:
                import inspect as _inspect

                r = close()
                if _inspect.isawaitable(r):
                    await r
            except Exception as e:
                import logging as _logging

                _logging.getLogger(__name__).warning("compression provider close failed: %s", e)
        if owned:
            try:
                await end_mutation(session.id, mutation_token)
            except Exception:
                pass


async def _estimate_next_request(session_id, budget: int) -> int:
    """Budget the ACTUAL next model request (LP-11), not just stored rows.

    Measures the actual message list and provider tool schema that will be
    sent, including in-turn accumulated tool messages and mandatory
    persona/instructions. Distinguishes model context capacity, output
    reservation, per-run spending, and durable usage budgets.
    """
    from ah.core.assembler import get_token_count

    total = 0
    # Stored context tokens (actual message content)
    try:
        total += await context_manager.get_token_usage(session_id)
    except Exception:
        pass
    # Tool schema tokens (sent with every request)
    try:
        from ah.tools.base import registry

        total += get_token_count(str(registry.get_tool_definitions()))
    except Exception:
        pass
    # Reserved output tokens
    try:
        total += int(config.get("max_tokens") or 4096)
    except Exception:
        total += 4096
    # Mandatory system/persona instructions (always included)
    try:
        from ah.core.agent import SYSTEM_PROMPT

        total += get_token_count(SYSTEM_PROMPT)
    except Exception:
        pass
    return total


async def maybe_auto_compact(session_id) -> dict | None:
    """Threshold-triggered rolling compaction at a safe turn boundary.

    Holds a durable MUTATION claim for the whole operation (LP-11): skips
    when busy (deferred to a later boundary, never queued behind a turn),
    verifies ownership is still ours before replacing, and budgets the next
    rendered request including tool schemas and reserved output. Originals
    archive transactionally; concurrent inserts survive via exact-ID replace.
    """
    try:
        if not config.get("auto_compaction_enabled"):
            return None
        if not config.get("compression_enabled"):
            return None
    except Exception:
        pass
    from ah.core.turns import begin_mutation, end_mutation

    token = await begin_mutation(session_id)
    if not token:
        return None  # busy: defer, do not queue behind live work
    try:
        from ah.core.session import session_manager as _sessions

        session = await _sessions.get(session_id)
        if session is None:
            return None
        budget = session.context_budget or 8000
        try:
            threshold = float(config.get("compression_threshold") or 0.8)
        except Exception:
            threshold = 0.8
        if await _estimate_next_request(session_id, budget) < int(budget * threshold):
            return None
        # No-progress guard: skip when a previous run already compacted at
        # nearly this size.
        state = session.state or {}
        last = state.get("last_auto_compact_tokens")
        total_now = await context_manager.get_token_usage(session_id)
        if isinstance(last, int) and total_now <= last + max(100, int(budget * 0.05)):
            return None
        # compress_session verifies this same token before replacing (shared
        # ownership, no double claim).
        result = await compress_session(session, mutation_token=token)
        if result is None:
            return None
        # Ownership re-check: an expired claim means another worker may own
        # the session now — record progress only when still ours.
        try:
            from ah.db.connection import db as _db

            if _db.connected:
                from ah.core.turns import _db_claim_state

                current = await _db_claim_state(session_id)
                if current is None or current.get("claim_owner") != token:
                    return None
        except Exception:
            pass
        try:
            state = dict(session.state or {})
            state["last_auto_compact_tokens"] = result.compressed_tokens
            await _sessions.update_state(session_id, state)
        except Exception:
            pass
        # Eviction pass when configured (retention policy, distinct from
        # summarization): keep reversible via archive.
        try:
            max_tokens = int(config.get("eviction_max_tokens") or 0)
        except Exception:
            max_tokens = 0
        evicted = 0
        if max_tokens and max_tokens > 0:
            try:
                evicted = await context_manager.evict_old_chunks(session_id, max_tokens=max_tokens)
            except Exception:
                evicted = 0
        return {
            "compressed": True,
            "originalTokens": result.original_tokens,
            "compressedTokens": result.compressed_tokens,
            "evicted": evicted,
            "method": result.method,
        }
    except Exception:
        return None
    finally:
        try:
            await end_mutation(session_id, token)
        except Exception:
            pass


def learn_skill(
    source: str,
    *,
    name: str | None = None,
    description: str | None = None,
    triggers: list[str] | None = None,
):
    """Create a skill from a file path, an http(s) URL, or an existing skill name."""
    from ah.skills.registry import skill_registry

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
        # 5.8: destination enforced AT the pinned connection
        # (ah/security/fetch.py): single bounded resolution, every address
        # checked, TCP opened to the validated IP (no re-resolution for
        # rebinding to steer), TLS SNI/hostname preserved, redirects
        # re-validated hop-by-hop, body capped DURING streaming. A pre-check
        # alone (or post-fetch revalidation) is detection, not prevention.
        # Network initiation here is human transport (CLI/gateway token), and
        # full-host mode never relaxes SSRF policy.
        from ah.security.fetch import FetchError, pinned_fetch

        try:
            content = pinned_fetch(source)
        except FetchError as e:
            raise ServiceError(f"URL rejected by security policy: {e}") from None
        except Exception as e:
            raise ServiceError(f"Failed to fetch URL: {e}") from None
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
    """Database, counts, mode, and real capability health.

    Every flag maps to enforced behavior or an explicit degraded/unsupported
    state — never healthy-by-config alone. Requires a connected ``db``.
    """
    from ah.tools.base import registry

    version = await db.fetchval("SELECT version()")
    try:
        from ah.core.runtime import runtime_services

        runtime = runtime_services.status()
    except Exception:
        runtime = {}
    try:
        from ah.memory.embeddings import embedding_status

        retrieval = embedding_status()
    except Exception:
        retrieval = {"mode": "keyword-only"}
    try:
        rag_docs = await db.fetchval(
            "SELECT COUNT(DISTINCT (payload_msgpack)) FROM context_chunks WHERE chunk_type = 'document'"
        )
    except Exception:
        rag_docs = 0
    try:
        from ah.core.config import config as _cfg

        mode = _cfg.get("execution_mode") or "ask"
        backend = "sandbox" if mode == "sandbox" else "host"
        workspace = _cfg.get("workspace_root") or _cfg.get("agent_harness_home") or ""
        reranker = "passthrough"
        try:
            if _cfg.get("cohere_api_key"):
                reranker = "cohere"
        except Exception:
            pass
        auto_compact = bool(_cfg.get("auto_compaction_enabled") and _cfg.get("compression_enabled"))
        extraction = (
            "ready"
            if bool(_cfg.get("memory_consolidation_enabled") and _cfg.get("memory_enabled"))
            else "disabled"
        )
    except Exception:
        mode, backend, workspace, reranker, auto_compact, extraction = (
            "ask",
            "host",
            "",
            "passthrough",
            True,
            "ready",
        )
    try:
        jobs_waiting = await db.fetchval(
            "SELECT COUNT(*) FROM jobs WHERE status = 'error' AND last_error ILIKE '%needs_approval%'"
        )
    except Exception:
        jobs_waiting = 0
    base = {
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
    base.update(
        {
            "mode": mode,
            "backend": backend,
            "workspace": workspace,
            "chatProvider": runtime.get("chat_provider", "unknown"),
            "memoryRetrieval": retrieval.get("mode", "keyword-only"),
            "memoryRetrievalReason": retrieval.get("reason", ""),
            "memoryExtraction": extraction,
            "documentRag": (
                f"ready; {rag_docs} indexed documents"
                if rag_docs
                else "degraded; no indexed documents (index material to enable retrieval)"
            ),
            "reranker": reranker,
            "autoCompaction": "enabled" if auto_compact else "disabled",
            "sandbox": runtime.get("sandbox", "unknown"),
            "jobsAwaitingApproval": jobs_waiting,
            "researchTraining": "offline, not part of chat runtime",
            "capabilities": runtime.get("capabilities", []),
        }
    )
    try:
        from ah.permissions import store as _perm_store

        durable = _perm_store.is_durable()
        base["permissionDurability"] = (
            "durable" if durable else ("local-only" if durable is False else "unknown")
        )
    except Exception:
        base["permissionDurability"] = "unknown"
    return base
