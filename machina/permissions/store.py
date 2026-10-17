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
# - Revocation bound (F12): a cache hit is re-validated for the cheap local
#   `revoked` boolean and dropped if any entry is revoked, but a revocation
#   performed by ANOTHER worker is only observed on the next broker turn —
#   the cache is deliberately turn-scoped, so the worst-case staleness is one
#   turn, never a durable grant lifetime. The pair tests lock this bound in.
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
        cached = [dict(g) for g in _grant_cache[cache_key]]
        # F12 cheap boolean: entries locally marked revoked are never served,
        # even if a future write path forgot to invalidate the slice.
        live = [g for g in cached if not g.get("revoked")]
        if len(live) != len(cached):
            _grant_cache.pop(cache_key, None)
        return live
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


def _redact_public_text(value: object) -> str:
    """Redact secret-looking material from a persisted/public value.

    Fail-closed: stored proposals, displays, and listings are review artifacts,
    so when the redactor is unavailable the value is masked rather than
    exposed raw. Execution identity is separately bound by the canonical
    digest over the raw values, never by this review copy.
    """
    text = str(value)
    if not text:
        return text
    try:
        from machina.memory.redaction import redact_secrets

        return redact_secrets(text).text
    except Exception:
        logger.warning("approval redaction unavailable; masking public value")
        return "[REDACTED]"


def _reviewed_capabilities(req: Any) -> list[str]:
    """Exact capability identities the reviewed action required.

    Declared capabilities plus the policy-derived sensitive identities (for
    example ``credential_access`` for a sensitive path or ``destructive_system``
    for ``find -delete``). Session grants persist this exact set so the grant
    covers what the review actually authorized; policy matches each element
    exactly, never by substring.
    """
    reviewed = {
        str(c).strip() for c in (getattr(req, "capabilities", None) or []) if str(c).strip()
    }
    try:
        from machina.permissions.policy import _sensitive_capabilities

        reviewed.update(str(c) for c in _sensitive_capabilities(req))
    except Exception as error:
        from machina.observability.diagnostics import record_failure

        record_failure("permissions.store.reviewed_capabilities", error)
    return sorted(reviewed)


def proposal_of(req: Any) -> dict:
    """Versioned canonical action proposal (AH-AUDIT-005).

    Durable and reviewable: exact executable/arguments, cwd, backend,
    timeout, network destinations, content identity, capabilities, and tool
    identity. Secret-bearing display values (argv/shell/targets/network) are
    redacted here — this record is persisted and listed. Exact execution
    identity stays bound by the versioned ``digest`` computed over the raw
    values, which is never redacted or exposed as raw material.
    """
    return {
        "version": 2,
        "workspace_root": getattr(req, "workspace_root", ""),
        "operation": getattr(req, "operation", ""),
        "tool": getattr(req, "tool", ""),
        "argv": [_redact_public_text(a) for a in (getattr(req, "argv", None) or [])],
        "shell": _redact_public_text(getattr(req, "shell", "")),
        "cwd": getattr(req, "cwd", ""),
        "backend": getattr(req, "backend", ""),
        "mode": getattr(req, "mode", ""),
        "targets": [_redact_public_text(t) for t in (getattr(req, "targets", None) or [])],
        "capabilities": _reviewed_capabilities(req),
        "network": [_redact_public_text(n) for n in (getattr(req, "network", None) or [])],
        "opaque_network": bool(getattr(req, "opaque_network", False)),
        "timeout": getattr(req, "timeout", 60),
        "content_digest": getattr(req, "content_digest", ""),
        "content_length": getattr(req, "content_length", 0),
        "digest": getattr(req, "digest", ""),
    }


def display_of(req: Any) -> str:
    """Sanitized human-review representation (redacted preview, no secrets)."""
    import json as _json

    preview = _redact_public_text(str(getattr(req, "content_preview", "") or ""))[:4000]
    proposal = proposal_of(req)
    # Keep the display compact: full proposal fields plus truncated preview.
    return _json.dumps({**proposal, "content_preview": preview}, ensure_ascii=False)[:8000]


