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
from pathlib import Path

from machina.observability.diagnostics import record_failure
from machina.observability.instrumentation import instrument, instrumented, log_event
from machina.permissions import store as _store
from machina.permissions.policy import ActionRequest, Decision, decide

logger = logging.getLogger(__name__)

# Display/audit truncation bound (F4): the digest always binds the FULL value;
# only rendered cards cut long network/query values, with an explicit counter.
DISPLAY_VALUE_MAX = 300

ApprovalHandler = Callable[[dict], Awaitable[str]]
# Transport-provided resolution sink (R4-065): called ONLY for terminal
# decisions the broker itself records (the deadline expiry), never for human
# decisions — those are recorded and announced by the transport that made
# them, so the broker must not duplicate them. Synchronous, best-effort:
# a transport failure never alters the authorization outcome.
ApprovalResolutionNotifier = Callable[[dict], None]
# Transport-provided human approver (TUI card / HTTP resolve). Never set to an
# agent tool — broker rejects agent principals at resolve time.
_approval_handler: ContextVar[ApprovalHandler | None] = ContextVar("approval_handler", default=None)
_approval_resolution: ContextVar[ApprovalResolutionNotifier | None] = ContextVar(
    "approval_resolution_notifier", default=None
)
_principal: ContextVar[str] = ContextVar("approval_principal", default="tui")
_turn: ContextVar[str] = ContextVar("approval_turn", default="")
resume_approval_var: ContextVar[str | None] = ContextVar("resume_approval", default=None)

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
    return _execution_context.get()


def clear_execution_context() -> None:
    """Clear the execution snapshot at the end of guard and execution."""
    _execution_context.set(None)


def get_current_turn() -> str:
    """Return the current broker turn id, or "" outside a turn."""
    try:
        return str(_turn.get() or "")
    except Exception:
        return ""


def set_approval_handler(
    handler: ApprovalHandler | None,
    *,
    principal: str = "tui",
    turn_id: str = "",
    on_resolved: ApprovalResolutionNotifier | None = None,
) -> None:
    _approval_handler.set(handler)
    _principal.set(principal)
    _turn.set(turn_id)
    _approval_resolution.set(on_resolved)


class NeedsApproval(Exception):
    """Structured pause (not an error): action awaits human approval."""

    def __init__(self, approval: dict) -> None:
        super().__init__(f"needs_approval:{approval.get('request_id')}")
        self.approval = approval


class ApprovalDenied(Exception):
    def __init__(self, reason: str = "denied") -> None:
        super().__init__(reason)
        self.reason = reason


def _log_approval_wait(req: ActionRequest, approval: object) -> None:
    """Record that an action is parked awaiting a human decision (no secrets)."""
    try:
        record = approval if isinstance(approval, dict) else {}
        log_event(
            "permissions.awaiting_approval",
            operation=req.operation,
            mode=req.mode,
            tool=req.tool or None,
            request_id=record.get("request_id") or req.request_id or None,
            session_id=req.session_id or None,
            agent_id=req.agent_id or None,
        )
    except Exception:
        pass


