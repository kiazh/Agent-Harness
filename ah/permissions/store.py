"""Durable grants + approvals store (Phase D).

DB-backed with in-memory fallback for tests/no-DB. Consumption is atomic:
duplicate replies cannot execute twice. Revocation affects pending/future,
never completed effects.
"""

from __future__ import annotations

import time
import uuid
from typing import Any


class _MemoryStore:
    def __init__(self) -> None:
        self.grants: dict[str, dict] = {}
        self.approvals: dict[str, dict] = {}


_mem = _MemoryStore()


async def _db() -> Any | None:
    try:
        from ah.db.connection import db

        return db if db.connected else None
    except Exception:
        return None


async def save_grant(grant: dict) -> dict:
    grant = dict(grant)
    grant.setdefault("id", str(uuid.uuid4()))
    db = await _db()
    if db is None:
        _mem.grants[grant["id"]] = grant
        return grant
    try:
        await db.execute(
            """INSERT INTO permission_grants
               (id, session_id, agent_id, mode, capability, scope_path, digest, expires_at)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8)""",
            uuid.UUID(grant["id"]) if len(grant["id"]) > 20 else uuid.uuid4(),
            uuid.UUID(grant["session_id"]) if grant.get("session_id") else None,
            grant.get("agent_id", ""),
            grant.get("mode", "ask"),
            grant.get("capability", ""),
            grant.get("scope_path"),
            grant.get("digest", ""),
            grant.get("expires_at"),
        )
    except Exception:
        _mem.grants[grant["id"]] = grant
        return grant
    return grant


async def list_grants(session_id: str) -> list[dict]:
    rows: list[dict] = [
        g
        for g in _mem.grants.values()
        if g.get("session_id") == str(session_id) and not g.get("revoked")
    ]
    db = await _db()
    if db is None:
        return rows
    try:
        recs = await db.fetch(
            """SELECT id, session_id, agent_id, mode, capability, scope_path, digest,
                      expires_at, created_at, revoked FROM permission_grants
               WHERE session_id = $1 AND revoked = FALSE""",
            uuid.UUID(session_id),
        )
        for r in recs:
            rows.append(
                {
                    "id": str(r["id"]),
                    "session_id": str(r["session_id"]) if r["session_id"] else "",
                    "agent_id": r["agent_id"],
                    "mode": r["mode"],
                    "capability": r["capability"],
                    "scope_path": r["scope_path"],
                    "digest": r["digest"],
                    "expires_at": r["expires_at"],
                    "revoked": r["revoked"],
                }
            )
    except Exception:
        pass
    return rows


async def revoke_grants(session_id: str, grant_id: str | None = None) -> int:
    n = 0
    for g in _mem.grants.values():
        if g.get("session_id") == str(session_id) and (grant_id is None or g["id"] == grant_id):
            g["revoked"] = True
            n += 1
    db = await _db()
    if db is not None:
        try:
            if grant_id:
                await db.execute(
                    "UPDATE permission_grants SET revoked = TRUE WHERE id = $1", uuid.UUID(grant_id)
                )
            else:
                await db.execute(
                    "UPDATE permission_grants SET revoked = TRUE WHERE session_id = $1",
                    uuid.UUID(session_id),
                )
        except Exception:
            pass
    return n


async def create_approval(req: Any, principal: str) -> dict:
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
    }
    _mem.approvals[req.request_id] = rec
    db = await _db()
    if db is not None:
        try:
            await db.execute(
                """INSERT INTO permission_approvals
                   (request_id, session_id, turn_id, agent_id, principal, operation, target, digest, status)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'pending')""",
                req.request_id,
                uuid.UUID(req.session_id) if req.session_id else None,
                req.turn_id or None,
                req.agent_id,
                principal,
                req.operation,
                "|".join(req.targets)[:500],
                req.digest,
            )
        except Exception:
            pass
    return rec


async def resolve_approval(request_id: str, verdict: str, principal: str) -> dict | None:
    rec = _mem.approvals.get(request_id)
    if rec is not None:
        if rec["status"] != "pending" or rec["consumed"]:
            return None  # atomic consumption: no double-execution
        # Agent tools can never approve themselves.
        if principal == rec.get("agent_id") or principal.startswith("agent:"):
            return None
        rec["status"] = verdict
        rec["consumed"] = verdict == "approved"
        rec["resolved_at"] = time.time()
    db = await _db()
    if db is not None:
        try:
            row = await db.fetchrow(
                "SELECT status, consumed, agent_id FROM permission_approvals WHERE request_id = $1 FOR UPDATE",
                request_id,
            )
            if row is None or row["status"] != "pending" or row["consumed"]:
                return rec
            if principal == row["agent_id"] or str(principal).startswith("agent:"):
                return None
            await db.execute(
                """UPDATE permission_approvals SET status = $2, consumed = $3,
                   resolved_at = now() WHERE request_id = $1""",
                request_id,
                verdict,
                verdict == "approved",
            )
            if rec is None:
                rec = {"request_id": request_id, "status": verdict}
            else:
                rec["status"] = verdict
        except Exception:
            pass
    return rec


async def get_approval(request_id: str) -> dict | None:
    if request_id in _mem.approvals:
        return _mem.approvals[request_id]
    db = await _db()
    if db is not None:
        try:
            row = await db.fetchrow(
                "SELECT * FROM permission_approvals WHERE request_id = $1", request_id
            )
            if row:
                return dict(row)
        except Exception:
            pass
    return None


async def find_pending(session_id: str, digest: str) -> dict | None:
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


async def list_pending(session_id: str) -> list[dict]:
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
