"""R5-A A1 RED hop 3 + A3 RED: a live full-mode session grant bypasses
sensitive-capability checks in decide().

Builds a credential_access request (file.read of a descendant `credentials`
file) in mode="full" with a well-formed full-mode session grant (the exact
identity grant_full_default mints: mode=full, capability=session,
digest=full-mode-session). Pre-fix decide() returns "allowed" via the fast
path at policy.py:858 BEFORE _sensitive_capabilities is consulted.
"""
import sys

sys.path.insert(0, "/Users/shadow/machina")

from machina.permissions.policy import ActionRequest, decide

SID = "123e4567-e89b-42d3-a456-426614174000"
req = ActionRequest(
    session_id=SID,
    mode="full",
    operation="file.read",
    targets=["/tmp/ws/credentials"],
    cwd="/tmp/ws",
    workspace_root="/tmp/ws",
)
grant = {
    "session_id": SID,
    "agent_id": "",
    "mode": "full",
    "capability": "session",
    "scope_path": None,
    "scope_type": "file",
    "grant_kind": "session",
    "digest": "full-mode-session",
}
d = decide(req, [grant])
print("verdict:", d.verdict, "| reason:", d.reason)
assert d.verdict == "allowed", f"expected fast-path allow, got {d.verdict}"
assert d.reason == "full-mode session grant"

# Control: same request WITHOUT the grant must pend (full requires activation).
d2 = decide(req, [])
print("no-grant verdict:", d2.verdict, "| reason:", d2.reason)
assert d2.verdict == "pending"

print("RED A1-hop3/A3 REPRODUCED: full grant allows credential read without approval")