class PermissionBroker:
    @instrumented("permissions", "guard")
    async def guard(self, req: ActionRequest) -> ActionRequest:
        """Evaluate *req*; allow, await human approval, or raise."""
        req.turn_id = req.turn_id or _turn.get()
        log_event(
            "permissions.propose",
            operation=req.operation,
            mode=req.mode,
            backend=req.backend,
            tool=req.tool or None,
            target_count=len(req.targets),
            targets=[_redacted(target) for target in req.targets[:5]],
            request_id=req.request_id or None,
            session_id=req.session_id or None,
            agent_id=req.agent_id or None,
        )
        # LP-08: inherited parent caps enforced at the broker boundary (not
        # just stored on the agent): a restricted parent can never mint a
        # broader child through tools, RPC, jobs, or delegation.
        try:
            from machina.core.agent_factory import check_mode_cap, parent_authority_var

            parent = parent_authority_var.get()
            if parent is not None:
                if "tools" in parent and parent["tools"] is not None:
                    if req.tool and req.tool not in parent["tools"]:
                        audit_permission_verdict(
                            "denied", req, reason=f"tool {req.tool!r} outside parent authority"
                        )
                        raise ApprovalDenied(
                            f"tool {req.tool!r} outside parent authority"
                        ) from None
                check_mode_cap(parent, req.mode)
                if req.mode == "full":
                    for source in parent.get("full_grant_sources", []):
                        grants = await _store.list_grants(source["session_id"])
                        if not any(
                            str(grant.get("id")) == str(source["grant_id"])
                            and grant.get("agent_id") == source["agent_id"]
                            and grant.get("mode") == "full"
                            and grant.get("capability") == "session"
                            and grant.get("digest") == "full-mode-session"
                            for grant in grants
                        ):
                            audit_permission_verdict(
                                "denied", req, reason="inherited full authority revoked or expired"
                            )
                            raise ApprovalDenied("inherited full authority revoked or expired")
        except ApprovalDenied:
            raise
        except Exception as error:
            logger.error("parent authority evaluation failed (%s)", type(error).__name__)
            audit_permission_verdict("denied", req, reason="parent authority unavailable")
            raise ApprovalDenied("parent authority unavailable") from None
        resume_id = resume_approval_var.get()
        if resume_id:
            from machina.core.approval_resume import matches_approval

            record = await _store.get_approval(resume_id)
            if record is None or record.get("status") != "approved":
                audit_permission_verdict(
                    "denied", req, reason="approval already consumed or not approved"
                )
                raise ApprovalDenied("approval already consumed or not approved")
            if not matches_approval(req, record):
                fresh = await _store.create_approval(req, principal="http")
                raise NeedsApproval(fresh)
            if not await _store.claim_execution(
                resume_id, digest=req.digest, session_id=req.session_id, agent_id=req.agent_id
            ):
                raise ApprovalDenied("approval already consumed")
            req.approval_id = resume_id
            _stamp_execution(req)
            audit_permission_verdict("allowed", req, reason="approval resumed")
            return req
        try:
            grants = await _store.list_grants(req.session_id) if req.session_id else []
        except Exception:
            logger.exception("grant lookup failed; denying by default")
            raise ApprovalDenied("grant store unavailable") from None
        decision: Decision = decide(req, grants)
        log_event(
            "permissions.guard",
            operation=req.operation,
            mode=req.mode,
            tool=req.tool or None,
            verdict=decision.verdict,
            reason=decision.reason,
            session_id=req.session_id or None,
        )
        if decision.verdict == "allowed":
            # Stamp the immutable execution snapshot: backends execute
            # exactly what was authorized, even if globals change next.
            _stamp_execution(req)
            audit_permission_verdict("allowed", req, reason=decision.reason)
            return req
        if decision.verdict == "denied":
            audit_permission_verdict("denied", req, reason=decision.reason)
            raise ApprovalDenied(decision.reason)
        # pending → human approval path (never agent self-approval).
        handler = _approval_handler.get()
        principal = _principal.get()
        if principal == req.agent_id or str(principal).startswith("agent:"):
            audit_permission_verdict(
                "denied", req, reason="agents cannot approve their own actions"
            )
            raise ApprovalDenied("agents cannot approve their own actions")
        audit_permission_verdict("pending", req, reason=decision.reason)
        if handler is None:
            return await self._guard_headless(req)
        return await self._guard_interactive(req, handler, principal)

    async def _guard_headless(self, req: ActionRequest) -> ActionRequest:
        """No approver: resume an approved action or pause with a record."""
        existing = await _store.find_pending(req.session_id, req.digest, agent_id=req.agent_id)
        if existing is not None:
            _log_approval_wait(req, existing)
            raise NeedsApproval(existing)
        # LP-02 resume: an approved-but-unclaimed action is claimed for this
        # fresh execution (revalidated). No new card, no permanent grant.
        approved = await _store.find_approved_unclaimed(req.session_id, req.digest, agent_id=req.agent_id)
        if approved is not None:
            if await _store.claim_execution(
                approved["request_id"], digest=req.digest, session_id=req.session_id, agent_id=req.agent_id
            ):
                req.approval_id = approved["request_id"]
                _stamp_execution(req)
                audit_permission_verdict("allowed", req, reason="approved action resumed")
                return req
            current = await _store.get_approval(approved["request_id"])
            raise NeedsApproval(current or approved)
        approval = await _store.create_approval(req, principal="headless")
        _log_approval_wait(req, approval)
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
                _log_approval_wait(req, current)
                raise NeedsApproval(current) from None
            if state == "approved":
                # AH-AUDIT-005: legacy approvals without a reviewable
                # proposal never resume — the user never saw the full
                # action. Fail closed with explicit re-approval.
                if not current.get("proposal_complete", bool(current.get("proposal"))):
                    audit_permission_verdict(
                        "denied", req, reason="legacy approval lacks a reviewable proposal"
                    )
                    raise ApprovalDenied(
                        "legacy approval lacks a reviewable proposal; re-approval required"
                    ) from None
                if await _store.claim_execution(
                    req.request_id, digest=req.digest, session_id=req.session_id, agent_id=req.agent_id
                ):
                    req.approval_id = req.request_id
                    _stamp_execution(req)
                    audit_permission_verdict("allowed", req, reason="approval resumed")
                    return req
                current = await _store.get_approval(req.request_id)
                raise NeedsApproval(current or {"request_id": req.request_id}) from None
            audit_permission_verdict("denied", req, reason=f"approval already {state}")
            raise ApprovalDenied(f"approval already {state}") from None
        existing = await _store.find_pending(req.session_id, req.digest, agent_id=req.agent_id)
        if existing is not None:
            # Queue/combine: no concurrent card flood for the same action.
            _log_approval_wait(req, existing)
            raise NeedsApproval(existing)
        approval = await _store.create_approval(req, principal=principal)
        _log_approval_wait(req, approval)
        try:
            from machina.core.config import config

            timeout = float(config.get("approval_timeout") or 300)
        except Exception:
            timeout = 300.0
        try:
            verdict = await asyncio.wait_for(
                handler(_approval_view(req, approval)), timeout=timeout
            )
        except TimeoutError:
            return await self._approval_deadline(req, approval, principal)
        # The transport recorded the decision; read it back (never re-resolve:
        # the record is already consumed and a second resolve must fail).
        rec = await _store.get_approval(req.request_id)
        status = (rec or {}).get("status")
        if verdict != "approved" or status != "approved":
            audit_permission_verdict("denied", req, reason=f"approval {verdict}")
            raise ApprovalDenied(f"approval {verdict}") from None
        # Material revalidation: digest at resolve time must match proposal.
        if rec.get("digest") != req.digest:
            audit_permission_verdict("denied", req, reason="action changed after approval")
            raise ApprovalDenied("action changed after approval; re-approval required") from None
        # Execution claim: exactly one executor per approval (LP-01).
        if not await _store.claim_execution(
            req.request_id, digest=req.digest, session_id=req.session_id, agent_id=req.agent_id
        ):
            audit_permission_verdict("denied", req, reason="approval already consumed")
            raise ApprovalDenied("approval already consumed by another execution") from None
        req.approval_id = req.request_id
        # No grant saved here: allow-once authorizes this execution only
        # (LP-03). Reusable authority is saved by the transport at resolve
        # time when the user explicitly chose a session/directory grant.
        _stamp_execution(req)
        audit_permission_verdict("allowed", req, reason="approval granted")
        return req

    async def _approval_deadline(
        self, req: ActionRequest, approval: dict, principal: str
    ) -> ActionRequest:
        """Approval deadline passed: expire, or honour a decision that landed first.

        R4-066: the conditional expire can lose to a human decision recorded
        microseconds before the deadline. The DURABLE record is the truth, so
        an approved-but-unclaimed decision is claimed and executed exactly
        once here — never reported as "expired" against the record, and never
        left as a stale approved row a later retry could replay.
        R4-066b: the resolve return is this worker's commit-time view, not
        truth — a decision that becomes durable between the conditional
        UPDATE and the report (another worker; a commit that landed after the
        awaited cancel) must still win. The report is therefore derived from
        a fresh durable read, never from the resolve return.
        R4-065: a genuine expiry is surfaced on the transport's decision
        channel (when one registered a sink) so the UI retires the card
        without a decision; only an expiry this broker recorded itself is
        notified.
        """
        resolved = await _store.resolve_decision(req.request_id, "expired", principal)
        # F6: only the durable record may be reported as the verdict. Re-read
        # the authority after the conditional expire; a decision that landed
        # in the window above is honoured below (approved → claim/execute),
        # never reported as an expiry the record does not carry.
        current = await _store.get_approval(req.request_id) or {}
        state = str(current.get("status") or "")
        if state == "expired":
            if resolved is not None:
                # This broker recorded the expiry: surface it on the
                # transport's decision channel (R4-065).
                _notify_resolution({**approval, "status": "expired"})
            raise NeedsApproval({**approval, "status": "expired"}) from None
        if state == "approved" and not current.get("consumed"):
            if current.get("digest") == req.digest and await _store.claim_execution(
                req.request_id, digest=req.digest, session_id=req.session_id, agent_id=req.agent_id
            ):
                req.approval_id = req.request_id
                _stamp_execution(req)
                audit_permission_verdict("allowed", req, reason="approval granted at deadline")
                return req
            # Approved but not claimable by this execution (changed action or
            # an existing claim): report the live durable state, fail closed.
            raise NeedsApproval(current) from None
        if state == "pending":
            # The conditional expire could not commit and the row is still
            # pending: report the truth, never a false expiry.
            raise NeedsApproval(current) from None
        audit_permission_verdict("denied", req, reason=f"approval already {state or 'expired'}")
        raise ApprovalDenied(f"approval already {state or 'expired'}") from None

    async def complete(self, request_id: str, outcome: str) -> bool:
        """Mark a claimed execution completed/failed/cancelled (best effort)."""
        if not request_id:
            return False
        with instrument(
            "permissions", "resolve", approval_id=request_id, outcome=outcome
        ) as span:
            try:
                recorded = await _store.complete_execution(request_id, outcome)
                span.note(recorded=bool(recorded))
                return recorded
            except Exception:
                logger.warning("approval completion record failed", exc_info=True)
                return False

    async def revoke(self, session_id: str, grant_id: str | None = None) -> int:
        if session_id == "*":
            # CLI-level durable reset across all sessions (explicit scope).
            return await _store.revoke_all_grants()
        return await _store.revoke_grants(session_id, grant_id)


