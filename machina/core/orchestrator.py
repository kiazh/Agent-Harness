"""Multi-agent orchestration (Phase 5).

Builds a :class:`~machina.core.agent.ReActAgent` from an :class:`AgentDef` and runs a
delegated task against a fresh child session, recording the exchange in
``agent_messages``. Supports running several delegations sequentially (each sees
the previous results) or in parallel.
"""

from __future__ import annotations

import asyncio
import logging
import os
import stat
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field

from machina.core.agent_def import AgentDef, agent_registry
from machina.core.assembler import PromptAssembler, get_token_count, truncate_to_tokens
from machina.core.background_tasks import join_agent_background_tasks
from machina.core.config import config
from machina.core.context import context_manager
from machina.core.exceptions import DelegationAccountingError
from machina.core.session import session_manager
from machina.db.connection import db
from machina.observability.diagnostics import record_failure

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
    status: str  # "complete", "waiting_approval", or "error"
    needs_approval: list[dict] = field(default_factory=list)


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
        from machina.core.agent_factory import (
            build_agent_for_session,
            cap_child_authority,
            definition_tool_cap,
        )
        from machina.core.models import Session as _Session

        tools = cap_child_authority(authority or {}, definition_tool_cap(definition))
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

    @staticmethod
    def _check_expected_artifact(expected_artifact_path: str) -> tuple[bool, str]:
        """Verify the contract artifact exists and is non-empty.

        Resolves via the file backend's :func:`resolve_path` (same
        workspace binding as read/write tools), then opens the resolved
        path with the backend's O_NOFOLLOW guarded reader and validates the
        descriptor (regular file, non-empty, dev/ino still matches the path).
        This closes the wave-2 stat-then-swap TOCTOU window: a symlink or
        swapped entry fails the guarded open instead of being stat'd through.
        Returns (ok, reason).
        """
        from machina.tools.file import _open_secure_read, resolve_path

        try:
            candidate = resolve_path(expected_artifact_path)
        except ValueError as e:
            return False, f"Error: expected artifact '{expected_artifact_path}' invalid: {e}"
        fd = None
        try:
            fd = _open_secure_read(candidate)
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                return (
                    False,
                    f"Error: expected artifact '{expected_artifact_path}' missing or empty"
                    " (not a file)",
                )
            if st.st_size == 0:
                return (
                    False,
                    f"Error: expected artifact '{expected_artifact_path}' missing or empty",
                )
        except (OSError, ValueError) as e:
            return (
                False,
                f"Error: expected artifact '{expected_artifact_path}' missing or empty: {e}",
            )
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        return True, ""

    async def delegate(
        self,
        agent_name: str,
        task: str,
        *,
        from_agent: str = "orchestrator",
        parent_session_id: uuid.UUID | None = None,
        _hop_count: int = 0,
        authority: dict | None = None,
        expected_output: str | None = None,
        expected_artifact_path: str | None = None,
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
            expected_output: Free-text description of the required result
                shape (dspy-Signature output contract; documented to the
                child, never prose-sniffed).
            expected_artifact_path: Workspace-relative file the child must
                produce; verified present and non-empty after a non-paused
                child completes. ``None`` disables verification (unchanged).

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
            from machina.core.agent_factory import AgentDefinitionError

            raise AgentDefinitionError(f"agent definition {agent_name!r} unavailable: {e}") from e
        if definition is None:
            from machina.core.agent_def import BUILTIN_AGENTS

            if agent_name not in BUILTIN_AGENTS:
                raise AgentNotFoundError(f"no agent named {agent_name!r}")
            definition = BUILTIN_AGENTS[agent_name]

        handoff = await self._parent_context(parent_session_id) if parent_session_id else ""

        from machina.core.agent_factory import (
            cap_child_authority,
            check_mode_cap,
            definition_tool_cap,
            merge_authority,
            parent_authority_var,
        )
        from machina.core.session_mode import resolve_effective_mode

        parent = dict(parent_authority_var.get() or {})
        if authority:
            # An explicit restriction cannot erase an ambient restriction.
            if authority.get("max_mode") is not None:
                check_mode_cap(parent, authority["max_mode"])
        # Unconditional: merge_authority always materializes full_grant_sources,
        # which downstream inheritance relies on.
        parent = merge_authority(parent, authority or {})
        parent_mode = await resolve_effective_mode(
            str(parent_session_id) if parent_session_id else None
        )
        # A cap can restrict an otherwise broader durable mode. Never switch a
        # sandbox parent to a host mode merely because the global default changed.
        try:
            check_mode_cap(parent, parent_mode)
        except Exception as exc:
            record_failure("orchestrator.check_mode_cap", exc)
            parent_mode = str(parent.get("max_mode") or "ask")
        child_authority = dict(parent)
        child_authority["max_mode"] = parent_mode
        child_authority["tools"] = cap_child_authority(parent, definition_tool_cap(definition))
        if parent_mode == "sandbox" or config.get("terminal_sandbox") == "docker":
            child_authority["terminal_backend"] = "sandbox"

        inherited_grant = None
        if parent_mode == "full" and parent_session_id is not None:
            from machina.permissions import store as permission_store

            parent_session = await session_manager.get_fresh(parent_session_id)
            for grant in await permission_store.list_grants(str(parent_session_id)):
                if (
                    parent_session is not None
                    and grant.get("agent_id") == parent_session.agent_id
                    and grant.get("mode") == "full"
                    and grant.get("capability") == "session"
                    and grant.get("digest") == "full-mode-session"
                ):
                    inherited_grant = grant
                    child_authority["full_grant_sources"].append(
                        {
                            "session_id": str(parent_session_id),
                            "agent_id": parent_session.agent_id,
                            "grant_id": str(grant["id"]),
                        }
                    )
                    break

        from machina.core.agent_factory import resolve_provider_model

        child_provider, child_model = resolve_provider_model(definition=definition)
        child = await session_manager.create(
            title=f"{agent_name}: {task[:50]}",
            agent_id=agent_name,
            model=child_model,
            provider=child_provider,
            context_budget=config.get("context_budget"),
            execution_mode=parent_mode,
            parent_session_id=parent_session_id,
            delegation_authority=child_authority,
        )
        # Phase 4.5/6 + LP-08: same factory/accounting/cancellation pipeline
        # as normal turns. Child authority = definition ∩ parent caps, and the
        # broker enforces it at execution (not just on the stored agent).
        # Build the agent BEFORE recording the start row (m3): a construction
        # failure cannot leave a pending agent_messages row.
        # F12 (2026-10-15 review): the ownership cleanup scope opens the moment
        # the child session + owned provider exist — it also covers recording
        # the start row. A build failure, a DB error in ``_record_start``, or
        # cancellation during that insert closes the owned provider and
        # archives the fresh child instead of leaking/orphaning them. The
        # inherited-grant copy is inside the scope too: a failure there has the
        # same orphan risk and needs the same archive-before-raise contract.
        # ``message_id`` stays None until the child actually ran; only then is
        # the (now durable) child session kept.
        agent = None
        message_id: uuid.UUID | None = None
        depth_token = None
        deadline_token = None
        authority_token = None
        try:
            if inherited_grant is not None:
                await permission_store.save_grant(
                    {
                        "session_id": str(child.id),
                        "agent_id": agent_name,
                        "mode": "full",
                        "capability": "session",
                        "scope_path": None,
                        "scope_type": "file",
                        "grant_kind": "session",
                        "digest": "full-mode-session",
                        "expires_at": inherited_grant.get("expires_at"),
                    }
                )
            if self._agent_factory is not None:
                agent = self._agent_factory(definition)
            else:
                agent = await self._default_agent(definition, authority=child_authority)
            message_id = await self._record_start(parent_session_id, from_agent, agent_name, task)
            depth_token = delegation_depth.set(_hop_count)
            loop = asyncio.get_running_loop()
            deadline_token = delegation_deadline.set(
                delegation_deadline.get() or loop.time() + self.DELEGATION_TIMEOUT_SECONDS
            )
            authority_token = parent_authority_var.set(child_authority)
            child_task = self._build_delegation_prompt(agent_name, task, handoff)
            if expected_output:
                child_task += f"\n\n## Expected Output\n{expected_output}"
            if expected_artifact_path:
                child_task += f"\n\n## Expected Artifact\n{expected_artifact_path}"
            response = await asyncio.wait_for(
                agent.run(child.id, child_task, verbose=False),
                timeout=max(0, delegation_deadline.get() - loop.time()),
            )
        except asyncio.CancelledError:
            if message_id is not None:
                try:
                    tokens = await self._charged_child_tokens(child.id, agent_name)
                    await self._record_end(
                        message_id, status="cancelled", response="cancelled", tokens=tokens
                    )
                except DelegationAccountingError as accounting_error:
                    # R4-029: spend unknown — close the row with NULL tokens rather
                    # than strand it; the cancellation itself still propagates.
                    await self._close_row_with_unknown_spend(
                        message_id, status="cancelled", response="cancelled"
                    )
                    record_failure("orchestrator.delegate", accounting_error)
                except Exception as accounting_error:
                    # Accounting failures must not swallow the parent's cancellation.
                    record_failure("orchestrator.delegate", accounting_error)
            raise
        except Exception as e:
            if message_id is None:
                # Build or start-row failure: no row was recorded and the child
                # never ran; re-raise so the finally archives/closes cleanly.
                raise
            logger.exception("Delegation to %s failed", agent_name)
            detail = "delegation timed out" if isinstance(e, TimeoutError) else str(e)
            try:
                tokens = await self._charged_child_tokens(child.id, agent_name)
            except DelegationAccountingError as accounting_error:
                # R4-029: spend unknown — close the row before re-raising so
                # the parent stops spending without a stranded delegation.
                await self._close_row_with_unknown_spend(
                    message_id, status="error", response=f"Error: {accounting_error}"
                )
                raise
            await self._record_end(
                message_id, status="error", response=f"{type(e).__name__}: {detail}", tokens=tokens
            )
            return DelegationResult(
                agent_name, task, f"Error: {detail}", child.id, tokens, 0, "error"
            )
        finally:
            if depth_token is not None:
                delegation_depth.reset(depth_token)
            if deadline_token is not None:
                delegation_deadline.reset(deadline_token)
            if authority_token is not None:
                try:
                    from machina.core.agent_factory import parent_authority_var as _pav

                    _pav.reset(authority_token)
                except Exception as _boundary_error:
                    # Optional fallback preserves the primary outcome; report no payload.
                    record_failure("orchestrator.delegate", _boundary_error)
            # Consolidation shares the agent provider; join it (bounded)
            # before any close so extraction never calls a closed client.
            if agent is not None:
                try:
                    await join_agent_background_tasks(agent)
                except Exception as _boundary_error:
                    # Optional fallback preserves the primary outcome; report no payload.
                    record_failure("orchestrator.delegate", _boundary_error)
            # AH-AUDIT-026: shared owner-aware cleanup contract (bounded;
            # custom test factories inject shared agents that stay open).
            if getattr(self, "_owns_factory_agents", False) and agent is not None:
                try:
                    from machina.core.agent_factory import close_agent_provider

                    await close_agent_provider(agent)
                except Exception as _boundary_error:
                    # Optional fallback preserves the primary outcome; report no payload.
                    record_failure("orchestrator.delegate", _boundary_error)
            # F12: the child never ran (build or start-recording failed); the
            # fresh session is archived so it is not orphaned active.
            if message_id is None:
                try:
                    await session_manager.archive(child.id)
                except Exception as _cleanup_error:
                    # Optional fallback preserves the primary outcome; report no payload.
                    record_failure("orchestrator.delegate.build", _cleanup_error)

        try:
            tokens = await self._charged_child_tokens(child.id, agent_name, response.tokens_used)
        except DelegationAccountingError as accounting_error:
            # R4-029: the child already ran; the spend cannot be read. Close
            # the row with NULL tokens (explicitly unknown, never zero) and
            # re-raise so the parent stops spending.
            await self._close_row_with_unknown_spend(
                message_id, status="error", response=f"Error: {accounting_error}"
            )
            raise
        pauses = getattr(response, "needs_approval", None)
        pauses = (
            [dict(pause) for pause in pauses if isinstance(pause, dict)]
            if isinstance(pauses, list)
            else []
        )
        for pause in pauses:
            pause.setdefault("session_id", str(child.id))
            pause.setdefault("agent_id", agent_name)
        # Typed outcomes (never prose): a failed child is an error even when
        # its body sounds benign. Pauses keep waiting_approval semantics.
        outcome = getattr(response, "outcome", "completed")
        status = "waiting_approval" if pauses else ("error" if outcome == "failed" else "complete")
        final_content = response.content
        if status == "complete" and expected_artifact_path is not None:
            _ok, _reason = self._check_expected_artifact(expected_artifact_path)
            if not _ok:
                status = "error"
                final_content = _reason
        await self._record_end(message_id, status=status, response=final_content, tokens=tokens)
        if parent_session_id is not None:
            await context_manager.add_chunk(
                session_id=parent_session_id,
                agent_id=from_agent,
                chunk_type="result",
                payload={"agent": agent_name, "content": f"[{agent_name}] {final_content}"},
                token_count=get_token_count(final_content),
            )
        return DelegationResult(
            agent=agent_name,
            task=task,
            response=final_content,
            session_id=child.id,
            tokens=tokens,
            iterations=response.iterations,
            status=status,
            needs_approval=pauses,
        )

    async def _charged_child_tokens(
        self, session_id: uuid.UUID, agent_name: str, known_tokens: int = 0
    ) -> int:
        """Read spend for this fresh child, including descendant reservations.

        Reading the ledger adds no wrapper charge. The returned response may
        already include descendants, so take the maximum rather than adding it.
        """
        from machina.core.usage import db as usage_db
        from machina.core.usage import usage_store

        try:
            summary = await usage_store.summary(session_id, agent_name)
            return max(known_tokens, int(summary["session"]["chargedTokens"]))
        except Exception as error:
            if usage_db.connected:
                raise DelegationAccountingError(
                    "delegation usage accounting unavailable"
                ) from error
            # Explicitly DB-less runs have no durable ledger; retain any known
            # successful response usage supplied by the injected child agent.
            return known_tokens

    async def _close_row_with_unknown_spend(
        self, message_id: uuid.UUID, *, status: str, response: str
    ) -> None:
        """Terminal row when post-run child spend cannot be read (R4-029).

        The child already ran, so the row must not stay non-terminal.
        ``tokens_used`` is recorded as NULL — explicitly unknown, never zero.
        A failed bookkeeping write is reported; the caller re-raises the typed
        accounting error so the parent stops spending.
        """
        try:
            await self._record_end(message_id, status=status, response=response, tokens=None)
        except Exception as record_error:
            record_failure("orchestrator.delegate.accounting_row", record_error)

    async def _parent_context(self, session_id: uuid.UUID) -> str:
        """Pass a bounded summary of recent parent activity to a child agent."""
        # AH-AUDIT-019: delegation reads current durable parent state.
        parent = await session_manager.get_fresh(session_id)
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
            if results[-1].needs_approval:
                break
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
        self, message_id: uuid.UUID, *, status: str, response: str, tokens: int | None
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
