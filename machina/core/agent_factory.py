"""Definition-aware agent factory (AH-022; Phase B/C shared factory).

Direct session execution (REST prompt_session, gateway _run_turn) must honor
the session's agent definition (allowed_tools, system prompt, model/provider
overrides). Executing a specialist child session with a bare ReActAgent
would bypass its restricted tool definition.

Phase B/C: resolves stored session identity, agent definition,
model/provider overrides, shared dependencies (memory retriever +
consolidator when enabled, shared RAG pipeline), workspace, and inherited
authority consistently across chat / REST / jobs / delegation. Never
eagerly instantiates cloud-key-dependent clients when features are
unavailable or disabled.
"""

from __future__ import annotations

from contextvars import ContextVar as _ContextVar

from machina.core.models import Session

# Inherited authority for the currently executing (parent) agent, set by the
# orchestrator around delegated child runs and read by the broker at the
# execution boundary (LP-08). Tools/mode outside these caps are denied even
# when a session grant would otherwise allow them.
parent_authority_var: _ContextVar[dict | None] = _ContextVar("parent_authority", default=None)


def _memory_enabled() -> bool:
    try:
        from machina.core.config import config

        return bool(config.get("memory_enabled"))
    except Exception:
        return True


def _rag_enabled() -> bool:
    try:
        from machina.core.config import config

        return bool(config.get("rag_enabled"))
    except Exception:
        return True


async def _shared_rag_pipeline():
    """Shared RAG service, initialized lazily (LP-10).

    Creates the pipeline on first use when enabled (so a cold restart still
    retrieves already-indexed documents through ordinary prompts), or returns
    None when disabled/unavailable â€” callers keep the keyword path and report
    degraded status honestly instead of failing startup.
    """
    if not _rag_enabled():
        return None
    try:
        from machina.tools.rag import get_rag_pipeline

        return await get_rag_pipeline()
    except Exception:
        return None


class AgentDefinitionError(ValueError):
    """Raised when a requested specialist definition cannot be resolved.

    A ValueError so callers treating unresolvable definitions as invalid
    input keep working; fail-closed semantics are unchanged.
    """


