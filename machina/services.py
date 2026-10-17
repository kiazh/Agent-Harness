"""Feature operations shared by the CLI and the gateway.

Each function does the work and returns data (or raises ``ServiceError`` with a
user-facing message); callers decide how to present it.
"""

from __future__ import annotations

import asyncio as _asyncio
import inspect as _inspect
import json
import logging as _logging
from pathlib import Path
from typing import Any

from machina.core.compression import CompressionConfig, CompressionResult, ContextCompressor
from machina.core.config import config
from machina.core.context import context_manager
from machina.core.metrics import metrics
from machina.core.models import Session
from machina.db.connection import db
from machina.observability.diagnostics import record_failure

__all__ = [
    "ServiceError",
    "CompressOutcome",
    "compress_session",
    "compress_session_with_status",
    "maybe_auto_compact",
    "export_markdown",
    "format_status_lines",
    "learn_skill",
    "status_summary",
]


from dataclasses import dataclass, field


@dataclass
class CompressOutcome:
    """Typed compression outcome (no-op/stale/failure are distinct, not None).

    status: no_op | completed | stale_ownership | stale_snapshot | failed | busy
    result: CompressionResult when completed, else None.
    error: bounded diagnostic for failed (never raw payloads).
    cause: original exception behind failed/stale statuses, consumed by the
        legacy compress_session wrapper; never rendered or serialized.
    """

    status: str
    result: CompressionResult | None = None
    error: str = ""
    cause: BaseException | None = field(default=None, repr=False, compare=False)


class ServiceError(Exception):
    """A failure with a message that is safe to show the user."""


