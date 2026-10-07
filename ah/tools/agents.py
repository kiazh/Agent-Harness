"""Multi-agent tools: delegate a task to another agent, or list agents."""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextvars import ContextVar

from ah.core.exceptions import ToolError
from ah.db.connection import db
from ah.tools.base import registry

logger = logging.getLogger(__name__)

# Set only while the current agent executes a tool. ContextVar keeps concurrent
# sessions isolated and avoids accepting a model-supplied parent session ID.
current_session_id: ContextVar[uuid.UUID | None] = ContextVar("agent_session_id", default=None)
current_agent_id: ContextVar[str | None] = ContextVar("agent_tool_owner_id", default=None)


async def active_agent_scope() -> tuple[str, uuid.UUID]:
    """Derive a tool caller from the executing agent and its owned session."""
    session_id = current_session_id.get()
    agent_id = current_agent_id.get()
    if session_id is None or agent_id is None:
        raise ToolError("tool requires an active agent session")
    session_owner = await db.fetchval("SELECT agent_id FROM sessions WHERE id = $1", session_id)
    if session_owner != agent_id:
        raise ToolError("active session does not belong to the running agent")
    return agent_id, session_id


@registry.register(
    name="delegate",
    description=(
        "Hand a self-contained task to another agent and get its result. Use for work "
        "that suits a specialist (e.g. 'researcher' to gather info, 'coder' to edit code). "
        "Call list_agents first to see who is available."
    ),
    parameters={
        "type": "object",
        "properties": {
            "agent": {"type": "string", "description": "Name of the agent to delegate to"},
            "task": {
                "type": "string",
                "description": "A complete, standalone description of the task",
            },
        },
        "required": ["agent", "task"],
    },
)
async def delegate(agent: str, task: str) -> str:
    """Delegate *task* to *agent* and return its response."""
    from ah.core.orchestrator import AgentNotFoundError, delegation_depth, orchestrator

    if not task.strip():
        raise ToolError("task must not be empty")
    parent_session_id = current_session_id.get()
    from_agent = "delegate-tool"
    if parent_session_id is not None:
        from_agent, parent_session_id = await active_agent_scope()
    # LP-08: propagate the executing parent's authority so the child cannot
    # escalate through delegation, HTTP/RPC, jobs, or raw helper calls.
    try:
        from ah.core.agent_factory import parent_authority_var

        parent_authority = parent_authority_var.get()
    except Exception:
        parent_authority = None
    try:
        result = await asyncio.wait_for(
            orchestrator.delegate(
                agent,
                task,
                from_agent=from_agent,
                parent_session_id=parent_session_id,
                _hop_count=delegation_depth.get() + 1,
                authority=parent_authority,
            ),
            timeout=60,
        )
    except AgentNotFoundError as e:
        raise ToolError(f"{e}. Use list_agents to see available agents.") from None
    except Exception as e:
        logger.exception("delegate tool failed")
        raise ToolError(f"delegation failed: {e}") from None

    if result.status == "error":
        return f"[{agent} failed] {result.response}"
    return f"[{agent} responded]\n{result.response}"


@registry.register(
    name="list_agents",
    description="List the agents you can delegate to, with their descriptions.",
    parameters={"type": "object", "properties": {}},
)
async def list_agents() -> str:
    """Return the available agents and what they are for."""
    from ah.core.agent_def import agent_registry

    agents = await agent_registry.list()
    if not agents:
        return "No agents are available."
    return "\n".join(f"- {a.name}: {a.description or '(no description)'}" for a in agents)


@registry.register(
    name="share_memory",
    description="Share one signed memory you own with another agent. Delivery checks provenance and the recipient's identity gate.",
    parameters={
        "type": "object",
        "properties": {
            "memory_id": {"type": "string", "description": "Memory UUID from remember or recall"},
            "recipient_agent": {"type": "string", "description": "Agent to receive this memory"},
        },
        "required": ["memory_id", "recipient_agent"],
    },
)
async def share_memory(memory_id: str, recipient_agent: str) -> str:
    """Deliver a memory through the durable per-recipient identity gate."""
    from ah.memory.shared_bus import shared_memory_bus

    agent_id, _ = await active_agent_scope()
    try:
        source_id = uuid.UUID(memory_id)
    except ValueError:
        raise ToolError("memory_id must be a UUID") from None
    try:
        receipt = await shared_memory_bus.deliver(
            source_id, recipient_agent, publisher_agent=agent_id
        )
    except (ValueError, PermissionError) as exc:
        raise ToolError(str(exc)) from None
    return (
        f"Memory delivery {receipt.status} for {recipient_agent}; "
        f"receipt source={receipt.source_memory_id}; "
        f"target={receipt.target_memory_id or '-'}; {receipt.reason}"
    )


registry.declare_effects(
    {
        "delegate": ("delegate",),
        "list_agents": (),
        "share_memory": ("memory",),
    }
)
