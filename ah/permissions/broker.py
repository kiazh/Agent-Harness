"""Permission broker: evaluate → approve/deny, atomic consumption (Phase D; LP-01/02/03).

All execution paths (file/RAG/skill/terminal/jobs/delegation/plugins) go
through :meth:`PermissionBroker.guard`. Headless transports (no approver)
never hang: they receive a structured needs_approval result and release
leases/slots into a paused state.

Decision/execution separation (LP-01): the human transport records the
DECISION (resolve_decision); the broker then atomically CLAIMS the approved
action for exactly one execution (claim_execution). Duplicates cannot
re-execute. Allow-once authorizes one bound action and saves no reusable
grant (LP-03); reusable session/directory authority needs an explicit user
choice made at resolve time by the transport.
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

# Immutable execution context (AH-AUDIT-002): the broker stamps the approved
# proposal's material fields at guard time. Backends (terminal/file) consume
# this snapshot instead of re-reading mutable global settings, so a
# concurrent mode/configuration change cannot alter approved execution
# semantics. Set for the guard+execute window; cleared by the caller (agent
# loop) afterwards. Never silently switch sandbox execution to host
# execution: backend changes require fresh approval.
_execution_context: ContextVar[dict | None] = ContextVar("execution_context", default=None)


def get_execution_context() -> dict | None:
    """Return the broker-stamped execution snapshot, if one is active."""
    try:
        return _execution_context.get()
    except Exception:
        return None


def clear_execution_context() -> None:
    """Clear the execution snapshot (end of the guard+execute window)."""
    try:
        _execution_context.set(None)
    except Exception:
        pass


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
        # LP-08: inherited parent caps enforced at the broker boundary (not
        # just stored on the agent): a restricted parent can never mint a
        # broader child through tools, RPC, jobs, or delegation.
        try:
            from ah.core.agent_factory import check_mode_cap, parent_authority_var

            parent = parent_authority_var.get()
            if parent is not None:
                if "tools" in parent and parent["tools"] is not None:
                    if req.tool and req.tool not in parent["tools"]:
                        raise ApprovalDenied(
                            f"tool {req.tool!r} outside parent authority"
                        ) from None
                check_mode_cap(parent, req.mode)
        except ApprovalDenied:
            raise
        except Exception:
            pass
        try:
            grants = await _store.list_grants(req.session_id) if req.session_id else []
        except Exception:
            logger.exception("grant lookup failed; denying by default")
            raise ApprovalDenied("grant store unavailable") from None
        decision: Decision = decide(req, grants)
        if decision.verdict == "allowed":
            # Stamp the immutable execution snapshot: backends execute
            # exactly what was authorized, even if globals change next.
            _execution_context.set(
                {
                    "session_id": req.session_id,
                    "mode": req.mode,
                    "backend": req.backend,
                    "operation": req.operation,
                    "digest": req.digest,
                    "approval_id": req.approval_id,
                }
            )
            return req
        if decision.verdict == "denied":
            raise ApprovalDenied(decision.reason)
        # pending → human approval path (never agent self-approval).
        handler = _approval_handler.get()
        principal = _principal.get()
        if principal == req.agent_id or str(principal).startswith("agent:"):
            raise ApprovalDenied("agents cannot approve their own actions")
        if handler is None:
            return await self._guard_headless(req)
        return await self._guard_interactive(req, handler, principal)

    async def _guard_headless(self, req: ActionRequest) -> ActionRequest:
        """No approver: resume an approved action or pause with a record."""
        existing = await _store.find_pending(req.session_id, req.digest)
        if existing is not None:
            raise NeedsApproval(existing)
        # LP-02 resume: an approved-but-unclaimed action is claimed for this
        # fresh execution (revalidated). No new card, no permanent grant.
        approved = await _store.find_approved_unclaimed(req.session_id, req.digest)
        if approved is not None:
            if await _store.claim_execution(
                approved["request_id"], digest=req.digest, session_id=req.session_id
            ):
                req.approval_id = approved["request_id"]
                _stamp_execution(req)
                return req
            current = await _store.get_approval(approved["request_id"])
            raise NeedsApproval(current or approved)
        approval = await _store.create_approval(req, principal="headless")
        raise NeedsApproval(approval)

    async def _guard_interactive(
        self, req: ActionRequest, handler: ApprovalHandler, principal: str
    ) -> ActionRequest:
        # Same request_id replayed (retries, duplicate submits, double cards):
        # route by the live record instead of creating a duplicate row.
        # Pending → wait on it; approved-but-unclaimed → claim and resume;
        # terminal → already executed: deny replay (a fresh action needs a new
        # request, which gets its own approval).
        current = await _store.get_approval(req.request_id)
        if current is not None:
            state = current.get("status")
            if state == "pending":
                raise NeedsApproval(current) from None
            if state == "approved":
                # AH-AUDIT-005: legacy approvals without a reviewable
                # proposal never resume — the user never saw the full
                # action. Fail closed with explicit re-approval.
                if not current.get("proposal_complete", bool(current.get("proposal"))):
                    raise ApprovalDenied(
                        "legacy approval lacks a reviewable proposal; re-approval required"
                    ) from None
                if await _store.claim_execution(
                    req.request_id, digest=req.digest, session_id=req.session_id
                ):
                    req.approval_id = req.request_id
                    _stamp_execution(req)
                    return req
                current = await _store.get_approval(req.request_id)
                raise NeedsApproval(current or {"request_id": req.request_id}) from None
            raise ApprovalDenied(f"approval already {state}") from None
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
            await _store.resolve_decision(req.request_id, "expired", principal)
            raise NeedsApproval({**approval, "status": "expired"}) from None
        # The transport recorded the decision; read it back (never re-resolve:
        # the record is already consumed and a second resolve must fail).
        rec = await _store.get_approval(req.request_id)
        status = (rec or {}).get("status")
        if verdict != "approved" or status != "approved":
            raise ApprovalDenied(f"approval {verdict}") from None
        # Material revalidation: digest at resolve time must match proposal.
        if rec.get("digest") != req.digest:
            raise ApprovalDenied("action changed after approval; re-approval required") from None
        # Execution claim: exactly one executor per approval (LP-01).
        if not await _store.claim_execution(
            req.request_id, digest=req.digest, session_id=req.session_id
        ):
            raise ApprovalDenied("approval already consumed by another execution") from None
        req.approval_id = req.request_id
        # No grant saved here: allow-once authorizes this execution only
        # (LP-03). Reusable authority is saved by the transport at resolve
        # time when the user explicitly chose a session/directory grant.
        _stamp_execution(req)
        return req

    async def complete(self, request_id: str, outcome: str) -> bool:
        """Mark a claimed execution completed/failed/cancelled (best effort)."""
        if not request_id:
            return False
        try:
            return await _store.complete_execution(request_id, outcome)
        except Exception:
            logger.warning("approval completion record failed", exc_info=True)
            return False

    async def revoke(self, session_id: str, grant_id: str | None = None) -> int:
        if session_id == "*":
            # CLI-level durable reset across all sessions (explicit scope).
            from ah.permissions import store as _s

            return await _s.revoke_all_grants()
        return await _store.revoke_grants(session_id, grant_id)


def _stamp_execution(req: ActionRequest) -> None:
    """Stamp the immutable execution snapshot for the guard+execute window."""
    _execution_context.set(
        {
            "session_id": req.session_id,
            "mode": req.mode,
            "backend": req.backend,
            "operation": req.operation,
            "digest": req.digest,
            "approval_id": req.approval_id,
        }
    )


def _file_write_diff(req: ActionRequest) -> dict:
    """Best-effort reviewable diff for file writes (AH-AUDIT-004).

    Returns redacted current/new previews plus a unified diff, each
    truncated with disclosure. Never raises: card rendering must not block
    authorization on IO failure.
    """
    out: dict = {"current_preview": "", "diff": "", "truncated": False}
    try:
        if req.operation != "file.write" or not req.targets:
            return out
        from ah.memory.redaction import redact_secrets as _redact

        current = ""
        try:
            from pathlib import Path as _Path

            p = _Path(req.targets[0])
            if p.is_file() and p.stat().st_size <= 200_000:
                current = p.read_text(encoding="utf-8", errors="replace")[:4000]
        except Exception:
            current = ""
        out["current_preview"] = _redact(current).text if current else "(new file)"
        proposed_preview = str(getattr(req, "content_preview", "") or "")
        if proposed_preview and current:
            import difflib as _difflib

            diff_lines = list(
                _difflib.unified_diff(
                    current.splitlines(),
                    proposed_preview.splitlines(),
                    fromfile="current",
                    tofile="proposed",
                )
            )[:120]
            out["diff"] = "\n".join(diff_lines)
            out["truncated"] = len(diff_lines) >= 120 or bool(
                getattr(req, "content_length", 0) and req.content_length > 4000
            )
        elif proposed_preview:
            out["truncated"] = bool(getattr(req, "content_length", 0) and req.content_length > 4000)
    except Exception:
        pass
    return out


def _approval_view(req: ActionRequest, approval: dict) -> dict:
    """Human-facing card payload (AH-AUDIT-003/004).

    Exact executable and arguments (unambiguous argv list, never a sole
    shell-looking string), cwd, backend, timeout, network, capabilities,
    plus file-write content identity and diff. Secret values are redacted
    for display; execution still binds the exact approved identity.
    """
    from ah.permissions import store as _store2

    try:
        durable = _store2.is_durable()
    except Exception:
        durable = None
    card: dict = {
        "request_id": req.request_id,
        "session_id": req.session_id,
        "turn_id": req.turn_id,
        "agent": req.agent_id,
        "mode": req.mode,
        "backend": req.backend,
        "operation": req.operation,
        "tool": req.tool,
        "argv": list(req.argv or []),
        "shell": req.shell,
        "cwd": req.cwd,
        "targets": list(req.targets or []),
        "capabilities": list(req.capabilities or []),
        "network": list(req.network or []),
        "timeout": req.timeout,
        "content_digest": req.content_digest,
        "content_preview": req.content_preview,
        "content_length": req.content_length,
        "digest": req.digest[:16],
        "durable": durable,
    }
    if req.operation == "file.write":
        card["file_diff"] = _file_write_diff(req)
    return card


permission_broker = PermissionBroker()
