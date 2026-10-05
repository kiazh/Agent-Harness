"""Agent-related feature handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ah.gateway.errors import INVALID_PARAMS, NOT_FOUND, RpcError
from ah.gateway.features._common import _int, _str, _uuid

if TYPE_CHECKING:
    from ah.gateway.server import Gateway


def _agent(d: Any) -> dict[str, Any]:
    return d.to_dict()


async def agents_list(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """List all agents."""
    gw.require_db()
    from ah.core.agent_def import agent_registry

    return {"agents": [_agent(a) for a in await agent_registry.list()]}


async def agents_save(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Save a new agent."""
    gw.require_db()
    from ah.core.agent_def import BUILTIN_AGENTS, AgentDef, agent_registry

    tools = params.get("tools", [])
    if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
        raise RpcError(INVALID_PARAMS, "tools must be a list of strings")
    name = _str(params, "name", max_len=100)
    if name in BUILTIN_AGENTS:
        raise RpcError(INVALID_PARAMS, f"{name!r} is a built-in agent and cannot be saved")
    saved = await agent_registry.save(
        AgentDef(
            name=name,
            description=_str(params, "description", required=False, max_len=500),
            system_prompt=_str(params, "systemPrompt", required=False, max_len=10_000),
            tools=tools,
            model=_str(params, "model", required=False, max_len=200) or None,
            provider=_str(params, "provider", required=False, max_len=50) or None,
            max_iterations=_int(params, "maxIterations", 10, 1, 50),
        )
    )
    return {"agent": _agent(saved)}


async def agents_delete(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Delete an agent."""
    gw.require_db()
    from ah.core.agent_def import BUILTIN_AGENTS, agent_registry

    name = _str(params, "name", max_len=100)
    if name in BUILTIN_AGENTS:
        raise RpcError(INVALID_PARAMS, f"{name!r} is a built-in agent and cannot be deleted")
    if not await agent_registry.delete(name):
        raise RpcError(NOT_FOUND, f"agent {name!r} not found")
    return {"deleted": True}


def _delegation(r: Any) -> dict[str, Any]:
    return {
        "agent": r.agent,
        "task": r.task,
        "response": r.response,
        "sessionId": str(r.session_id),
        "tokens": r.tokens,
        "iterations": r.iterations,
        "status": r.status,
    }


def _steps(params: dict[str, Any]) -> list[tuple[str, str]]:
    raw = params.get("steps")
    if not isinstance(raw, list) or not raw:
        raise RpcError(INVALID_PARAMS, "steps must be a non-empty list of {agent, task}")
    if len(raw) > 16:
        raise RpcError(INVALID_PARAMS, "steps must contain at most 16 delegations")
    steps: list[tuple[str, str]] = []
    for item in raw:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("agent"), str)
            or not isinstance(item.get("task"), str)
        ):
            raise RpcError(INVALID_PARAMS, "each step needs a string 'agent' and 'task'")
        steps.append((item["agent"], item["task"]))
    return steps


async def agents_run(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Run delegations sequentially (default) or in parallel."""
    gw.require_db()
    from ah.core.orchestrator import AgentNotFoundError, orchestrator

    steps = _steps(params)
    mode = params.get("mode", "sequential")
    if mode not in ("sequential", "parallel"):
        raise RpcError(INVALID_PARAMS, "mode must be 'sequential' or 'parallel'")
    parent = _uuid(params, "sessionId") if params.get("sessionId") else None
    try:
        if mode == "parallel":
            results = await orchestrator.run_parallel(steps, parent_session_id=parent)
        else:
            results = await orchestrator.run_sequential(steps, parent_session_id=parent)
    except AgentNotFoundError as e:
        raise RpcError(NOT_FOUND, str(e)) from None
    serialized = []
    for (agent, task), result in zip(steps, results, strict=True):
        if isinstance(result, BaseException):
            serialized.append(
                {
                    "agent": agent,
                    "task": task,
                    "response": f"{type(result).__name__}: {result}",
                    "sessionId": None,
                    "tokens": 0,
                    "iterations": 0,
                    "status": "error",
                }
            )
        else:
            serialized.append(_delegation(result))
    return {"results": serialized}


async def agents_history(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Get orchestrator history for a session."""
    gw.require_db()
    from ah.core.orchestrator import orchestrator

    session = await gw.get_session(params)
    return {
        "messages": await orchestrator.history(session.id, limit=_int(params, "limit", 50, 1, 500))
    }
