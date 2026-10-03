"""Multi-agent tools: delegate a task to another agent, or list agents."""

from __future__ import annotations

import logging
import uuid
from contextvars import ContextVar

from ah.core.exceptions import ToolError
from ah.tools.base import registry

logger = logging.getLogger(__name__)

# Set only while the current agent executes a tool. ContextVar keeps concurrent
# sessions isolated and avoids accepting a model-supplied parent session ID.
current_session_id: ContextVar[uuid.UUID | None] = ContextVar("agent_session_id", default=None)


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
    from ah.core.orchestrator import AgentNotFoundError, orchestrator

    if not task.strip():
        raise ToolError("task must not be empty")
    try:
        result = await orchestrator.delegate(
            agent, task, from_agent="delegate-tool", parent_session_id=current_session_id.get()
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
