"""Permission broker: evaluate → approve/deny, atomic consumption (Phase D).

All execution paths (file/RAG/skill/terminal/jobs/delegation/plugins) go
through :meth:`PermissionBroker.guard`. Headless transports (no approver)
never hang: they receive a structured needs_approval result and release
leases/slots into a paused state.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextvars import ContextVar

from ah.permissions import store as _store
from ah.permissions.policy import ActionRequest, Decision, decide

logger = logging.getLogger(__name__)

ApprovalHandler = Callable[[dict], Awaitable[str]]
# Transport-provided human approver (TUI card / HTTP resolve). Never set to an
# agent tool — broker rejects agent principals at resolve time.
_approval_handler: ContextVar[ApprovalHandler | None] = ContextVar("approval_handler", default=None)
_principal: ContextVar[str] = ContextVar("approval_principal", default="tui")
_turn: ContextVar[str] = ContextVar("approval_turn", default="")


def set_approval_handler(
    handler: ApprovalHandler | None, *, principal: str = "tui", turn_id: str = ""
) -> None:
    _approval_handler.set(handler)
    _principal.set(principal)
    _turn.set(turn_id)


class NeedsApproval(Exception):
    """Structured pause (not an error): action awaits human approval."""

    def __init__(self, approval: dict) -> None:
        super().__init__(f"needs_approval:{approval.get('request_id')}")
        self.approval = approval


class ApprovalDenied(Exception):
    def __init__(self, reason: str = "denied") -> None:
        super().__init__(reason)
        self.reason = reason


class PermissionBroker:
    async def guard(self, req: ActionRequest) -> ActionRequest:
        """Evaluate *req*; allow, await human approval, or raise."""
        try:
            grants = await _store.list_grants(req.session_id) if req.session_id else []
        except Exception:
            grants = []
        decision: Decision = decide(req, grants)
        if decision.verdict == "allowed":
            return req
        if decision.verdict == "denied":
            raise ApprovalDenied(decision.reason)
        # pending → human approval path (never agent self-approval).
        handler = _approval_handler.get()
        principal = _principal.get()
        if handler is None:
            existing = await _store.find_pending(req.session_id, req.digest)
            if existing is not None:
                raise NeedsApproval(existing)
            approval = await _store.create_approval(req, principal="headless")
            raise NeedsApproval(approval)
        if principal == req.agent_id or str(principal).startswith("agent:"):
            raise ApprovalDenied("agents cannot approve their own actions")
        existing = await _store.find_pending(req.session_id, req.digest)
        if existing is not None:
            # Queue/combine: no concurrent card flood for the same action.
            raise NeedsApproval(existing)
        approval = await _store.create_approval(req, principal=principal)
        try:
            from ah.core.config import config

            timeout = float(config.get("approval_timeout") or 300)
        except Exception:
            timeout = 300.0
        try:
            verdict = await asyncio.wait_for(
                handler(_approval_view(req, approval)), timeout=timeout
            )
        except TimeoutError:
            await _store.resolve_approval(req.request_id, "expired", principal)
            raise NeedsApproval({**approval, "status": "expired"}) from None
        rec = await _store.resolve_approval(req.request_id, verdict, principal)
        if rec is None or verdict != "approved":
            raise ApprovalDenied(f"approval {verdict}")
        # Material revalidation: digest at resolve time must match proposal.
        if rec.get("digest") != req.digest:
            raise ApprovalDenied("action changed after approval; re-approval required")
        # Persist scoped grant for matching future actions (exact digest).
        try:
            await _store.save_grant(
                {
                    "session_id": req.session_id,
                    "agent_id": req.agent_id,
                    "mode": req.mode,
                    "capability": req.operation,
                    "scope_path": req.targets[0] if req.targets else None,
                    "digest": req.digest,
                }
            )
        except Exception:
            pass
        return req

    async def revoke(self, session_id: str, grant_id: str | None = None) -> int:
        if session_id == "*":
            # CLI-level full reset: clear in-memory grants; DB rows revoked
            # per session is out of scope without a session id.
            from ah.permissions.store import _mem as _memstore

            n = len(_memstore.grants)
            for g in _memstore.grants.values():
                g["revoked"] = True
            return n
        return await _store.revoke_grants(session_id, grant_id)


def _approval_view(req: ActionRequest, approval: dict) -> dict:
    """Human-facing card payload: no secret values, names only."""
    return {
        "request_id": req.request_id,
        "session_id": req.session_id,
        "turn_id": req.turn_id,
        "agent": req.agent_id,
        "mode": req.mode,
        "backend": req.backend,
        "operation": req.operation,
        "argv": req.argv,
        "shell": req.shell,
        "cwd": req.cwd,
        "targets": req.targets,
        "capabilities": req.capabilities,
        "network": req.network,
        "timeout": req.timeout,
        "digest": req.digest[:16],
    }


permission_broker = PermissionBroker()