def _stamp_execution(req: ActionRequest) -> None:
    """Stamp the immutable execution snapshot for the guard+execute window."""
    _execution_context.set(
        {
            "session_id": req.session_id,
            "workspace_root": req.workspace_root,
            "mode": req.mode,
            "backend": req.backend,
            "operation": req.operation,
            "argv": list(req.argv),
            "shell_payload": req.shell_payload,
            "cwd": req.cwd,
            "digest": req.digest,
            "approval_id": req.approval_id,
        }
    )


def _notify_resolution(card: dict) -> None:
    """Surface a broker-recorded resolution on the transport's frame channel.

    The transport owns the channel (gateway write / SSE); the broker only
    reports the terminal state it recorded itself (deadline expiry). No sink
    is registered outside interactive transports, and a failing sink never
    alters the authorization outcome.
    """
    notifier = _approval_resolution.get()
    if notifier is None:
        return
    try:
        notifier(card)
    except Exception as _boundary_error:
        # Optional fallback preserves the primary outcome; report no payload.
        record_failure("broker.approval_resolution", _boundary_error)


def _redacted(value: object) -> str:
    """Best-effort secret redaction for DISPLAY and audit text.

    Deferred import keeps this module importable without the memory package;
    failures fail closed with an explicit marker (never the raw string) and
    are reported without payload.
    """
    text = str(value)
    try:
        from machina.memory.redaction import redact_secrets

        return redact_secrets(text).text
    except Exception as _boundary_error:
        # Optional fallback preserves the primary outcome; report no payload.
        record_failure("broker.redacted", _boundary_error)
        return "[redaction unavailable]"


