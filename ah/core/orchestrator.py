"""Multi-agent orchestration (Phase 5).

Builds a :class:`~ah.core.agent.ReActAgent` from an :class:`AgentDef` and runs a
delegated task against a fresh child session, recording the exchange in
``agent_messages``. Supports running several delegations sequentially (each sees
the previous results) or in parallel.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass

from ah.core.agent import ReActAgent
from ah.core.agent_def import AgentDef, agent_registry
from ah.core.assembler import PromptAssembler, get_token_count, truncate_to_tokens
from ah.core.config import config
from ah.core.context import context_manager
from ah.core.session import session_manager
from ah.core.usage import usage_store
from ah.db.connection import db

__all__ = ["DelegationResult", "Orchestrator", "AgentNotFoundError"]

logger = logging.getLogger(__name__)


class AgentNotFoundError(Exception):
    """Raised when a delegation names an agent that does not exist."""


@dataclass
class DelegationResult:
    agent: str
    task: str
    response: str
    session_id: uuid.UUID
    tokens: int
    iterations: int
    status: str  # "complete" or "error"


class Orchestrator:
    """Runs tasks on named agents and records them in ``agent_messages``."""

    MAX_HOP_COUNT = 10  # Maximum delegation depth to prevent circular delegation
    PROMPT_VERSION = "1.0.0"  # Version of the delegation prompt template

    @staticmethod
    def _build_delegation_prompt(agent_name: str, task: str, context: str) -> str:
        """Build a deterministic, versioned delegation prompt.

        Args:
            agent_name: Name of the agent being delegated to.
            task: The task description.
            context: Parent session context (may be empty).

        Returns:
            A versioned prompt string.
        """
        parts = [
            f"## Task",
            f"{task}",
            f"",
            f"# Delegation Prompt v{Orchestrator.PROMPT_VERSION}",
            f"",
            f"## Agent",
            f"{agent_name}",
        ]
        if context:
            parts.extend([
                f"",
                f"## Parent Session Context",
                f"{context}",
            ])
        return "\n".join(parts)

    def __init__(self, agent_factory=None) -> None:
        # Injectable so tests can supply a fake agent without an API key.
        self._agent_factory = agent_factory or self._build_agent

    def _build_agent(self, definition: AgentDef) -> ReActAgent:
        from ah.core.provider import get_provider

        provider_name = definition.provider or config.get("provider")
        model = definition.model or config.get("model")
        return ReActAgent(
            provider=get_provider(provider=provider_name, model=model),
            max_iterations=definition.max_iterations,
            agent_id=definition.name,
            system_prompt=definition.system_prompt or None,
            allowed_tools=definition.tools or None,
        )

    async def delegate(
        self,
        agent_name: str,
        task: str,
        *,
        from_agent: str = "orchestrator",
        parent_session_id: uuid.UUID | None = None,
        _hop_count: int = 0,
    ) -> DelegationResult:
        """Run *task* on *agent_name* in a fresh child session.

        Args:
            agent_name: Name of the agent to delegate to.
            task: The task description.
            from_agent: The agent making this delegation.
            parent_session_id: Optional parent session for context.
            _hop_count: Internal hop counter to prevent circular delegation.

        Raises:
            ValueError: If hop count exceeds MAX_HOP_COUNT (deadlock guard).
        """
        if _hop_count > self.MAX_HOP_COUNT or _hop_count < 0:
            raise ValueError(
                f"Invalid hop count {_hop_count} — "
                f"must be between 0 and {self.MAX_HOP_COUNT}"
            )
        definition = await agent_registry.get(agent_name)
        if definition is None:
            raise AgentNotFoundError(f"no agent named {agent_name!r}")

        handoff = await self._parent_context(parent_session_id) if parent_session_id else ""

        child = await session_manager.create(
            title=f"{agent_name}: {task[:50]}",
            agent_id=agent_name,
            model=definition.model or config.get("model"),
            provider=definition.provider or config.get("provider"),
            context_budget=config.get("context_budget"),
        )
        message_id = await self._record_start(parent_session_id, from_agent, agent_name, task)

        agent = self._agent_factory(definition)
        reservation_id = None
        try:
            child_task = self._build_delegation_prompt(agent_name, task, handoff)
            # Reserve usage for the child session if we have a parent session
            if parent_session_id is not None:
                reservation_id = await usage_store.reserve(
                    child.id, agent_name, "orchestrator", definition.model or config.get("model"),
                    [{"role": "user", "content": child_task}], [], 4096,
                )
            response = await agent.run(child.id, child_task, verbose=False)
        except Exception as e:
            logger.exception("Delegation to %s failed", agent_name)
            if reservation_id is not None:
                await usage_store.finish(reservation_id, failed=True)
            await self._record_end(
                message_id, status="error", response=f"{type(e).__name__}: {e}", tokens=0
            )
            return DelegationResult(agent_name, task, f"Error: {e}", child.id, 0, 0, "error")

        # Finish usage reservation
        if reservation_id is not None:
            await usage_store.finish(
                reservation_id,
                {"total_tokens": response.tokens_used},
                model=definition.model or config.get("model"),
            )

        await self._record_end(
            message_id, status="complete", response=response.content, tokens=response.tokens_used
        )
        if parent_session_id is not None:
            await context_manager.add_chunk(
                session_id=parent_session_id,
                agent_id=from_agent,
                chunk_type="result",
                payload={"agent": agent_name, "content": f"[{agent_name}] {response.content}"},
                token_count=get_token_count(response.content),
            )
        return DelegationResult(
            agent=agent_name,
            task=task,
            response=response.content,
            session_id=child.id,
            tokens=response.tokens_used,
            iterations=response.iterations,
            status="complete",
        )

    async def _parent_context(self, session_id: uuid.UUID) -> str:
        """Pass a bounded summary of recent parent activity to a child agent."""
        parent = await session_manager.get(session_id)
        if parent is None:
            raise ValueError(f"parent session {session_id} not found")
        recent = await context_manager.get_recent_context(session_id, limit=10)
        assembler = PromptAssembler()
        parts = []
        if parent.goal:
            parts.append(f"Goal: {parent.goal}")
        for chunk in reversed(recent):
            if chunk["type"] in {"user_message", "assistant_message"} or (
                chunk["type"] == "result" and "agent" in chunk["payload"]
            ):
                parts.append(assembler._compress_chunk(chunk))
        return truncate_to_tokens("\n".join(parts), 1200)

    async def run_sequential(
        self,
        steps: list[tuple[str, str]],
        *,
        parent_session_id: uuid.UUID | None = None,
    ) -> list[DelegationResult]:
        """Run (agent, task) steps in order; each task is prefixed with prior results."""
        results: list[DelegationResult] = []
        for agent_name, task in steps:
            prompt = task
            if results:
                prior = "\n\n".join(f"### {r.agent} said:\n{r.response}" for r in results)
                prompt = f"{task}\n\n## Results so far\n{prior}"
            results.append(
                await self.delegate(
                    agent_name,
                    prompt,
                    from_agent="orchestrator",
                    parent_session_id=parent_session_id,
                )
            )
        return results

    async def run_parallel(
        self,
        tasks: list[tuple[str, str]],
        *,
        parent_session_id: uuid.UUID | None = None,
    ) -> list[DelegationResult]:
        """Run (agent, task) delegations concurrently."""
        return list(
            await asyncio.gather(
                *(
                    self.delegate(
                        agent_name,
                        task,
                        from_agent="orchestrator",
                        parent_session_id=parent_session_id,
                    )
                    for agent_name, task in tasks
                ),
                return_exceptions=True,
            )
        )

    # ─── persistence ──────────────────────────────────────────────────────
    async def _record_start(
        self, session_id: uuid.UUID | None, from_agent: str, to_agent: str, task: str
    ) -> uuid.UUID:
        message_id = uuid.uuid4()
        await db.execute(
            """
            INSERT INTO agent_messages (id, session_id, from_agent, to_agent, task, status)
            VALUES ($1, $2, $3, $4, $5, 'pending')
            """,
            message_id,
            session_id,
            from_agent,
            to_agent,
            task,
        )
        return message_id

    async def _record_end(
        self, message_id: uuid.UUID, *, status: str, response: str, tokens: int
    ) -> None:
        await db.execute(
            """
            UPDATE agent_messages
            SET status = $2, response = $3, tokens_used = $4, completed_at = now()
            WHERE id = $1
            """,
            message_id,
            status,
            response,
            tokens,
        )

    async def history(self, session_id: uuid.UUID, limit: int = 50) -> list[dict]:
        rows = await db.fetch(
            """
            SELECT id, from_agent, to_agent, task, response, status, tokens_used, created_at, completed_at
            FROM agent_messages WHERE session_id = $1 ORDER BY created_at ASC LIMIT $2
            """,
            session_id,
            limit,
        )
        return [
            {
                "id": str(r["id"]),
                "fromAgent": r["from_agent"],
                "toAgent": r["to_agent"],
                "task": r["task"],
                "response": r["response"],
                "status": r["status"],
                "tokens": r["tokens_used"],
                "createdAt": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ]


orchestrator = Orchestrator()