async def build_agent_for_session(
    session: Session,
    *,
    provider_override: str | None = None,
    model_override: str | None = None,
    authority: dict | None = None,
):
    """Build a ReActAgent honoring definition + shared services + authority.

    *authority*: inherited grant caps for delegated children
    (``{"max_mode": ..., "tools": [...]}``). Child effective
    authority never exceeds parent caps nor its own definition (enforced by
    callers via :func:`cap_child_authority`).

    Fail-closed (5.4): a requested NON-default specialist whose definition
    cannot be resolved (lookup error, unknown/deleted name, malformed row)
    raises AgentDefinitionError â€” never broad all-tool access. Only the
    general-purpose default (``harness``) falls back to unrestricted tools.
    Full-host user grants never widen a restrictive child definition.
    """
    from machina.core.agent import ReActAgent
    from machina.core.agent_def import agent_registry
    from machina.core.config import config
    from machina.core.provider import get_provider

    # A resumed/forked child keeps the restrictions stamped at delegation.
    # Newly supplied authority can narrow these caps but cannot replace them.
    authority = merge_authority(getattr(session, "delegation_authority", {}) or {}, authority or {})

    requested = session.agent_id or "harness"
    try:
        definition = await agent_registry.get(requested)
    except Exception as e:
        if requested != "harness":
            raise AgentDefinitionError(f"agent definition {requested!r} unavailable: {e}") from e
        definition = None
    if definition is None and requested != "harness":
        # Unknown/deleted specialist: controlled failure, not general access.
        # Built-ins are resolved deliberately (no blind all-tool default).
        from machina.core.agent_def import BUILTIN_AGENTS

        builtin = BUILTIN_AGENTS.get(requested)
        if builtin is None:
            raise AgentDefinitionError(f"unknown agent {requested!r}")
        definition = builtin

    if definition is not None:
        provider_name = (
            provider_override or session.provider or definition.provider or config.get("provider")
        )
        model = model_override or session.model or definition.model or config.get("model")
        system_prompt = definition.system_prompt or None
        # Explicit AgentDef -> authority translation; see definition_tool_cap.
        allowed_tools = definition_tool_cap(definition)
        agent_id = definition.name
    else:
        from machina.core.config import config as _cfg

        provider_name = provider_override or session.provider or _cfg.get("provider")
        model = model_override or session.model or _cfg.get("model")
        system_prompt = None
        allowed_tools = None
        agent_id = session.agent_id

    llm = get_provider(provider=provider_name, model=model)

    # Shared dependencies (lazy, never eager cloud instantiation).
    memory_retriever = None
    memory_consolidator = None
    if _memory_enabled():
        try:
            from machina.memory.retriever import MemoryRetriever
            from machina.memory.store import memory_store

            memory_retriever = MemoryRetriever(store=memory_store)
        except Exception:
            memory_retriever = None
        try:
            from machina.memory.consolidator import MemoryConsolidator

            # Extraction is an LLM job: the consolidator must carry the
            # session provider, or every consolidation fails with
            # "no LLM provider available for memory extraction".
            memory_consolidator = MemoryConsolidator(llm_provider=llm)
        except Exception:
            memory_consolidator = None

    rag_pipeline = await _shared_rag_pipeline()

    # Authority caps (LP-08): key presence/None semantics, never truthiness.
    # tools absent/None = no parent restriction; [] = zero permitted tools.
    # Intersect definition and parent tools; enforce the result at execution
    # (allowed_tools is checked per tool call in the agent loop).
    if authority is not None and "tools" in authority:
        parent_tools = authority["tools"]
        if parent_tools is None:
            pass
        elif allowed_tools is None:
            allowed_tools = list(parent_tools)
        else:
            allowed_tools = [t for t in allowed_tools if t in parent_tools]

    # F4 (2026-10-15 review): ``llm`` owns an HTTP client. Exporting the
    # definition and constructing the agent are the remaining steps that can
    # raise after the provider exists; any failure must close it rather than
    # leak the connection pool. (The memory/RAG steps above already swallow
    # their own optional-dependency failures.)
    try:
        from machina.soulspec.adapters import MachinaAdapter

        soul_spec = (
            MachinaAdapter.to_soulspec(definition).to_storage_dict()
            if definition is not None
            else None
        )
        agent = ReActAgent(
            provider=llm,
            agent_id=agent_id,
            system_prompt=system_prompt,
            memory_retriever=memory_retriever,
            memory_consolidator=memory_consolidator,
            rag_pipeline=rag_pipeline,
            allowed_tools=allowed_tools,
            soul_spec=soul_spec,
        )
    except BaseException:
        await _close_orphaned_provider(llm)
        raise
    # Mark ownership so callers close only providers they created (AH-023).
    agent._owns_provider = True  # type: ignore[attr-defined]
    agent._authority = authority or {}  # type: ignore[attr-defined]
    return agent


def cap_child_authority(
    parent_authority: dict, definition_tools: list[str] | None
) -> list[str] | None:
    """Child tools = definition ∩ parent caps (Phase 4.5/6, LP-08).

    A restricted parent cannot create an unrestricted child; broad parent
    access never expands a child's restrictive definition. Key presence/None
    semantics: absent/None = unrestricted on that side; [] = no tools.
    """
    parent_tools = (parent_authority or {}).get("tools", None)
    if "tools" not in (parent_authority or {}) or parent_tools is None:
        return definition_tools
    if definition_tools is None:
        return list(parent_tools)
    return [t for t in definition_tools if t in parent_tools]


def definition_tool_cap(definition: object) -> list[str] | None:
    """Translate AgentDef tools into authority-layer tools, explicitly.

    Two conventions meet here and must never be conflated by truthiness:

    - AgentDef convention (machina/core/agent_def.py): an EMPTY tools list
      means every registered tool (it is the default).
    - Authority convention (this module): None = unrestricted, [] = zero
      tools.

    The translation is deliberately spelled out so a call site can never
    silently reinterpret one convention as the other.
    """
    tools = list(getattr(definition, "tools", None) or [])
    return tools or None