def public_view(rec: dict | None) -> dict | None:
    """Redacted public view of a stored approval record (listings/transports).

    Redaction is applied on read as well as on write so legacy rows persisted
    before this boundary never leak raw material through the REST or gateway
    listings.
    """
    if rec is None:
        return None
    out = dict(rec)
    proposal = out.get("proposal")
    if isinstance(proposal, str):
        out["proposal"] = _redact_public_text(proposal)
    elif isinstance(proposal, dict):
        redacted = dict(proposal)
        for key in ("argv", "targets", "network"):
            values = redacted.get(key)
            if isinstance(values, list):
                redacted[key] = [_redact_public_text(v) for v in values]
        for key in ("shell",):
            if isinstance(redacted.get(key), str):
                redacted[key] = _redact_public_text(redacted[key])
        out["proposal"] = redacted
    for key in ("display", "content_preview"):
        if isinstance(out.get(key), str):
            out[key] = _redact_public_text(out[key])
    # R4-105b: the top-level ``target`` column is persisted raw (durable row
    # stays intact for execution identity) so it must be redacted here at the
    # view boundary, like proposal/display. Covers legacy rows and listings.
    if isinstance(out.get("target"), str):
        out["target"] = _redact_public_text(out["target"])
    return out


def grant_capability(rec: dict) -> str:
    """Exact capability identity set persisted for a session grant (LP-03).

    The reviewed set is the approval proposal's capability identities
    (declared plus policy-derived sensitive capabilities such as
    ``credential_access``) together with the operation identity, so both the
    sensitive-capability coverage check and the operation-equality branch in
    policy see an exact match. Comma-joined: policy splits and matches each
    element exactly, never by substring. Legacy records without a proposal
    fall back to the recorded operation.
    """
    proposal = rec.get("proposal") or {}
    reviewed = {str(c).strip() for c in (proposal.get("capabilities") or []) if str(c).strip()}
    operation = str(proposal.get("operation") or rec.get("operation") or "").strip()
    if operation:
        reviewed.add(operation)
    return ",".join(sorted(reviewed))


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
        # Origin marker: durable rows are authoritatively decided/claimed only
        # through the DB; the memory mirror is never an authority for them.
        "durable": False,
        # In-memory reviewable proposal (full preview while live).
        "proposal": proposal,
        "display": display,
        "content_preview": _redact_public_text(getattr(req, "content_preview", "") or ""),
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
            # R4-068: the in-memory record was only a mirror of the intended
            # durable row. A failed INSERT must not leave a pending record
            # behind — nothing announces it, nothing can resolve it durably,
            # and the caller is about to fail closed. Drop exactly this record.
            async with _mem.lock:
                if _mem.approvals.get(req.request_id) is rec:
                    _mem.approvals.pop(req.request_id, None)
            raise
        async with _mem.lock:
            rec["durable"] = True
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

    F6: the in-memory record is advanced ONLY after the durable conditional
    UPDATE confirms (or when the memory store is the sole authority). On a
    lost race or a failed write, memory mirrors durable truth — or stays
    pending — so a later DB outage cannot resurrect a denied/claimed verdict
    through the memory fallback path.
    """
    global _last_durable
    if verdict not in ("approved", "denied", "expired", "cancelled"):
        raise ValueError(f"invalid verdict {verdict!r}")
    if _agent_principal(principal):
        return None
    db = await _db()
    if db is None:
        # Memory backend is the only authority: apply the same conditional
        # transition under the lock. Durable-origin records are refused: with
        # the DB disconnected their mirror may already be stale (another
        # worker may have decided/claimed the durable row), so a local verdict
        # must never authorize them (fail closed).
        rec = None
        async with _mem.lock:
            rec = _mem.approvals.get(request_id)
            if (
                rec is None
                or rec.get("durable")
                or rec.get("status") != "pending"
                or rec.get("consumed")
            ):
                return None
            if principal == rec.get("agent_id"):
                return None
            rec["status"] = verdict
            rec["resolved_at"] = time.time()
        _last_durable = False
        invalidate_grant_cache((rec or {}).get("session_id"))
        return rec
    resolved = False
    stale: tuple[str, bool] | None = None
    try:
        async with db.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT status, consumed, agent_id FROM permission_approvals "
                    "WHERE request_id = $1 FOR UPDATE",
                    request_id,
                )
                if row is None or row["status"] != "pending" or row["consumed"]:
                    # Durable truth already differs: mirror it into memory
                    # (fail closed) instead of the attempted verdict.
                    if row is not None:
                        stale = (str(row["status"]), bool(row["consumed"]))
                elif principal == row["agent_id"]:
                    return None
                else:
                    updated = await conn.fetchrow(
                        """UPDATE permission_approvals SET status = $2, resolved_at = now()
                           WHERE request_id = $1 AND status = 'pending'
                           RETURNING request_id, status""",
                        request_id,
                        verdict,
                    )
                    resolved = updated is not None
    except Exception:
        logger.exception("durable decision resolve failed")
        raise
    if stale is not None:
        async with _mem.lock:
            rec = _mem.approvals.get(request_id)
            if rec is not None:
                rec["status"] = stale[0]
                if stale[1]:
                    rec["consumed"] = True
        return None
    if not resolved:
        return None
    _last_durable = True
    # Decision writes invalidate the grant cache (session unknown on this
    # path without an extra read; decisions are human-paced, not hot-path).
    _grant_cache.clear()
    async with _mem.lock:
        rec = _mem.approvals.get(request_id)
        if rec is not None and not rec.get("consumed"):
            # Only now, after the durable conditional UPDATE confirmed.
            rec["status"] = verdict
            rec["resolved_at"] = time.time()
    return {"request_id": request_id, "status": verdict}


async def claim_execution(
    request_id: str, *, digest: str, session_id: str, agent_id: str | None = None
) -> bool:
    """Atomically claim an approved action for exactly one execution.

    approved → claimed, conditional on digest + session match. Exactly one
    claimer wins across workers; duplicates get False (no double effect).

    F6: the memory record is advanced only after the durable conditional
    UPDATE confirms. When the durable claim fails (lost race), memory is
    marked consumed (fail closed) so an outage cannot replay through the
    memory path; the memory path itself always re-verifies ``consumed``.
    """
    global _last_durable
    db = await _db()
    if db is None:
        rec = None
        mem_ok = False
        async with _mem.lock:
            rec = _mem.approvals.get(request_id)
            mem_ok = (
                rec is not None
                # Durable-origin approval: the DB is the only authority, and
                # with it disconnected this mirror may already be stale
                # (another worker may have claimed the durable row). Fail closed.
                and not rec.get("durable")
                and rec.get("status") == "approved"
                and not rec.get("consumed")
                and rec.get("digest") == digest
                and rec.get("session_id") == str(session_id)
                and agent_id is not None
                and rec.get("agent_id") == agent_id
            )
            if mem_ok:
                rec["status"] = "claimed"
                rec["consumed"] = True
        if rec is not None:
            _last_durable = False
        return bool(mem_ok)
    try:
        async with db.acquire() as conn:
            updated = await conn.fetchrow(
                """UPDATE permission_approvals SET status = 'claimed', consumed = TRUE
                   WHERE request_id = $1 AND status = 'approved' AND digest = $2
                     AND session_id = $3 AND agent_id = $4
                   RETURNING request_id""",
                request_id,
                digest,
                uuid.UUID(session_id) if session_id else None,
                agent_id,
            )
            ok = updated is not None
    except Exception:
        logger.exception("durable execution claim failed")
        raise
    _last_durable = True
    async with _mem.lock:
        rec = _mem.approvals.get(request_id)
        if rec is not None:
            if ok and not rec.get("consumed"):
                rec["status"] = "claimed"
                rec["consumed"] = True
            elif not ok:
                # Conditional failure = the durable side did not confirm this
                # claim (already claimed/changed). Fail closed in memory.
                rec["consumed"] = True
    return ok


async def complete_execution(request_id: str, outcome: str) -> bool:
    """claimed → completed/failed/cancelled. Conditional; False if not claimed.

    F6/R4-068: durable-first — the memory mirror advances only AFTER the
    durable conditional UPDATE confirms. With the DB disconnected nothing is
    completed locally (a durable-origin mirror must not record a terminal
    state the durable authority never saw). A durable conditional failure
    fails closed in memory (consumed) so nothing can replay the row through
    the memory path after an outage.
    """
    global _last_durable
    if outcome not in TERMINAL:
        raise ValueError(f"invalid outcome {outcome!r}")
    db = await _db()
    if db is None:
        async with _mem.lock:
            rec = _mem.approvals.get(request_id)
            mem_ok = (
                rec is not None
                # Durable-origin approval: the DB is the only authority, and
                # with it disconnected this mirror may already be stale.
                and not rec.get("durable")
                and rec.get("status") == "claimed"
            )
            if mem_ok:
                rec["status"] = outcome
        if mem_ok:
            _last_durable = False
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
    async with _mem.lock:
        rec = _mem.approvals.get(request_id)
        if rec is not None:
            if ok:
                # Only now, after the durable conditional UPDATE confirmed.
                rec["status"] = outcome
            else:
                # Conditional failure = the durable side did not confirm this
                # completion (not claimed / already terminal). Fail closed.
                rec["consumed"] = True
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
    """Durable approval record through the redacted public view.

    R4-105b behavior change: the returned mapping is the ``public_view`` of
    the stored row — the top-level ``target`` column (persisted raw) is
    redacted here at the view boundary. The durable row itself is untouched;
    execution identity (digest/raw argv) still binds the exact action via
    ``claim_execution``/``proposal_of``. Internal grant-scope derivation from
    ``target`` therefore sees the redacted value (fail-closed direction).
    """
    db = await _db()
    if db is not None:
        try:
            row = await db.fetchrow(
                "SELECT * FROM permission_approvals WHERE request_id = $1", request_id
            )
            if row:
                return public_view(_with_completeness(dict(row)))
        except Exception as error:
            logger.error("durable approval read failed (%s)", type(error).__name__)
            raise
        return None
    async with _mem.lock:
        rec = _mem.approvals.get(request_id)
        mem_copy = dict(rec) if rec is not None else None
    return public_view(_with_completeness(mem_copy))


async def find_pending(session_id: str, digest: str, *, agent_id: str | None = None) -> dict | None:
    """Newest pending action for this session+digest, through ``public_view``.

    F4/R4-105b: the raw ``target`` column is persisted unredacted, so the
    record handed to transports (``NeedsApproval``) is redacted at this view
    boundary exactly like ``get_approval``/``list_pending``. Selection is
    unchanged (session + digest + status + recency); only the rendered view
    is redacted.
    """
    db = await _db()
    if db is not None:
        try:
            row = await db.fetchrow(
                """SELECT * FROM permission_approvals
                   WHERE session_id = $1 AND digest = $2 AND agent_id = $3 AND status = 'pending'
                   ORDER BY created_at DESC LIMIT 1""",
                uuid.UUID(session_id),
                digest,
                agent_id,
            )
            if row:
                return public_view(_with_completeness(dict(row)))
        except Exception as error:
            logger.error("durable approval read failed (%s)", type(error).__name__)
            raise
        return None
    async with _mem.lock:
        for rec in _mem.approvals.values():
            if (
                rec.get("session_id") == str(session_id)
                and rec.get("digest") == digest
                and agent_id is not None
                and rec.get("agent_id") == agent_id
                and rec.get("status") == "pending"
                and not rec.get("consumed")
            ):
                return public_view(dict(rec))
    return None


async def find_approved_unclaimed(
    session_id: str, digest: str, *, agent_id: str | None = None
) -> dict | None:
    """Approved-but-never-executed action for headless resume (no new card).

    Legacy rows without a reviewable proposal never resume: a digest alone
    is not informed approval (AH-AUDIT-005). The caller creates a fresh
    approval card instead (fail closed).

    F4/R4-105b: like ``get_approval``/``list_pending``, the returned record is
    passed through ``public_view`` — the raw top-level ``target`` column must
    not reach transports. The proposal-presence selection check runs on the
    raw row before redaction, so resume semantics are unchanged.
    """
    db = await _db()
    if db is not None:
        try:
            row = await db.fetchrow(
                """SELECT * FROM permission_approvals
                   WHERE session_id = $1 AND digest = $2 AND agent_id = $3 AND status = 'approved'
                     AND consumed = FALSE
                   ORDER BY created_at DESC LIMIT 1""",
                uuid.UUID(session_id),
                digest,
                agent_id,
            )
            if row:
                candidate = dict(row)
                if candidate.get("proposal"):
                    return public_view(_with_completeness(candidate))
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
                and agent_id is not None
                and rec.get("agent_id") == agent_id
                and rec.get("status") == "approved"
                and not rec.get("consumed")
                # Durable-origin approvals are DB-authoritative only; without
                # the DB they must never resume from a possibly stale mirror.
                and not rec.get("durable")
                and rec.get("proposal")
            ):
                return public_view(_with_completeness(rec))
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
            return [public_view(_with_completeness(dict(row))) for row in rows]
        except Exception as error:
            logger.error("durable approval read failed (%s)", type(error).__name__)
            raise
    async with _mem.lock:
        return [
            public_view(dict(rec))
            for rec in _mem.approvals.values()
            if rec.get("session_id") == str(session_id) and rec.get("status") == "pending"
        ]
