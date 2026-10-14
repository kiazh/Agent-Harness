#!/usr/bin/env python
"""Green equivalent of audit_grant_capability.py.

The original repro hardcodes the OLD route payload (capability = operation),
so it cannot flip once the route is fixed. This variant drives the REAL
machina.api.routes.approvals.resolve_approval route and the gateway mirror
(machina.gateway.features.mode.approvals_resolve) and asserts the session
grant now stores the reviewed capability set and covers the sensitive read.
"""
import asyncio
import sys
import tempfile
import uuid
from pathlib import Path

REPO = Path("/Users/shadow/machina")
sys.path.insert(0, str(REPO))

work = Path(tempfile.mkdtemp(prefix="audit-grant-green-"))
env_file = work / ".env"
env_file.write_text("PRIVATE_TOKEN=review-fixture-secret\n", encoding="utf-8")

from machina.core.config import config

config.workspace_root = str(work)

from machina.permissions.policy import build_request, decide
from machina.permissions import store

SESSION = str(uuid.uuid4())
SESSION2 = str(uuid.uuid4())


def make_req(session=SESSION):
    return build_request(
        operation="file.read",
        targets=[str(env_file)],
        cwd=".",
        mode="ask",
        backend="host",
        tool="read_file",
        session_id=session,
    )


async def main() -> None:
    import machina.api.routes.approvals as approvals_route
    from machina.api.routes.models import ResolveApprovalRequest

    # ── REST route ──────────────────────────────────────────────────────────
    req = make_req()
    approval = await store.create_approval(req, "tui")
    await approvals_route.resolve_approval(
        approval["request_id"],
        ResolveApprovalRequest(verdict="approved", sessionId=uuid.UUID(SESSION), grant="session"),
    )
    grants = await store.list_grants(SESSION)
    caps = [g["capability"] for g in grants]
    print("REST route stored grant capability:", caps)
    d1 = decide(make_req(), grants)
    print(f"after real 'session' grant, same read decision: {d1.verdict} ({d1.reason})")

    # ── gateway mirror ──────────────────────────────────────────────────────
    from machina.gateway.features.mode import approvals_resolve

    class _Gw:
        def __init__(self):
            self._pending_approvals = {}
            self.writes = []

        def require_db(self):
            return None

        def _write(self, payload):
            self.writes.append(payload)

        def event_payload(self, *args, **kwargs):
            return {"args": args, "kwargs": kwargs}

    gw = _Gw()
    req2 = make_req(SESSION2)
    approval2 = await store.create_approval(req2, "tui")
    await approvals_resolve(
        gw,
        {"requestId": approval2["request_id"], "verdict": "approved", "grant": "session",
         "sessionId": SESSION2},
    )
    grants2 = await store.list_grants(SESSION2)
    caps2 = [g["capability"] for g in grants2]
    print("gateway mirror stored grant capability:", caps2)
    d2 = decide(make_req(SESSION2), grants2)
    print(f"after gateway 'session' grant, same read decision: {d2.verdict} ({d2.reason})")

    safe = d1.verdict == "allowed" and d2.verdict == "allowed"
    print("SAFE BEHAVIOR CONFIRMED" if safe else "STILL BROKEN")
    sys.exit(0 if safe else 1)


asyncio.run(main())