def merge_authority(base: dict | None, overlay: dict | None) -> dict:
    """Compose an authority overlay onto a base without ever widening it.

    Single source of truth for authority composition (factory build,
    agent-loop ambient merge, delegation). Semantics, by key presence:

    - tools: intersection with None-as-unrestricted (cap_child_authority).
      An explicit ``[]`` overlay yields ``[]`` (never folded to unrestricted).
    - max_mode: rank-min of the two; the present one when either is absent.
      This helper never raises — callers that must reject escalation apply
      check_mode_cap first (agent loop, delegate).
    - full_grant_sources: order-preserving union (base first).
    - terminal_backend: a sandbox base pins the result to sandbox.
    - every other overlay key overrides the base.
    """
    base = dict(base or {})
    overlay = dict(overlay or {})
    merged = {**base, **{key: value for key, value in overlay.items() if key != "tools"}}
    merged["tools"] = cap_child_authority(base, overlay.get("tools"))
    base_max = base.get("max_mode")
    overlay_max = overlay.get("max_mode")
    if base_max is not None and overlay_max is not None:
        base_rank = _MODE_RANK.get(str(base_max).lower(), 0)
        overlay_rank = _MODE_RANK.get(str(overlay_max).lower(), 0)
        merged["max_mode"] = base_max if base_rank <= overlay_rank else overlay_max
    elif base_max is not None:
        merged["max_mode"] = base_max
    sources = list(base.get("full_grant_sources") or [])
    merged["full_grant_sources"] = sources + [
        source for source in (overlay.get("full_grant_sources") or []) if source not in sources
    ]
    if base.get("terminal_backend") == "sandbox":
        merged["terminal_backend"] = "sandbox"
    return merged


_MODE_RANK = {"ask": 0, "workspace": 1, "sandbox": 2, "full": 3}


def check_mode_cap(parent_authority: dict | None, requested_mode: str) -> None:
    """Deny mode escalation above the parent's cap (LP-08, enforced at broker)."""
    if not parent_authority or "max_mode" not in parent_authority:
        return
    cap = _MODE_RANK.get(str(parent_authority.get("max_mode") or "ask").lower(), 0)
    want = _MODE_RANK.get(str(requested_mode or "ask").lower(), 0)
    if want > cap:
        from machina.permissions.broker import ApprovalDenied

        raise ApprovalDenied(f"mode {requested_mode!r} exceeds parent cap")


async def close_agent_provider(agent, timeout: float = 5.0) -> None:
    """Close only owned providers, recording sanitized outcomes within a deadline.

    Claim release remains the caller's independent finally. Resistant cleanup
    remains strongly referenced until it retires; cancellation still propagates.
    """
    import asyncio
    import inspect
    import logging
    import time

    from machina.core.metrics import metrics

    if not getattr(agent, "_owns_provider", False):
        return
    provider = getattr(agent, "provider", None)
    close = getattr(provider, "close", None)
    if not callable(close):
        return
    started = time.monotonic()
    logger = logging.getLogger(__name__)
    identity = type(provider).__name__

    def report(outcome: str, error: BaseException | None = None) -> None:
        elapsed = (time.monotonic() - started) * 1000
        metrics.increment_counter(f"provider.close.{outcome}")
        metrics.record_latency("provider.close", elapsed)
        if outcome != "success":
            logger.warning(
                "owned provider close %s provider=%s elapsed_ms=%.1f error_type=%s",
                outcome,
                identity,
                elapsed,
                type(error).__name__ if error else "none",
            )

    try:
        result = close()
        if inspect.isawaitable(result):
            from machina.core.cleanup import bounded_cleanup

            await bounded_cleanup(result, timeout, "owned provider", report=report)
        else:
            report("success")
    except asyncio.CancelledError:
        raise
    except Exception as error:
        # Async cleanup already reported its outcome; synchronous failures
        # are secondary to the turn and must be reported here.
        if "result" not in locals() or not inspect.isawaitable(result):
            report("failed", error)


async def _close_orphaned_provider(provider, timeout: float = 5.0) -> None:
    """Close a provider whose agent construction failed (F4, 2026-10-15 review).

    ``get_provider`` returns an HTTP-client-owning transport before the
    definition export / ``ReActAgent`` construction that adopts it can fail.
    Reuses the audited owner-aware close contract through a minimal owner shim
    so the pool is released with the same bounded, sanitized reporting. A close
    failure is reported, never masks the construction error being raised.
    """
    from types import SimpleNamespace

    if provider is None:
        return
    await close_agent_provider(SimpleNamespace(provider=provider, _owns_provider=True), timeout)
