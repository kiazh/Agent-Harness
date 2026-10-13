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
import json
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

# Per-turn grant cache (Stream A cost seam): list_grants is called on every
# broker guard, so N tool calls cost N identical SELECTs. Cache per
# (broker-turn, session) on the durable path only; the memory fallback stays
# uncached (cheap and outage-sensitive). Approval rows are NEVER cached here:
# only list_grants reads/writes this cache, and approval creation never
# touches it. Bounded; turn ids are unique per turn so entries naturally
# partition by turn.
#
# Stream B seam contract (formalized, behavior unchanged):
# - Copies in/out: hits return fresh ``dict`` copies and the cache stores
#   fresh copies, so callers mutating a result can never poison later reads.
# - Bypass without turn id: when no broker turn is active the cache is
#   skipped entirely (every read hits durable authority).
# - Invalidation: save_grant / revoke_grants invalidate the session slice,
#   revoke_all_grants and resolve_decision clear the whole cache (decisions
#   are human-paced, not hot-path), and session_mode.activate_durable_mode's
#   raw-SQL revocation (which bypasses store.revoke_grants) invalidates
#   explicitly at its own call site.
# - Fail-closed: durable read failures raise before anything is cached.
# Deletion test: removing this cache only costs N SELECTs per turn — verdicts
# are identical with it cold, which the pair tests lock in.
_grant_cache: dict[tuple[str, str], list[dict]] = {}
_GRANT_CACHE_CAP = 1024


def _current_turn() -> str:
    """Broker turn id, or "" outside a turn (then caching is bypassed).

    THE single broker-turn seam in this module (Stream B): only list_grants
    reads the turn, and only through here. Prefers the
    ``broker.get_current_turn()`` accessor when present, falling back to the
    ``broker._turn`` ContextVar otherwise. The import stays deferred (not
    module-level): broker imports this store at module level, so a top-level
    back-edge would be a static cycle AND a partial-initialization hazard
    (``_turn`` is defined after broker's imports). Never raises — failures
    yield "" so reads bypass the cache (fail-closed, never poisoned).
    """
    try:
        from machina.permissions import broker as _broker

        accessor = getattr(_broker, "get_current_turn", None)
        if callable(accessor):
            return str(accessor() or "")
        return str(_broker._turn.get() or "")
    except Exception:
        return ""


def invalidate_grant_cache(session_id: str | None = None) -> None:
    """Drop cached grants for one session, or the whole cache when None."""
    if session_id is None:
        _grant_cache.clear()
        return
    sid = str(session_id)
    for key in [k for k in _grant_cache if k[1] == sid]:
        _grant_cache.pop(key, None)


def clear_grant_cache() -> None:
    """Empty the grant cache (tests only)."""
    _grant_cache.clear()


async def _db() -> Any | None:
    try:
        from machina.db.connection import db

        return db if db.connected else None
    except Exception as error:
        # Degraded, not silent: callers fall back to the memory store and
        # report local-only durability via is_durable().
        from machina.observability.diagnostics import record_failure

        record_failure("permissions.store", error)
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
    """Expiry + revocation + binding check, applied immediately before execution.

    A present-but-unparseable ``expires_at`` fails closed (not live): only an
    absent expiry means "never expires". Stored timestamps are typed, so this
    guards hand-built or legacy rows, never the normal path.
    """
    if grant.get("revoked"):
        return False
    raw = grant.get("expires_at")
    if raw is None:
        return True
    exp = _parse_ts(raw)
    if exp is None:
        return False
    if exp <= (now or _utcnow()):
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


async def save_grant(grant: dict, *, connection: Any | None = None) -> dict:
    global _last_durable
    grant = dict(grant)
    grant.setdefault("id", str(uuid.uuid4()))
    grant.setdefault("scope_type", "file")
    grant.setdefault("grant_kind", "session")
    db = connection if connection is not None else await _db()
    if db is None:
        logger.warning("permission grant stored in-memory only (DB unavailable)")
        _last_durable = False
        _mem.grants[grant["id"]] = grant
        invalidate_grant_cache(grant.get("session_id"))
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
    invalidate_grant_cache(grant.get("session_id"))
    return grant


