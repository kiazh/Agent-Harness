#!/usr/bin/env python
"""Audit finding: after another worker claims an approved action durably, a
worker whose pool is gone can still claim its stale in-memory mirror.

Uses the dedicated machina_test database only.
"""
import asyncio
import sys
import uuid
from pathlib import Path

REPO = Path("/Users/shadow/machina")
sys.path.insert(0, str(REPO))

DSN = "postgresql://postgres:postgres@localhost:5432/machina_test"

from machina.db.connection import db
from machina.permissions import store
from machina.permissions.policy import build_request


async def main() -> None:
    db.dsn = DSN
    await db.connect()
    await db.initialize_schema()

    session_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO sessions (id, title, agent_id) VALUES ($1, $2, $3)",
        uuid.UUID(session_id),
        "audit-stale",
        "harness",
    )
    req = build_request(
        operation="file.write",
        targets=["audit-stale.txt"],
        content="audit payload",
        cwd=".",
        mode="ask",
        backend="host",
        tool="write_file",
        session_id=session_id,
    )
    approval = await store.create_approval(req, "tui")
    request_id = approval["request_id"]
    await store.resolve_decision(request_id, "approved", "http")
    print("A: created + approved (durable + memory mirror approved)")

    # Worker B claims the same approval durably (exact claim_execution UPDATE).
    await db.execute(
        "UPDATE permission_approvals SET status = 'claimed', consumed = TRUE "
        "WHERE request_id = $1",
        request_id,
    )
    row = await db.fetchrow(
        "SELECT status, consumed FROM permission_approvals WHERE request_id = $1",
        request_id,
    )
    print(f"B: durable row now status={row['status']} consumed={row['consumed']}")

    rec = store._mem.approvals.get(request_id) or {}
    print(f"A: memory mirror still status={rec.get('status')} consumed={rec.get('consumed')}")

    # A loses its database pool, then retries the claim from memory only.
    await db.close()
    print("A: db.connected =", db.connected)
    replayed = await store.claim_execution(
        request_id, digest=req.digest, session_id=session_id
    )
    print("A: claim_execution from stale mirror ->", replayed)

    # Cleanup the ephemeral fixture row.
    await db.connect()
    await db.execute("DELETE FROM permission_approvals WHERE request_id = $1", request_id)
    await db.execute("DELETE FROM sessions WHERE id = $1", uuid.UUID(session_id))
    await db.close()

    confirmed = replayed is True
    print("CLAIM CONFIRMED (stale approved mirror authorized a durable-claimed action)"
          if confirmed else "CLAIM NOT REPRODUCED")
    sys.exit(0 if confirmed else 1)


asyncio.run(main())
