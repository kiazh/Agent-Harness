"""Definition-aware agent factory (AH-022).

Direct session execution (REST prompt_session, gateway _run_turn) must honor
the session's agent definition (allowed_tools, system prompt, model/provider
overrides). Executing a specialist child session with a bare ReActAgent
would bypass its restricted tool definition.
"""

from __future__ import annotations

from ah.core.models import Session


async def build_agent_for_session(
    session: Session,
    *,
    provider_override: str | None = None,
    model_override: str | None = None,
):
    """Build a ReActAgent honoring the session's agent definition."""
    from ah.core.agent import ReActAgent
    from ah.core.agent_def import agent_registry
    from ah.core.config import config
    from ah.core.provider import get_provider

    definition = None
    try:
        definition = await agent_registry.get(session.agent_id)
    except Exception:
        definition = None

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

    llm = get_provider(provider=provider_name, model=model)
    agent = ReActAgent(
        provider=llm,
        max_iterations=max_iterations,
        agent_id=agent_id,
        system_prompt=system_prompt,
        allowed_tools=allowed_tools,
    )
    # Mark ownership so callers close only providers they created (AH-023).
    agent._owns_provider = True  # type: ignore[attr-defined]
    return agent


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
