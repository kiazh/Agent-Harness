#!/usr/bin/env python
"""Audit finding: session approval grants store the operation (file.read)
instead of the reviewed sensitive capability (credential_access), so the
approved sensitive read still asks afterwards.

Replicates the exact grant payload built by
machina/api/routes/approvals.py resolve_approval (grant=session branch).
"""
import asyncio
import sys
import tempfile
import uuid
from pathlib import Path

REPO = Path("/Users/shadow/machina")
sys.path.insert(0, str(REPO))

work = Path(tempfile.mkdtemp(prefix="audit-grant-"))
env_file = work / ".env"
env_file.write_text("PRIVATE_TOKEN=review-fixture-secret\n", encoding="utf-8")

from machina.core.config import config

config.workspace_root = str(work)

from machina.permissions.policy import build_request, decide
from machina.permissions import store

SESSION = str(uuid.uuid4())


def make_req():
    return build_request(
        operation="file.read",
        targets=[str(env_file)],
        cwd=".",
        mode="ask",
        backend="host",
        tool="read_file",
        session_id=SESSION,
    )


async def main() -> None:
    req = make_req()
    d0 = decide(req, [])
    print(f"initial decision: {d0.verdict} ({d0.reason})")

    approval = await store.create_approval(req, "tui")
    await store.resolve_decision(approval["request_id"], "approved", "http")

    # Exact route logic (approvals.py:50-63): capability = record operation.
    targets = [t for t in str(approval.get("target", "")).split("|") if t]
    await store.save_grant(
        {
            "session_id": approval.get("session_id", ""),
            "agent_id": approval.get("agent_id", ""),
            "mode": "ask",
            "capability": approval.get("operation", ""),
            "scope_path": targets[0] if targets else None,
            "scope_type": "file",
            "grant_kind": "session",
            "digest": approval.get("digest", ""),
        }
    )
    grants = await store.list_grants(SESSION)
    print("stored grant capability:", [g["capability"] for g in grants])

    d1 = decide(make_req(), grants)
    print(f"after 'session' grant, same read decision: {d1.verdict} ({d1.reason})")
    print("=> grant does NOT cover the reviewed capability"
          if d1.verdict == "pending" else "=> grant covered it")

    # Control: a grant with the reviewed sensitive capability works.
    await store.save_grant(
        {
            "session_id": SESSION,
            "agent_id": "",
            "mode": "ask",
            "capability": "credential_access",
            "scope_path": str(env_file),
            "scope_type": "file",
            "grant_kind": "session",
            "digest": make_req().digest,
        }
    )
    grants2 = await store.list_grants(SESSION)
    d2 = decide(make_req(), grants2)
    print(f"control grant with capability=credential_access: {d2.verdict} ({d2.reason})")

    ok = d1.verdict == "pending" and d2.verdict == "allowed"
    print("CLAIM CONFIRMED" if ok else "CLAIM NOT REPRODUCED")
    sys.exit(0 if ok else 1)


asyncio.run(main())
