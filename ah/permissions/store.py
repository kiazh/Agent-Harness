"""Durable grants + approvals store (Phase D; LP-01/02/03/05/06 corrective).

State model: proposed → pending → approved / denied / expired / cancelled;
approved → claimed → completed / failed / cancelled (execution consumption).

- Human DECISION (resolve_decision) and EXECUTION claiming (claim_execution)
  are distinct atomic transitions. Exactly one claimer wins per approval.
- DB transitions run in ONE transaction on ONE acquired connection with
  conditional UPDATEs; durability failures RAISE explicitly — process-local
  state never silently stands in for cross-worker guarantees.
- In-memory backend (unit tests / DB-down) is guarded by a lock and reports
  non-durable via is_durable(); callers disclose degraded guarantees.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

TERMINAL = ("completed", "failed", "cancelled")
GRANTABLE_OUTCOMES = ("approved",)


class _MemoryStore:
    def __init__(self) -> None:
        self.grants: dict[str, dict] = {}
        self.approvals: dict[str, dict] = {}
        self.lock = asyncio.Lock()


_mem = _MemoryStore()
_last_durable: bool | None = None


async def _db() -> Any | None:
    try:
        from ah.db.connection import db

        return db if db.connected else None
    except Exception:
        return None


def is_durable() -> bool | None:
    """Last write durability: True (DB), False (memory fallback), None (no writes yet)."""
    return _last_durable


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _parse_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    try:
        text = str(value)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    except Exception:
        return None


def _grant_live(grant: dict, *, now: datetime | None = None) -> bool:
    """Expiry + revocation + binding check, applied immediately before execution."""
    if grant.get("revoked"):
        return False
    exp = _parse_ts(grant.get("expires_at"))
    if exp is not None and exp <= (now or _utcnow()):
        return False
    return True


def _agent_principal(principal: str) -> bool:
    return principal.startswith("agent:") or principal in ("agent", "delegate-tool")


# ─── grants ────────────────────────────────────────────────────────────────

GRANT_COLUMNS = (
    "id, session_id, agent_id, mode, capability, scope_path, scope_type, "
    "grant_kind, digest, expires_at, created_at, revoked"
)


def _row_to_grant(row: Any) -> dict:
    return {
        "id": str(row["id"]),
        "session_id": str(row["session_id"]) if row["session_id"] else "",
        "agent_id": row["agent_id"],
        "mode": row["mode"],
        "capability": row["capability"],
        "scope_path": row["scope_path"],
        "scope_type": row["scope_type"] if "scope_type" in row.keys() else "file",
        "grant_kind": row["grant_kind"] if "grant_kind" in row.keys() else "session",
        "digest": row["digest"],
        "expires_at": row["expires_at"],
        "revoked": row["revoked"],
    }


async def save_grant(grant: dict) -> dict:
    global _last_durable
    grant = dict(grant)
    grant.setdefault("id", str(uuid.uuid4()))
    grant.setdefault("scope_type", "file")
    grant.setdefault("grant_kind", "session")
    db = await _db()
    if db is None:
        logger.warning("permission grant stored in-memory only (DB unavailable)")
        _last_durable = False
        _mem.grants[grant["id"]] = grant
        return grant
    try:
        await db.execute(
            """INSERT INTO permission_grants
               (id, session_id, agent_id, mode, capability, scope_path, scope_type,
                grant_kind, digest, expires_at)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)""",
            uuid.UUID(grant["id"]) if len(grant["id"]) > 20 else uuid.uuid4(),
            uuid.UUID(grant["session_id"]) if grant.get("session_id") else None,
            grant.get("agent_id", ""),
            grant.get("mode", "ask"),
            grant.get("capability", ""),
            grant.get("scope_path"),
            grant.get("scope_type", "file"),
            grant.get("grant_kind", "session"),
            grant.get("digest", ""),
            grant.get("expires_at"),
        )
    except Exception:
        logger.exception("durable grant write failed")
        raise
    _last_durable = True
    return grant


async def list_grants(session_id: str) -> list[dict]:
    """Live grants only: revoked/expired/foreign rows are never returned."""
    now = _utcnow()
    rows: list[dict] = [
        g
        for g in _mem.grants.values()
        if g.get("session_id") == str(session_id) and _grant_live(g, now=now)
    ]
    db = await _db()
    if db is None:
        return rows
    try:
        recs = await db.fetch(
            f"""SELECT {GRANT_COLUMNS} FROM permission_grants
                WHERE session_id = $1 AND revoked = FALSE
                  AND (expires_at IS NULL OR expires_at > now())""",
            uuid.UUID(session_id),
        )
        for r in recs:
            rows.append(_row_to_grant(r))
    except Exception:
        logger.exception("durable grant read failed")
        raise
    return rows


async def revoke_grants(session_id: str, grant_id: str | None = None) -> int:
    global _last_durable
    n = 0
    for g in _mem.grants.values():
        if g.get("session_id") == str(session_id) and (grant_id is None or g["id"] == grant_id):
            g["revoked"] = True
            n += 1
    db = await _db()
    if db is not None:
        try:
            if grant_id:
                result = await db.execute(
                    "UPDATE permission_grants SET revoked = TRUE WHERE id = $1",
                    uuid.UUID(grant_id),
                )
            else:
                result = await db.execute(
                    "UPDATE permission_grants SET revoked = TRUE WHERE session_id = $1",
                    uuid.UUID(session_id),
                )
            from ah.db.connection import parse_command_count

            n += parse_command_count(result)
        except Exception:
            logger.exception("durable grant revocation failed")
            raise
        _last_durable = True
    elif n:
        _last_durable = False
    return n


async def revoke_all_grants() -> int:
    """Durable revocation across ALL sessions (explicit CLI reset scope)."""
    global _last_durable
    n = 0
    for g in _mem.grants.values():
        if not g.get("revoked"):
            g["revoked"] = True
            n += 1
    db = await _db()
    if db is not None:
        try:
            result = await db.execute(
                "UPDATE permission_grants SET revoked = TRUE WHERE revoked = FALSE"
            )
            from ah.db.connection import parse_command_count

            n += parse_command_count(result)
        except Exception:
            logger.exception("durable revoke-all failed")
            raise
        _last_durable = True
    elif n:
        _last_durable = False
    return n


# ─── approvals ─────────────────────────────────────────────────────────────


def proposal_of(req: Any) -> dict:
    """Versioned canonical action proposal (AH-AUDIT-005).

    Durable and reviewable: exact executable/arguments, cwd, backend,
    timeout, network destinations, content identity, capabilities, and tool
    identity. Secret-bearing values never enter here — content travels as a
    digest plus a separately redacted display preview.
    """
    return {
        "version": 1,
        "operation": getattr(req, "operation", ""),
        "tool": getattr(req, "tool", ""),
        "argv": list(getattr(req, "argv", None) or []),
        "shell": getattr(req, "shell", ""),
        "cwd": getattr(req, "cwd", ""),
        "backend": getattr(req, "backend", ""),
        "mode": getattr(req, "mode", ""),
        "targets": list(getattr(req, "targets", None) or []),
        "capabilities": sorted(getattr(req, "capabilities", None) or []),
        "network": list(getattr(req, "network", None) or []),
        "opaque_network": bool(getattr(req, "opaque_network", False)),
        "timeout": getattr(req, "timeout", 60),
        "content_digest": getattr(req, "content_digest", ""),
        "content_length": getattr(req, "content_length", 0),
        "digest": getattr(req, "digest", ""),
    }


def display_of(req: Any) -> str:
    """Sanitized human-review representation (redacted preview, no secrets)."""
    import json as _json

    preview = str(getattr(req, "content_preview", "") or "")
    proposal = proposal_of(req)
    # Keep the display compact: full proposal fields plus truncated preview.
    return _json.dumps({**proposal, "content_preview": preview[:4000]}, ensure_ascii=False)[:8000]


async def create_approval(req: Any, principal: str) -> dict:
    global _last_durable
    proposal = proposal_of(req)
    display = display_of(req)
    rec = {
        "id": str(uuid.uuid4()),
        "request_id": req.request_id,
        "session_id": str(req.session_id),
        "turn_id": str(req.turn_id),
        "agent_id": str(req.agent_id),
        "principal": principal,
        "operation": req.operation,
        "target": "|".join(req.targets)[:500],
        "digest": req.digest,
        "status": "pending",
        "consumed": False,
        "created_at": time.time(),
        # In-memory reviewable proposal (full preview while live).
        "proposal": proposal,
        "display": display,
        "content_preview": getattr(req, "content_preview", ""),
    }
    async with _mem.lock:
        _mem.approvals[req.request_id] = rec
    db = await _db()
    if db is not None:
        try:
            import json as _json

            async with db.acquire() as conn:
                await conn.execute(
                    """INSERT INTO permission_approvals
                       (request_id, session_id, turn_id, agent_id, principal, operation, target, digest, status, proposal, display)
                       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'pending', $9::jsonb, $10)""",
                    req.request_id,
                    uuid.UUID(req.session_id) if req.session_id else None,
                    req.turn_id or None,
                    req.agent_id,
                    principal,
                    req.operation,
                    "|".join(req.targets)[:500],
                    req.digest,
                    _json.dumps(proposal),
                    display,
                )
        except Exception:
            logger.exception("durable approval create failed")
            raise
        _last_durable = True
    else:
        _last_durable = False
    return rec


async def resolve_decision(request_id: str, verdict: str, principal: str) -> dict | None:
    """Record the HUMAN decision: pending → approved/denied/expired/cancelled.

    One transaction on one connection; conditional on pending+unconsumed.
    Returns the row, or None when already resolved/consumed/unknown.
    Agent principals are rejected (never approve themselves). DB failures
    raise explicitly.
    """
    global _last_durable
    if verdict not in ("approved", "denied", "expired", "cancelled"):
        raise ValueError(f"invalid verdict {verdict!r}")
    if _agent_principal(principal):
        return None
    async with _mem.lock:
        rec = _mem.approvals.get(request_id)
        mem_ok = False
        if rec is not None:
            if rec["status"] == "pending" and not rec.get("consumed"):
                if principal == rec.get("agent_id"):
                    return None
                rec["status"] = verdict
                rec["resolved_at"] = time.time()
                mem_ok = True
    db = await _db()
    if db is None:
        _last_durable = False if rec is not None else _last_durable
        return rec if mem_ok else None
    try:
        async with db.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT status, consumed, agent_id FROM permission_approvals "
                    "WHERE request_id = $1 FOR UPDATE",
                    request_id,
                )
                if row is None or row["status"] != "pending" or row["consumed"]:
                    return None
                if principal == row["agent_id"]:
                    return None
                updated = await conn.fetchrow(
                    """UPDATE permission_approvals SET status = $2, resolved_at = now()
                       WHERE request_id = $1 AND status = 'pending'
                       RETURNING request_id, status""",
                    request_id,
                    verdict,
                )
                if updated is None:
                    return None
    except Exception:
        logger.exception("durable decision resolve failed")
        raise
    _last_durable = True
    async with _mem.lock:
        if rec is not None:
            rec["status"] = verdict
            rec["resolved_at"] = time.time()
    return {"request_id": request_id, "status": verdict}


async def claim_execution(request_id: str, *, digest: str, session_id: str) -> bool:
    """Atomically claim an approved action for exactly one execution.

    approved → claimed, conditional on digest + session match. Exactly one
    claimer wins across workers; duplicates get False (no double effect).
    """
    global _last_durable
    async with _mem.lock:
        rec = _mem.approvals.get(request_id)
        mem_ok = (
            rec is not None
            and rec.get("status") == "approved"
            and not rec.get("consumed")
            and rec.get("digest") == digest
            and rec.get("session_id") == str(session_id)
        )
        if mem_ok:
            rec["status"] = "claimed"
            rec["consumed"] = True
    db = await _db()
    if db is None:
        if rec is not None:
            _last_durable = False
        return bool(mem_ok)
    try:
        async with db.acquire() as conn:
            updated = await conn.fetchrow(
                """UPDATE permission_approvals SET status = 'claimed', consumed = TRUE
                   WHERE request_id = $1 AND status = 'approved' AND digest = $2
                     AND session_id = $3
                   RETURNING request_id""",
                request_id,
                digest,
                uuid.UUID(session_id) if session_id else None,
            )
            ok = updated is not None
    except Exception:
        logger.exception("durable execution claim failed")
        raise
    _last_durable = True
    return ok


async def complete_execution(request_id: str, outcome: str) -> bool:
    """claimed → completed/failed/cancelled. Conditional; False if not claimed."""
    global _last_durable
    if outcome not in TERMINAL:
        raise ValueError(f"invalid outcome {outcome!r}")
    async with _mem.lock:
        rec = _mem.approvals.get(request_id)
        mem_ok = rec is not None and rec.get("status") == "claimed"
        if mem_ok:
            rec["status"] = outcome
    db = await _db()
    if db is None:
        return bool(mem_ok)
    try:
        async with db.acquire() as conn:
            updated = await conn.fetchrow(
                """UPDATE permission_approvals SET status = $2
                   WHERE request_id = $1 AND status = 'claimed'
                   RETURNING request_id""",
                request_id,
                outcome,
            )
            ok = updated is not None
    except Exception:
        logger.exception("durable execution completion failed")
        raise
    _last_durable = True
    return ok


def _with_completeness(rec: dict | None) -> dict | None:
    """Mark whether a record carries a reviewable proposal (AH-AUDIT-005).

    Legacy rows predate proposal/display columns: they prove identity (digest)
    only while the underlying proposal is unavailable, so transports must
    fail closed (request fresh approval) instead of executing blind.
    """
    if rec is None:
        return None
    out = dict(rec)
    out["proposal_complete"] = bool(out.get("proposal"))
    return out


async def get_approval(request_id: str) -> dict | None:
    async with _mem.lock:
        rec = _mem.approvals.get(request_id)
        mem_copy = dict(rec) if rec is not None else None
    if mem_copy is not None and mem_copy.get("proposal"):
        return _with_completeness(mem_copy)
    db = await _db()
    if db is not None:
        try:
            row = await db.fetchrow(
                "SELECT * FROM permission_approvals WHERE request_id = $1", request_id
            )
            if row:
                return _with_completeness(dict(row))
        except Exception:
            pass
    return _with_completeness(mem_copy)


async def find_pending(session_id: str, digest: str) -> dict | None:
    async with _mem.lock:
        for rec in _mem.approvals.values():
            if (
                rec.get("session_id") == str(session_id)
                and rec.get("digest") == digest
                and rec.get("status") == "pending"
                and not rec.get("consumed")
            ):
                return rec
    db = await _db()
    if db is not None:
        try:
            row = await db.fetchrow(
                """SELECT * FROM permission_approvals
                   WHERE session_id = $1 AND digest = $2 AND status = 'pending'
                   ORDER BY created_at DESC LIMIT 1""",
                uuid.UUID(session_id),
                digest,
            )
            if row:
                return dict(row)
        except Exception:
            pass
    return None


async def find_approved_unclaimed(session_id: str, digest: str) -> dict | None:
    """Approved-but-never-executed action for headless resume (no new card).

    Legacy rows without a reviewable proposal never resume: a digest alone
    is not informed approval (AH-AUDIT-005). The caller creates a fresh
    approval card instead (fail closed).
    """
    async with _mem.lock:
        for rec in _mem.approvals.values():
            if (
                rec.get("session_id") == str(session_id)
                and rec.get("digest") == digest
                and rec.get("status") == "approved"
                and not rec.get("consumed")
                and rec.get("proposal")
            ):
                return rec
    db = await _db()
    if db is not None:
        try:
            row = await db.fetchrow(
                """SELECT * FROM permission_approvals
                   WHERE session_id = $1 AND digest = $2 AND status = 'approved'
                     AND consumed = FALSE
                   ORDER BY created_at DESC LIMIT 1""",
                uuid.UUID(session_id),
                digest,
            )
            if row:
                candidate = dict(row)
                if candidate.get("proposal"):
                    return _with_completeness(candidate)
                return None
        except Exception:
            pass
    return None


async def list_pending(session_id: str) -> list[dict]:
    async with _mem.lock:
        out = [
            r
            for r in _mem.approvals.values()
            if r.get("session_id") == str(session_id) and r.get("status") == "pending"
        ]
    db = await _db()
    if db is not None:
        try:
            rows = await db.fetch(
                """SELECT * FROM permission_approvals
                   WHERE session_id = $1 AND status = 'pending'
                   ORDER BY created_at DESC LIMIT 50""",
                uuid.UUID(session_id),
            )
            seen = {r.get("request_id") for r in out}
            for row in rows:
                d = dict(row)
                if d.get("request_id") not in seen:
                    out.append(d)
        except Exception:
            pass
    return out