async def list_grants(session_id: str) -> list[dict]:
    """Live grants only: revoked/expired/foreign rows are never returned.

    Durable reads are cached per (broker-turn, session) so the N guards in
    one turn cost one SELECT. Writes (save/revoke/decision) invalidate.
    """
    db = await _db()
    if db is None:
        now = _utcnow()
        return [
            g
            for g in _mem.grants.values()
            if g.get("session_id") == str(session_id) and _grant_live(g, now=now)
        ]
    turn = _current_turn()
    cache_key = (turn, str(session_id)) if turn else None
    if cache_key is not None and cache_key in _grant_cache:
        return [dict(g) for g in _grant_cache[cache_key]]
    try:
        recs = await db.fetch(
            f"""SELECT {GRANT_COLUMNS} FROM permission_grants
                WHERE session_id = $1 AND revoked = FALSE
                  AND (expires_at IS NULL OR expires_at > now())""",  # nosec B608 # Fixed SQL fragments; external values use bound parameters.
            uuid.UUID(session_id),
        )
    except Exception:
        logger.exception("durable grant read failed")
        raise
    # Once durable authority is reachable, discard local grants from an
    # earlier outage so they cannot reappear during a later outage.
    _mem.grants.clear()
    grants = [_row_to_grant(r) for r in recs]
    if cache_key is not None:
        if len(_grant_cache) >= _GRANT_CACHE_CAP:
            _grant_cache.pop(next(iter(_grant_cache)), None)
        _grant_cache[cache_key] = [dict(g) for g in grants]
    return grants


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
                    "UPDATE permission_grants SET revoked = TRUE WHERE id = $1 AND session_id = $2",
                    uuid.UUID(grant_id),
                    uuid.UUID(session_id),
                )
            else:
                result = await db.execute(
                    "UPDATE permission_grants SET revoked = TRUE WHERE session_id = $1",
                    uuid.UUID(session_id),
                )
            from machina.db.connection import parse_command_count

            n += parse_command_count(result)
        except Exception:
            logger.exception("durable grant revocation failed")
            raise
        _last_durable = True
    elif n:
        _last_durable = False
    invalidate_grant_cache(session_id)
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
            from machina.db.connection import parse_command_count

            n += parse_command_count(result)
        except Exception:
            logger.exception("durable revoke-all failed")
            raise
        _last_durable = True
    elif n:
        _last_durable = False
    _grant_cache.clear()
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
        "version": 2,
        "workspace_root": getattr(req, "workspace_root", ""),
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
        if mem_ok:
            invalidate_grant_cache((rec or {}).get("session_id"))
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
    # Decision writes invalidate the grant cache (session unknown on this
    # path without an extra read; decisions are human-paced, not hot-path).
    _grant_cache.clear()
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
    # asyncpg returns UUIDs and JSONB text, while the in-memory record uses
    # strings and a proposal mapping. Keep the store interface identical for
    # both backends so origin checks and resumed actions use durable records.
    for key in ("id", "session_id"):
        if isinstance(out.get(key), uuid.UUID):
            out[key] = str(out[key])
    if isinstance(out.get("proposal"), str):
        try:
            out["proposal"] = json.loads(out["proposal"])
        except (ValueError, TypeError):
            out["proposal"] = None
    out["proposal_complete"] = bool(out.get("proposal"))
    return out


async def get_approval(request_id: str) -> dict | None:
    db = await _db()
    if db is not None:
        try:
            row = await db.fetchrow(
                "SELECT * FROM permission_approvals WHERE request_id = $1", request_id
            )
            if row:
                return _with_completeness(dict(row))
        except Exception as error:
            logger.error("durable approval read failed (%s)", type(error).__name__)
            raise
        return None
    async with _mem.lock:
        rec = _mem.approvals.get(request_id)
        mem_copy = dict(rec) if rec is not None else None
    return _with_completeness(mem_copy)


async def find_pending(session_id: str, digest: str) -> dict | None:
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
                return _with_completeness(dict(row))
        except Exception as error:
            logger.error("durable approval read failed (%s)", type(error).__name__)
            raise
        return None
    async with _mem.lock:
        for rec in _mem.approvals.values():
            if (
                rec.get("session_id") == str(session_id)
                and rec.get("digest") == digest
                and rec.get("status") == "pending"
                and not rec.get("consumed")
            ):
                return dict(rec)
    return None


async def find_approved_unclaimed(session_id: str, digest: str) -> dict | None:
    """Approved-but-never-executed action for headless resume (no new card).

    Legacy rows without a reviewable proposal never resume: a digest alone
    is not informed approval (AH-AUDIT-005). The caller creates a fresh
    approval card instead (fail closed).
    """
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
        except Exception as error:
            logger.error("durable approval read failed (%s)", type(error).__name__)
            raise
        return None
    async with _mem.lock:
        for rec in _mem.approvals.values():
            if (
                rec.get("session_id") == str(session_id)
                and rec.get("digest") == digest
                and rec.get("status") == "approved"
                and not rec.get("consumed")
                and rec.get("proposal")
            ):
                return _with_completeness(rec)
    return None


async def list_pending(session_id: str) -> list[dict]:
    db = await _db()
    if db is not None:
        try:
            rows = await db.fetch(
                """SELECT * FROM permission_approvals
                   WHERE session_id = $1 AND status = 'pending'
                   ORDER BY created_at DESC LIMIT 50""",
                uuid.UUID(session_id),
            )
            return [_with_completeness(dict(row)) for row in rows]
        except Exception as error:
            logger.error("durable approval read failed (%s)", type(error).__name__)
            raise
    async with _mem.lock:
        return [
            dict(rec)
            for rec in _mem.approvals.values()
            if rec.get("session_id") == str(session_id) and rec.get("status") == "pending"
        ]
