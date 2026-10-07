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
from contextvars import ContextVar
from dataclasses import dataclass

from ah.core.agent import ReActAgent
from ah.core.agent_def import AgentDef, agent_registry
from ah.core.assembler import PromptAssembler, get_token_count, truncate_to_tokens
from ah.core.config import config
from ah.core.context import context_manager
from ah.core.session import session_manager
from ah.db.connection import db

__all__ = ["DelegationResult", "Orchestrator", "AgentNotFoundError"]

logger = logging.getLogger(__name__)
delegation_depth: ContextVar[int] = ContextVar("delegation_depth", default=0)
delegation_deadline: ContextVar[float | None] = ContextVar("delegation_deadline", default=None)


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
    DELEGATION_TIMEOUT_SECONDS = 120.0
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
            "## Task",
            f"{task}",
            "",
            f"# Delegation Prompt v{Orchestrator.PROMPT_VERSION}",
            "",
            "## Agent",
            f"{agent_name}",
        ]
        if context:
            parts.extend(
                [
                    "",
                    "## Parent Session Context",
                    f"{context}",
                ]
            )
        return "\n".join(parts)

    def __init__(self, agent_factory=None) -> None:
        # Injectable so tests can supply a fake agent without an API key.
        # AH-023: only the default factory creates owned providers that this
        # scope must close; injected factories manage their own lifecycle.
        # Phase 4.5/6: default path uses the shared definition-aware factory
        # (same pipeline, permission broker, accounting, cancellation).
        self._owns_factory_agents = agent_factory is None
        self._agent_factory = agent_factory

    async def _default_agent(self, definition: AgentDef, authority: dict | None = None):
        from ah.core.agent_factory import build_agent_for_session, cap_child_authority
        from ah.core.models import Session as _Session

        tools = cap_child_authority(authority or {}, definition.tools or None)
        # Build through the shared factory for identical dependency wiring;
        # synthesize a session view carrying the child identity.
        import uuid as _uuid

        view = _Session(
            id=_uuid.uuid4(),
            agent_id=definition.name,
            model=definition.model,
            provider=definition.provider,
        )
        agent = await build_agent_for_session(view, authority=authority)
        # Enforce the capped tool list (definition ∩ parent caps).
        if tools is not None:
            agent.allowed_tools = tools
        return agent

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
        authority: dict | None = None,
    ) -> DelegationResult:
        """Run *task* on *agent_name* in a fresh child session.

        Args:
            agent_name: Name of the agent to delegate to.
            task: The task description.
            from_agent: The agent making this delegation.
            parent_session_id: Optional parent session for context.
            _hop_count: Internal hop counter to prevent circular delegation.
            authority: Explicit parent authority caps. When omitted, the
                ambient delegation authority (LP-08) applies.

        Raises:
            ValueError: If hop count exceeds MAX_HOP_COUNT (deadlock guard).
        """
        if _hop_count > self.MAX_HOP_COUNT or _hop_count < 0:
            raise ValueError(
                f"Invalid hop count {_hop_count} — must be between 0 and {self.MAX_HOP_COUNT}"
            )
        try:
            definition = await agent_registry.get(agent_name)
        except Exception as e:
            # Fail closed like the shared factory: an unresolvable specialist
            # is a controlled error, never an unrestricted agent.
            from ah.core.agent_factory import AgentDefinitionError

            raise AgentDefinitionError(f"agent definition {agent_name!r} unavailable: {e}") from e
        if definition is None:
            from ah.core.agent_def import BUILTIN_AGENTS

            if agent_name not in BUILTIN_AGENTS:
                raise AgentNotFoundError(f"no agent named {agent_name!r}")
            definition = BUILTIN_AGENTS[agent_name]

        handoff = await self._parent_context(parent_session_id) if parent_session_id else ""

        child = await session_manager.create(
            title=f"{agent_name}: {task[:50]}",
            agent_id=agent_name,
            model=definition.model or config.get("model"),
            provider=definition.provider or config.get("provider"),
            context_budget=config.get("context_budget"),
        )
        message_id = await self._record_start(parent_session_id, from_agent, agent_name, task)

        # Phase 4.5/6 + LP-08: same factory/accounting/cancellation pipeline
        # as normal turns. Child authority = definition ∩ parent caps, and the
        # broker enforces it at execution (not just on the stored agent).
        from ah.core.agent_factory import cap_child_authority, parent_authority_var

        ambient = parent_authority_var.get() or {}
        parent = dict(ambient)
        if authority:
            parent.update(authority)
        from ah.core.agent_factory import check_mode_cap

        check_mode_cap(parent or None, "ask")  # delegation itself stays ask-level
        capped_tools = cap_child_authority(parent, definition.tools or None)
        child_authority: dict = {"tools": capped_tools}
        if parent.get("max_mode") is not None:
            child_authority["max_mode"] = parent["max_mode"]
        if self._agent_factory is not None:
            agent = self._agent_factory(definition)
        else:
            agent = await self._default_agent(definition, authority=child_authority)
        depth_token = delegation_depth.set(_hop_count)
        loop = asyncio.get_running_loop()
        deadline_token = delegation_deadline.set(
            delegation_deadline.get() or loop.time() + self.DELEGATION_TIMEOUT_SECONDS
        )
        authority_token = parent_authority_var.set(child_authority)
        try:
            child_task = self._build_delegation_prompt(agent_name, task, handoff)
            response = await asyncio.wait_for(
                agent.run(child.id, child_task, verbose=False),
                timeout=max(0, delegation_deadline.get() - loop.time()),
            )
        except asyncio.CancelledError:
            await self._record_end(message_id, status="cancelled", response="cancelled", tokens=0)
            raise
        except Exception as e:
            logger.exception("Delegation to %s failed", agent_name)
            detail = "delegation timed out" if isinstance(e, TimeoutError) else str(e)
            await self._record_end(
                message_id, status="error", response=f"{type(e).__name__}: {detail}", tokens=0
            )
            return DelegationResult(agent_name, task, f"Error: {detail}", child.id, 0, 0, "error")
        finally:
            delegation_depth.reset(depth_token)
            delegation_deadline.reset(deadline_token)
            try:
                from ah.core.agent_factory import parent_authority_var as _pav

                _pav.reset(authority_token)
            except Exception:
                pass
            # AH-023: close only factory-owned providers. Custom factories
            # (tests) inject shared agents that must stay open.
            if getattr(self, "_owns_factory_agents", False):
                try:
                    prov = getattr(agent, "provider", None)
                    close = getattr(prov, "close", None)
                    if callable(close):
                        import inspect as _inspect

                        r = close()
                        if _inspect.isawaitable(r):
                            await r
                except Exception:
                    pass

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
        _hop_count: int = 0,
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
                    _hop_count=_hop_count + 1,
                )
            )
        return results

    async def run_parallel(
        self,
        tasks: list[tuple[str, str]],
        *,
        parent_session_id: uuid.UUID | None = None,
        _hop_count: int = 0,
    ) -> list[DelegationResult | Exception]:
        """Run (agent, task) delegations concurrently.

        Children inherit ``_hop_count + 1`` so circular delegation is still
        bounded. Individual failures are returned as exceptions in place
        (return_exceptions=True) so callers never lose results.
        """
        raw = await asyncio.gather(
            *(
                self.delegate(
                    agent_name,
                    task,
                    from_agent="orchestrator",
                    parent_session_id=parent_session_id,
                    _hop_count=_hop_count + 1,
                )
                for agent_name, task in tasks
            ),
            return_exceptions=True,
        )
        return list(raw)

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
