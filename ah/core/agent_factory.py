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

from ah.core.models import Session

# Inherited authority for the currently executing (parent) agent, set by the
# orchestrator around delegated child runs and read by the broker at the
# execution boundary (LP-08). Tools/mode outside these caps are denied even
# when a session grant would otherwise allow them.
parent_authority_var: _ContextVar[dict | None] = _ContextVar("parent_authority", default=None)


def _memory_enabled() -> bool:
    try:
        from ah.core.config import config

        return bool(config.get("memory_enabled"))
    except Exception:
        return True


def _rag_enabled() -> bool:
    try:
        from ah.core.config import config

        return bool(config.get("rag_enabled"))
    except Exception:
        return True


async def _shared_rag_pipeline():
    """Shared RAG service, initialized lazily (LP-10).

    Creates the pipeline on first use when enabled (so a cold restart still
    retrieves already-indexed documents through ordinary prompts), or returns
    None when disabled/unavailable — callers keep the keyword path and report
    degraded status honestly instead of failing startup.
    """
    if not _rag_enabled():
        return None
    try:
        from ah.tools.rag import get_rag_pipeline

        return await get_rag_pipeline()
    except Exception:
        return None


class AgentDefinitionError(Exception):
    """Raised when a requested specialist definition cannot be resolved."""


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
    raises AgentDefinitionError — never broad all-tool access. Only the
    general-purpose default (``harness``) falls back to unrestricted tools.
    Full-host user grants never widen a restrictive child definition.
    """
    from ah.core.agent import ReActAgent
    from ah.core.agent_def import agent_registry
    from ah.core.config import config
    from ah.core.provider import get_provider

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
        from ah.core.agent_def import BUILTIN_AGENTS

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
        allowed_tools = definition.tools or None
        max_iterations = definition.max_iterations
        agent_id = definition.name
    else:
        from ah.core.config import config as _cfg

        provider_name = provider_override or session.provider or _cfg.get("provider")
        model = model_override or session.model or _cfg.get("model")
        system_prompt = None
        allowed_tools = None
        max_iterations = _cfg.get("max_iterations")
        agent_id = session.agent_id

    # Shared dependencies (lazy, never eager cloud instantiation).
    memory_retriever = None
    memory_consolidator = None
    if _memory_enabled():
        try:
            from ah.memory.retriever import MemoryRetriever
            from ah.memory.store import memory_store

            memory_retriever = MemoryRetriever(store=memory_store)
        except Exception:
            memory_retriever = None
        try:
            from ah.memory.consolidator import MemoryConsolidator

            memory_consolidator = MemoryConsolidator()
        except Exception:
            memory_consolidator = None

    rag_pipeline = _shared_rag_pipeline()

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

    llm = get_provider(provider=provider_name, model=model)
    agent = ReActAgent(
        provider=llm,
        max_iterations=max_iterations,
        agent_id=agent_id,
        system_prompt=system_prompt,
        memory_retriever=memory_retriever,
        memory_consolidator=memory_consolidator,
        rag_pipeline=rag_pipeline,
        allowed_tools=allowed_tools,
    )
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


_MODE_RANK = {"ask": 0, "workspace": 1, "sandbox": 2, "full": 3}


def check_mode_cap(parent_authority: dict | None, requested_mode: str) -> None:
    """Deny mode escalation above the parent's cap (LP-08, enforced at broker)."""
    if not parent_authority or "max_mode" not in parent_authority:
        return
    cap = _MODE_RANK.get(str(parent_authority.get("max_mode") or "ask").lower(), 0)
    want = _MODE_RANK.get(str(requested_mode or "ask").lower(), 0)
    if want > cap:
        from ah.permissions.broker import ApprovalDenied

        raise ApprovalDenied(f"mode {requested_mode!r} exceeds parent cap")


async def close_agent_provider(agent) -> None:
    """Close an agent's provider if this scope owns it (AH-023)."""
    if not getattr(agent, "_owns_provider", False):
        return
    provider = getattr(agent, "provider", None)
    close = getattr(provider, "close", None)
    if callable(close):
        try:
            import inspect

            result = close()
            if inspect.isawaitable(result):
                await result
        except Exception:
            pass