def _truncate_display(value: str, limit: int = DISPLAY_VALUE_MAX) -> str:
    """Truncate attacker-influenced display text with an explicit hidden count."""
    if len(value) <= limit:
        return value
    return f"{value[:limit]}…(+{len(value) - limit} chars)"


# R4-067: audit rows must name the acted-on target without becoming a secret
# exfiltration channel. Bounded count, bounded length, redacted per element.
AUDIT_PREVIEW_ITEMS = 3
AUDIT_PREVIEW_MAX = 200


def _audit_previews(values: object) -> list[str]:
    """Bounded, redacted previews of attacker-influenced action material."""
    out = []
    for value in list(values or [])[:AUDIT_PREVIEW_ITEMS]:
        out.append(_truncate_display(_redacted(value), AUDIT_PREVIEW_MAX))
    return out


def audit_permission_verdict(verdict: str, req: ActionRequest, *, reason: str = "") -> None:
    """Emit a sanitized permission-verdict audit event (FM3-F8).

    Emitted by the broker boundary on every decision it makes; exposed so the
    agent loop can report verdicts observed outside :meth:`guard` without
    importing core internals. Never raises: auditing must not alter the
    authorization outcome.

    R4-067: routine allowed/denied rows carry bounded, redacted target/argv
    previews so an audit of an action without an approval card still names
    what was acted on; the execution digest (prefix) stays the exact binding.
    """
    try:
        from machina.core.provider import audit_log

        audit_log(
            f"permission_{verdict}",
            request_id=req.request_id,
            session_id=req.session_id,
            turn_id=req.turn_id,
            agent_id=req.agent_id,
            operation=req.operation,
            tool=req.tool,
            mode=req.mode,
            # Digest prefix only: the full digest is security material.
            digest_prefix=(req.digest or "")[:16],
            targets=_audit_previews(req.targets),
            argv=_audit_previews(req.argv),
            reason=_redacted(reason),
        )
    except Exception:
        logger.warning("permission verdict audit emission failed", exc_info=True)


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

        current = ""
        try:
            p = Path(req.targets[0])
            if p.is_file() and p.stat().st_size <= 200_000:
                current = p.read_text(encoding="utf-8", errors="replace")[:4000]
        except Exception:
            current = ""
        # RF3-F2: redact BEFORE diffing — the unified diff is display-only
        # (execution binds the digest), and diffing raw current leaked
        # secrets present in the existing file into the card/display.
        redacted_current = _redacted(current) if current else ""
        out["current_preview"] = redacted_current or "(new file)"
        proposed_preview = str(getattr(req, "content_preview", "") or "")
        if proposed_preview and current:
            import difflib as _difflib

            diff_lines = list(
                _difflib.unified_diff(
                    redacted_current.splitlines(),
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
    except Exception as _boundary_error:
        # Optional fallback preserves the primary outcome; report no payload.
        record_failure("broker._file_write_diff", _boundary_error)
    return out


def _approval_view(req: ActionRequest, approval: dict) -> dict:
    """Human-facing card payload (AH-AUDIT-003/004).

    Exact executable and arguments (unambiguous argv list, never a sole
    shell-looking string), cwd, backend, timeout, network, capabilities,
    plus file-write content identity and diff. Secret values are redacted
    for display; execution still binds the exact approved identity.
    """
    from machina.permissions import store as _store2

    try:
        durable = _store2.is_durable()
    except Exception:
        durable = None

    # RF3-F3: argv/targets/shell/network are attacker-influenced and can carry
    # credentials (e.g. `curl -H "Authorization: Bearer …"`). Redact the
    # DISPLAY copies only; execution/digest identity stays exact. F4: long
    # network/query values truncate here with a hidden-count marker — the
    # digest binds the full value.
    def _red(value: object) -> str:
        return _redacted(value)

    card: dict = {
        "request_id": req.request_id,
        "session_id": req.session_id,
        "turn_id": req.turn_id,
        "agent": req.agent_id,
        "mode": req.mode,
        "backend": req.backend,
        "operation": req.operation,
        "tool": req.tool,
        "argv": [_red(a) for a in (req.argv or [])],
        "shell": _red(req.shell),
        "cwd": req.cwd,
        "targets": [_red(t) for t in (req.targets or [])],
        "capabilities": list(req.capabilities or []),
        "network": [_truncate_display(_red(n)) for n in (req.network or [])],
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