async def export_markdown(session: Session) -> str:
    """Render a session's conversation as Markdown.

    Policy (AH-002): exports are redacted — secret-looking strings are
    replaced before rendering so raw transcripts never leak via
    session.export / /rpc. The stored chunks are unchanged.
    """
    from machina.memory.redaction import redact_secrets as _redact

    chunks = await context_manager.get_transcript_chunks(session.id)
    # AH-AUDIT-012: title/goal are outward-facing export fields subject to
    # the same structured redaction policy as message content. Raw stored
    # conversation (DB chunks) is intentionally untouched.
    lines = [
        f"# Session: {_redact(str(session.title or '(untitled)')).text}",
        "",
        f"**ID:** {session.id}",
        f"**Status:** {session.status}",
        f"**Agent:** {session.agent_id}",
        f"**Goal:** {_redact(str(session.goal or '(none)')).text}",
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
            heading = (
                "### Assistant (interrupted)" if payload.get("interrupted") else "### Assistant"
            )
            lines += [heading, "", _redact(str(payload.get("content", ""))).text, ""]
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
            result = payload.get("result", payload.get("result_preview", ""))
            if result:
                lines.append(f"**Result:** {_redact(str(result)).text}")
            if "result" not in payload:
                lines.append("[Historical tool output: only a preview was stored.]")
            elif payload.get("result_truncated"):
                lines.append("[Tool output truncated at the storage limit of 65,536 characters.]")
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


async def _effective_session_route(
    session: Session,
    *,
    provider: str | None = None,
    model: str | None = None,
) -> tuple[str | None, str | None]:
    """Resolve the summarization provider/model exactly like an ordinary turn.

    Same precedence as ``build_agent_for_session`` (AH-021/AH-022): explicit
    override, session pin, agent definition, global config. Compression must
    never silently move a session pinned to a local/private provider onto the
    gateway or global default (H20); when the effective route cannot build a
    provider, the compressor falls back to truncation instead of a different
    provider. A definition lookup that fails here degrades to the session/
    global route rather than failing the operation (compression is auxiliary).
    """
    definition = None
    provider_name = provider or getattr(session, "provider", None)
    model_name = model or getattr(session, "model", None)
    if not provider_name or not model_name:
        try:
            from machina.core.agent_def import agent_registry

            definition = await agent_registry.get(session.agent_id or "harness")
        except Exception:
            definition = None
        provider_name = provider_name or getattr(definition, "provider", None)
        model_name = model_name or getattr(definition, "model", None)
    return (
        provider_name or config.get("provider"),
        model_name or config.get("model"),
    )


async def session_context_window(session: Session) -> int:
    """Capacity for the same effective provider/model route as a session turn."""
    from machina.core.context_windows import context_window_for

    provider, model = await _effective_session_route(session)
    return context_window_for(model, provider)


async def compress_session_with_status(
    session: Session,
    *,
    model: str | None = None,
    provider: str | None = None,
    mutation_token: str | None = None,
) -> CompressOutcome:
    """Compress with an explicit status (see CompressOutcome).

    Ownership (LP-11): same fencing as compress_session. Stale ownership and
    stale snapshots abort with rollback and distinct statuses, never partial.
    Unexpected failures are recorded and returned as failed (bounded error).
    """
    from machina.core.turns import begin_mutation, end_mutation

    owned = mutation_token is None
    if owned:
        try:
            mutation_token = await begin_mutation(session.id)
        except Exception as exc:
            record_failure("services.compress_session", exc)
            return CompressOutcome(status="failed", error=type(exc).__name__, cause=exc)
        if not mutation_token:
            return CompressOutcome(status="busy")
    # H-04: Ownership lifetime starts immediately after claim acquisition.
    # All reads, config, provider construction, compression, persistence,
    # and cleanup happen inside this owning try/finally.
    llm_provider = None
    try:
        # AH-008: fetch ALL chunks (paginated), not just the newest 1000, and
        # replace only the captured input IDs so concurrent inserts survive.
        # AH-AUDIT-034: conversational compaction scope excludes indexed
        # documents — document rows keep their IDs/text/embeddings/source
        # metadata intact through explicit and automatic compression.
        # Eviction likewise never selects documents (context.py); dedicated
        # document lifecycle operations own the document index.
        all_chunks = await context_manager.get_all_chunks(session.id)
        chunks = [c for c in all_chunks if c.chunk_type != "document"]
        if not chunks:
            return CompressOutcome(status="no_op")

        comp_config = _compression_config()
        if comp_config.llm_summarize:
            from machina.core.provider import get_provider

            # H20: route summarization through the session's effective
            # provider/model (session pin -> agent definition -> global
            # config), the same precedence ordinary turns use. Explicit
            # callers may still override; gateway/global defaults must never
            # be passed as overrides for an explicit compression.
            provider_name, model_name = await _effective_session_route(
                session, provider=provider, model=model
            )
            try:
                llm_provider = get_provider(provider=provider_name, model=model_name)
            except Exception:
                llm_provider = None  # no key / provider: fall back to truncation

        # AH-011: await summarization directly instead of blocking the event loop
        # via Future.result() on a worker thread.
        from machina.core.compression import ContextCompressor as _CC

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
            return CompressOutcome(status="no_op", result=result)
        # AH-AUDIT-014/015: the live mutation token travels INTO the
        # archive/delete/insert transaction (fenced there on the DB clock),
        # and the captured original ID set is validated exactly. Stale
        # ownership or a stale snapshot aborts with rollback: an explicit
        # stale outcome, never a partial commit.
        from machina.core.exceptions import StaleOwnershipError, StaleSnapshotError

        try:
            await context_manager.replace_chunks_by_ids(
                session.id,
                [c.id for c in chunks],
                result.compressed_chunks,
                archive_reason="compressed",
                expected_owner=mutation_token,
            )
        except StaleOwnershipError as exc:
            return CompressOutcome(status="stale_ownership", error=type(exc).__name__, cause=exc)
        except StaleSnapshotError as exc:
            return CompressOutcome(status="stale_snapshot", error=type(exc).__name__, cause=exc)
        return CompressOutcome(status="completed", result=result)
    except Exception as exc:
        record_failure("services.compress_session", exc)
        return CompressOutcome(status="failed", error=type(exc).__name__, cause=exc)
    finally:
        # Owned compression provider closes on EVERY exit: success, no-op,
        # summarization/persistence failure, cancellation, or timeout.
        # AH-AUDIT-026: bounded join (5s) with claim release in an
        # independent outer finally — a stuck close cannot strand the
        # mutation claim or the serial runner.
        try:
            close = getattr(llm_provider, "close", None)
            if callable(close):
                try:
                    r = close()
                    if _inspect.isawaitable(r):
                        await _asyncio.wait_for(r, timeout=5)
                except Exception as e:
                    _logging.getLogger(__name__).warning(
                        "compression provider close failed: %s", e, exc_info=True
                    )
        finally:
            if owned:
                try:
                    await end_mutation(session.id, mutation_token)
                except Exception as _boundary_error:
                    # Optional fallback preserves the primary outcome; report no payload.
                    record_failure("services.compress_session", _boundary_error)


async def compress_session(
    session: Session,
    *,
    model: str | None = None,
    provider: str | None = None,
    mutation_token: str | None = None,
) -> CompressionResult | None:
    """Compress a session's context in place (backward-compatible wrapper).

    Delegates to compress_session_with_status: returns the CompressionResult
    on completion, else None for no-op, busy, or stale outcomes. Unexpected
    failures re-raise the original exception (legacy behavior) — use
    compress_session_with_status when the distinction matters.
    """
    outcome = await compress_session_with_status(
        session, model=model, provider=provider, mutation_token=mutation_token
    )
    if outcome.status == "completed":
        return outcome.result
    if outcome.status == "failed":
        # Legacy contract: the raised exception is the original failure
        # object, not a wrapper. The bounded error string is a fallback only.
        if outcome.cause is not None:
            raise outcome.cause
        raise RuntimeError(outcome.error or "compression failed")
    # no_op | busy | stale_ownership | stale_snapshot collapse to None.
    return None


async def _estimate_next_request(session_id) -> int:
    """Approximation of the next model request on the gate's own basis.

    The auto-compaction trigger must not use a different token unit than the
    hard capacity gate, or a session can sit "below threshold" while the gate
    refuses the turn. This function therefore rebuilds the same recent-context
    message window the agent assembles from and delegates to
    :func:`machina.core.prompt.request_context_tokens`: one tiktoken counter
    over one JSON serialization for context chunks, tool definitions, system
    instructions, framing, visual reserve and the output reserve.

    It remains an approximation (retrieved memory/RAG and the provider's
    instance-specific reservation ceiling are not replayed), never a substitute
    for the gate. No provider calls and no memory access-state changes.
    """
    from machina.core.prompt import request_context_tokens

    definition_prompt = ""
    allowed: list | None = None
    session = None
    try:
        from machina.core.session import session_manager as _sessions

        session = await _sessions.get(session_id)
    except Exception as _boundary_error:
        # Optional fallback preserves the primary outcome; report no payload.
        record_failure("services.maybe_auto_compact.session", _boundary_error)
    if session is not None:
        try:
            from machina.core.agent_def import agent_registry

            definition = await agent_registry.get(session.agent_id or "harness")
            if definition is not None:
                from machina.core.agent_factory import definition_tool_cap

                # AgentDef [] means unrestricted (full registry), unlike
                # authority-layer [] which means zero tools; resolve through
                # the same translation execution uses.
                allowed = definition_tool_cap(definition)
                definition_prompt = definition.system_prompt or ""
        except Exception as _boundary_error:
            # Optional fallback preserves the primary outcome; report no payload.
            record_failure("services._estimate_next_request", _boundary_error)

    # Context window: the same bounded recent-context read the agent uses.
    messages: list[dict[str, Any]] = []
    try:
        recent = await context_manager.get_recent_context(session_id, limit=10)
    except Exception as _boundary_error:
        # Optional fallback preserves the primary outcome; report no payload.
        record_failure("services._estimate_next_request", _boundary_error)
        recent = []
    for entry in reversed(recent or []):
        if not isinstance(entry, dict):
            continue
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        content = payload.get("content")
        if not isinstance(content, str):
            try:
                content = json.dumps(payload, ensure_ascii=False, default=str)
            except Exception:
                content = str(payload)
        chunk_type = entry.get("type")
        if chunk_type == "assistant_message":
            role = "assistant"
        elif chunk_type == "tool_call":
            role = "tool"
        else:
            role = "user"
        message: dict[str, Any] = {"role": role, "content": content}
        tool_call_id = payload.get("tool_call_id")
        if role == "tool" and isinstance(tool_call_id, str):
            message["tool_call_id"] = tool_call_id
        messages.append(message)

    # Effective tool schema: the session agent's allowlist when resolvable.
    tools: list = []
    try:
        from machina.tools.base import registry

        definitions = registry.get_tool_definitions()
        if allowed is None:
            tools = list(definitions)
        else:
            allowed_set = set(allowed)
            tools = [t for t in definitions if getattr(t, "name", "") in allowed_set]
    except Exception as _boundary_error:
        # Optional fallback preserves the primary outcome; report no payload.
        record_failure("services._estimate_next_request", _boundary_error)

    # Effective system instructions (base + specialist definition prompt).
    try:
        from machina.core.agent import SYSTEM_PROMPT

        system_prompt = SYSTEM_PROMPT + (definition_prompt or "")
    except Exception as _boundary_error:
        # Optional fallback preserves the primary outcome; report no payload.
        record_failure("services._estimate_next_request", _boundary_error)
        system_prompt = definition_prompt or ""
    messages.insert(0, {"role": "system", "content": system_prompt})

    provider_name = None
    if session is not None:
        candidate = getattr(session, "provider", None)
        provider_name = candidate if isinstance(candidate, str) else None
    if not provider_name:
        provider_name = config.get("provider")

    try:
        return request_context_tokens(messages, tools, provider_name)
    except Exception as _boundary_error:
        # Same counter/serialization fallback (never the old len//4 basis).
        from machina.core.assembler import get_token_count
        from machina.core.provider.transport import _tools_payload

        record_failure("services._estimate_next_request", _boundary_error)
        try:
            return (
                get_token_count(json.dumps(messages, ensure_ascii=False))
                + get_token_count(json.dumps(_tools_payload(tools), ensure_ascii=False))
                + int(config.get("max_tokens") or 4096)
            )
        except Exception:
            return 0


def _record_compaction_status(
    status: str,
    *,
    reason: str = "",
    error_class: str = "",
    tokens_before: int | None = None,
    tokens_after: int | None = None,
) -> None:
    """Bounded honest status for one auto-compaction decision (spec §14.7, §21).

    Exactly one counter per decision: ``compaction.<status>``, with the reason
    appended for skips (``compaction.skipped.busy``, ``.disabled``,
    ``.below_threshold``, ``.no_progress``, ``.session_missing``,
    ``.no_change``). A rich audit event is emitted only when it carries what
    a counter cannot — the error class, and the size before/after a real
    compaction — so the routine threshold skip on every turn boundary never
    writes an audit row. Bounded and default-safe: labels and counts only,
    never chunk bodies, prompts, credentials, or a raw exception message. A
    failure here can never change the scheduling decision.
    """
    counter = f"compaction.{status}"
    if status == "skipped" and reason:
        counter = f"{counter}.{reason}"
    try:
        metrics.increment_counter(counter)
    except Exception as exc:  # pragma: no cover - in-memory, best effort
        record_failure("services.compaction_status", exc)
        return
    if status not in ("completed", "failed"):
        return
    try:
        from machina.core.provider import audit_log

        audit_log(
            "compaction_status",
            status=status,
            error_class=error_class or None,
            tokens_before=tokens_before,
            tokens_after=tokens_after,
        )
    except Exception as exc:
        record_failure("services.compaction_status", exc)


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
            _record_compaction_status("skipped", reason="disabled")
            return None
        if not config.get("compression_enabled"):
            _record_compaction_status("skipped", reason="disabled")
            return None
    except Exception as _boundary_error:
        # Optional fallback preserves the primary outcome; report no payload.
        record_failure("services.maybe_auto_compact", _boundary_error)
    from machina.core.turns import begin_mutation, end_mutation

    # Claim acquisition is inside the recorder too: a failing begin_mutation
    # is a degraded deferral (never an unhandled task exception) reported
    # through the same operation name.
    try:
        token = await begin_mutation(session_id)
    except Exception as _claim_error:
        record_failure("services.maybe_auto_compact", _claim_error)
        # Failed before any claim: distinguishable from a started-then-failed
        # compaction (no `started` counter was emitted).
        _record_compaction_status("failed", error_class=type(_claim_error).__name__)
        return None
    if not token:
        # Busy: defer, do not queue behind live work. One counter, no started.
        _record_compaction_status("skipped", reason="busy")
        return None
    # Claim acquired: only now is the attempt "started" (§14.7). A scheduled
    # task that never reached this line stays visible as scheduled-only.
    _record_compaction_status("started")
    try:
        from machina.core.session import session_manager as _sessions

        session = await _sessions.get(session_id)
        if session is None:
            _record_compaction_status("skipped", reason="session_missing")
            return None
        budget = await session_context_window(session)
        try:
            threshold = float(config.get("compression_threshold") or 0.8)
        except Exception:
            threshold = 0.8
        if await _estimate_next_request(session_id) < int(budget * threshold):
            _record_compaction_status("skipped", reason="below_threshold")
            return None
        # No-progress guard: skip when a previous run already compacted at
        # nearly this size.
        state = session.state or {}
        last = state.get("last_auto_compact_tokens")
        total_now = await context_manager.get_token_usage(session_id)
        if isinstance(last, int) and total_now <= last + max(100, int(budget * 0.05)):
            _record_compaction_status("skipped", reason="no_progress")
            return None
        # compress_session verifies this same token before replacing (shared
        # ownership, no double claim).
        result = await compress_session(session, mutation_token=token)
        if result is None:
            # No-op/stale (busy is impossible here: we hold the mutation
            # claim and pass it through). Counted once, never as `started`.
            _record_compaction_status("skipped", reason="no_change")
            return None
        # Compression committed (compress_session fences the token inside the
        # archive/replace transaction), so this is a completed compaction even
        # if the progress watermark write below is skipped.
        _record_compaction_status(
            "completed",
            tokens_before=result.original_tokens,
            tokens_after=result.compressed_tokens,
        )
        # Ownership re-check: an expired claim means another worker may own
        # the session now — record progress only when still ours.
        try:
            from machina.db.connection import db as _db

            if _db.connected:
                from machina.core.turns import _db_claim_state

                current = await _db_claim_state(session_id)
                if current is None or current.get("claim_owner") != token:
                    # Compression is already counted `completed` above; this
                    # short-circuit withholds only the progress watermark from
                    # a session another worker may now own.
                    return None
        except Exception as _boundary_error:
            # Optional fallback preserves the primary outcome; report no payload.
            record_failure("services.maybe_auto_compact", _boundary_error)
        try:
            # AH-AUDIT-018: transactional field merge preserves unrelated
            # concurrent state (emotion/watermark) instead of overwriting
            # from a stale snapshot.
            await _sessions.update_state_fields(
                session_id, {"last_auto_compact_tokens": result.compressed_tokens}
            )
        except Exception as _boundary_error:
            # Optional fallback preserves the primary outcome; report no payload.
            record_failure("services.maybe_auto_compact", _boundary_error)
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
            except Exception as _boundary_error:
                # Optional fallback preserves the primary outcome; report no payload.
                record_failure("services.maybe_auto_compact.evict", _boundary_error)
                evicted = 0
        return {
            "compressed": True,
            "originalTokens": result.original_tokens,
            "compressedTokens": result.compressed_tokens,
            "evicted": evicted,
            "method": result.method,
        }
    except Exception as _compact_error:
        record_failure("services.maybe_auto_compact", _compact_error)
        # Started then failed: the class (not the message) is the only detail.
        _record_compaction_status("failed", error_class=type(_compact_error).__name__)
        return None
    finally:
        try:
            await end_mutation(session_id, token)
        except Exception as _boundary_error:
            # Optional fallback preserves the primary outcome; report no payload.
            record_failure("services.maybe_auto_compact", _boundary_error)


def learn_skill(
    source: str,
    *,
    name: str | None = None,
    description: str | None = None,
    triggers: list[str] | None = None,
):
    """Create a skill from a file path, an http(s) URL, or an existing skill name."""
    from machina.skills.registry import skill_registry

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
        # (machina/security/fetch.py): single bounded resolution, every address
        # checked, TCP opened to the validated IP (no re-resolution for
        # rebinding to steer), TLS SNI/hostname preserved, redirects
        # re-validated hop-by-hop, body capped DURING streaming. A pre-check
        # alone (or post-fetch revalidation) is detection, not prevention.
        # Network initiation here is human transport (CLI/gateway token), and
        # full-host mode never relaxes SSRF policy.
        from machina.security.fetch import FetchError, pinned_fetch

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
        content = skill_registry.get_skill_content(existing.name) or ""
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


def _safe_config_value(cfg, key: str) -> Any:
    """Read a config value or report unknown — never raise out of status."""
    try:
        value = cfg.get(key)
        return value if value is not None else "unknown"
    except Exception as _boundary_error:
        record_failure("services.status_summary", _boundary_error)
        return "unknown"


def _safe_config_bool(cfg, key: str) -> bool:
    try:
        return bool(cfg.get(key))
    except Exception as _boundary_error:
        record_failure("services.status_summary", _boundary_error)
        return False


async def status_summary() -> dict[str, Any]:
    """Database, counts, mode, and real capability health.

    Every flag maps to enforced behavior or an explicit degraded/unsupported
    state — never healthy-by-config alone. Requires a connected ``db``.
    """
    from machina.tools.base import registry

    version = await db.fetchval("SELECT version()")
    try:
        from machina.core.runtime import runtime_services

        runtime = runtime_services.status()
    except Exception as _boundary_error:
        # Optional fallback preserves the primary outcome; report no payload.
        record_failure("services.status_summary.runtime", _boundary_error)
        runtime = {}
    try:
        from machina.memory.embeddings import embedding_status

        retrieval = embedding_status()
    except Exception as _boundary_error:
        # Optional fallback preserves the primary outcome; report no payload.
        record_failure("services.status_summary.retrieval", _boundary_error)
        retrieval = {"mode": "keyword-only"}
    memory_mode = retrieval.get("mode", "keyword-only")
    if retrieval.get("backend") and retrieval.get("model"):
        memory_mode = f"{memory_mode} ({retrieval['backend']}:{retrieval['model']})"
    try:
        rag_docs = await db.fetchval(
            "SELECT COUNT(DISTINCT COALESCE(document_source, id::text)) FROM context_chunks WHERE chunk_type = 'document'"
        )
    except Exception as _boundary_error:
        # Optional fallback preserves the primary outcome; report no payload.
        record_failure("services.status_summary.rag_docs", _boundary_error)
        rag_docs = 0
    try:
        from machina.core.config import config as _cfg

        mode = _cfg.get("execution_mode") or "ask"
        backend = "sandbox" if mode == "sandbox" else "host"
        workspace = _cfg.get("workspace_root") or _cfg.get("machina_home") or ""
        # AH-AUDIT-036: status derives from the actual shared component
        # identity when initialized; otherwise an honestly labeled
        # expectation from the resolved selection (key/config presence alone
        # never claims healthy selection).
        reranker = "passthrough"
        try:
            from machina.tools import rag as _rag_tools

            _shared = _rag_tools._rag_pipeline
            if _shared is not None:
                identity = _shared.reranker_identity()
                value = identity.get("reranker", "passthrough")
                detail = identity.get("detail", "")
                # Backend + bound model; passthrough reasons stay short so the
                # status line reads as a state, not a log dump.
                reranker = f"{value}:{detail}" if detail and value != "passthrough" else value
            else:
                from machina.rag.reranker import resolve_reranker_selection

                selection = resolve_reranker_selection()
                if selection.kind != "none":
                    reranker = f"{selection.kind} (configured; pipeline not initialized)"
        except Exception as _boundary_error:
            # Optional fallback preserves the primary outcome; report no payload.
            record_failure("services.status_summary", _boundary_error)
        auto_compact = bool(_cfg.get("auto_compaction_enabled") and _cfg.get("compression_enabled"))
        extraction = (
            "ready"
            if bool(_cfg.get("memory_consolidation_enabled") and _cfg.get("memory_enabled"))
            else "disabled"
        )
    except Exception as _boundary_error:
        # Optional fallback preserves the primary outcome; report no payload.
        record_failure("services.status_summary.config", _boundary_error)
        mode, backend, workspace, reranker, auto_compact, extraction = (
            "ask",
            "host",
            "",
            "unknown",
            False,
            "unknown",
        )
    try:
        # AH-AUDIT-021: durable paused state plus legacy error rows.
        jobs_waiting = await db.fetchval(
            "SELECT COUNT(*) FROM jobs WHERE status = 'paused_approval' "
            "OR (status = 'error' AND last_error ILIKE '%needs_approval%')"
        )
    except Exception as _boundary_error:
        # Optional fallback preserves the primary outcome; report no payload.
        record_failure("services.status_summary.jobs_waiting", _boundary_error)
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
        # Total config failure must never crash the endpoint: report unknown.
        "openrouterKeySet": _safe_config_bool(config, "openrouter_api_key"),
        "model": _safe_config_value(config, "model"),
        "provider": _safe_config_value(config, "provider"),
    }
    base.update(
        {
            "mode": mode,
            "backend": backend,
            "workspace": workspace,
            "chatProvider": runtime.get("chat_provider", "unknown"),
            "memoryRetrieval": memory_mode,
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
        from machina.permissions import store as _perm_store

        durable = _perm_store.is_durable()
        base["permissionDurability"] = (
            "durable" if durable else ("local-only" if durable is False else "unknown")
        )
    except Exception as _boundary_error:
        # Optional fallback preserves the primary outcome; report no payload.
        record_failure("services.status_summary.permission_durability", _boundary_error)
        base["permissionDurability"] = "unknown"
    return base


def format_status_lines(summary: dict[str, Any]) -> list[tuple[str, str]]:
    """Shared (label, value) status rows for the CLI and the MCP shim.

    Both surfaces used to fork this rendering; they now derive the shared
    subset from one ``status_summary()`` payload here. No rendering and no
    Rich: each caller keeps its per-surface extras (CLI: Rich markup plus
    direct DB/registry reads; shim: count/model lines) and its own styling.
    """

    # Local mirror of the shim's tolerant value lookup: missing and None
    # collapse to the default so a degraded payload never crashes a surface.
    def value(key: str, default: object = "unknown") -> object:
        found = summary.get(key, default)
        return default if found is None else found

    retrieval = str(value("memoryRetrieval"))
    if summary.get("memoryRetrievalReason"):
        retrieval += f" ({summary['memoryRetrievalReason']})"
    return [
        ("Mode", str(value("mode"))),
        ("Backend", str(value("backend"))),
        ("Workspace", str(value("workspace", "") or "")),
        ("Chat provider", str(value("chatProvider"))),
        ("Memory retrieval", retrieval),
        ("Memory extraction", str(value("memoryExtraction"))),
        ("Document RAG", str(value("documentRag"))),
        ("Reranker", str(value("reranker"))),
        ("Automatic compaction", str(value("autoCompaction"))),
        ("Sandbox", str(value("sandbox"))),
        ("Jobs awaiting approval", str(value("jobsAwaitingApproval", 0))),
    ]
