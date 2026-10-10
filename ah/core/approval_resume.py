"""Exact-action HTTP continuation using the existing agent tool pipeline."""

from __future__ import annotations

from typing import Any

from ah.core.execution_context import resolve_execution_context
from ah.core.models import AgentResponse, LLMResponse, StreamEvent
from ah.permissions import store
from ah.permissions.policy import build_request
from ah.permissions.tools import action_for_tool


async def resumed_request(record: dict, arguments: dict[str, Any]):
    """Rebuild every material field from live configuration and client inputs."""
    tool = record["proposal"]["tool"]
    spec = action_for_tool(tool, arguments)
    context = await resolve_execution_context(record["session_id"], tool)
    return build_request(
        **spec,
        mode=context.mode,
        backend=context.backend,
        tool=tool,
        agent_id=record["agent_id"],
        session_id=record["session_id"],
        turn_id=record["turn_id"],
    )


def matches_approval(request, record: dict) -> bool:
    return (
        request.session_id == record.get("session_id")
        and request.agent_id == record.get("agent_id")
        and request.digest == record.get("digest")
        and store.proposal_of(request) == record.get("proposal")
    )


async def resume_action_stream(agent, session_id, record: dict, arguments: dict):
    """Continue one reviewed tool without replaying a prompt or provider request.

    Reuse definition restrictions, broker, registry, audit, metrics, history,
    deadline, and cancellation paths. Raw arguments come from the authenticated
    client and are checked against the durable digest; they are not persisted as
    an unredacted continuation payload.
    """
    made: list[dict] = []
    response = LLMResponse(
        content="",
        model="approval-continuation",
        tool_calls=[
            {
                "id": record["request_id"],
                "type": "function",
                "function": {"name": record["proposal"]["tool"], "arguments": arguments},
            }
        ],
    )
    async for event in agent._execute_tool_calls_stream(response, [], made, session_id):
        yield event
    pauses = [entry["needs_approval"] for entry in made if "needs_approval" in entry]
    yield StreamEvent(
        type="done",
        response=AgentResponse(
            content=made[0]["result_preview"] if made else "continuation ended",
            tool_calls=made,
            needs_approval=pauses,
        ),
    )
