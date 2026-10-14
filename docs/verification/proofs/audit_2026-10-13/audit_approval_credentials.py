#!/usr/bin/env python
"""Audit finding #4: approval proposals/display persist raw credentials.

Builds a request whose argv carries a bearer token, persists an approval,
then reads the stored proposal/display back through the same call the
authenticated REST route uses (store.list_pending).
"""
import asyncio
import json
import sys
import tempfile
import uuid
from pathlib import Path

REPO = Path("/Users/shadow/machina")
sys.path.insert(0, str(REPO))

work = Path(tempfile.mkdtemp(prefix="audit-creds-"))
SECRET = "review-fixture-bearer-8931"

from machina.core.config import config

config.workspace_root = str(work)

from machina.permissions.broker import _approval_view
from machina.permissions.policy import build_request
from machina.permissions import store

req = build_request(
    operation="process.exec",
    argv=["curl", "-X", "POST", "-H", f"Authorization: Bearer {SECRET}", "https://example.com/api"],
    cwd=".",
    mode="ask",
    backend="host",
    tool="terminal",
    session_id=str(uuid.uuid4()),
)

proposal = store.proposal_of(req)
display = store.display_of(req)
print("proposal_of(argv) contains raw secret:", SECRET in json.dumps(proposal))
print("display_of contains raw secret:      ", SECRET in display)


async def main() -> None:
    approval = await store.create_approval(req, "tui")
    rec = await store.get_approval(approval["request_id"])
    pending = await store.list_pending(req.session_id)
    stored_blobs = json.dumps({"rec": rec, "pending": pending})
    print("stored record contains raw secret:   ", SECRET in stored_blobs)

    # The live interactive card is redacted; contrast with the stored record.
    card = _approval_view(req, approval)
    print("live approval card contains secret:  ", SECRET in json.dumps(card))

    # Exercise the actual REST route body (auth dependency excluded) to show
    # the listing response carries the raw proposal/display.
    import machina.api.routes.approvals as approvals_route

    class _FakeSessionManager:
        async def get(self, _session_id):
            return object()  # session exists

    approvals_route.session_manager = _FakeSessionManager()
    response = await approvals_route.list_approvals(uuid.UUID(req.session_id))
    print("REST /api/v1/approvals contains secret:", SECRET in json.dumps(response))

    ok = (
        SECRET in json.dumps(proposal)
        and SECRET in display
        and SECRET in stored_blobs
        and SECRET in json.dumps(response)
    )
    print("CLAIM CONFIRMED" if ok else "CLAIM NOT REPRODUCED")
    sys.exit(0 if ok else 1)


asyncio.run(main())
