"""R5-A A3 GREEN (disposition: fast path stands, disclosure to R5-D).

No grant-path behavior change: a live full-mode session grant still allows a
credential_access request without approval (headless transports depend on
full meaning no-approval; see R4-018). This script pins that the semantics
R5-D must disclose are exactly what the code enforces.
"""
import sys

sys.path.insert(0, "/Users/shadow/machina")

from machina.permissions.policy import ActionRequest, decide

SID = "123e4567-e89b-42d3-a456-426614174000"


def req_for(op, targets):
    return ActionRequest(session_id=SID, mode="full", operation=op,
                         targets=targets, cwd="/tmp/ws", workspace_root="/tmp/ws")


grant = {"session_id": SID, "agent_id": "", "mode": "full", "capability": "session",
         "scope_path": None, "scope_type": "file", "grant_kind": "session",
         "digest": "full-mode-session"}

cases = [
    ("credential read", req_for("file.read", ["/tmp/ws/credentials"])),
    ("privilege escalation", ActionRequest(session_id=SID, mode="full",
         operation="process.exec", argv=["sudo", "ls"], cwd="/tmp/ws",
         workspace_root="/tmp/ws")),
    ("destructive op", ActionRequest(session_id=SID, mode="full",
         operation="process.exec", argv=["rm", "-rf", "/tmp/ws/x"], cwd="/tmp/ws",
         workspace_root="/tmp/ws")),
]
for name, req in cases:
    d = decide(req, [grant])
    print(f"{name}: {d.verdict} ({d.reason})")
    assert d.verdict == "allowed" and d.reason == "full-mode session grant", (name, d)

# Without the grant, full still pends activation — unchanged.
d = decide(req_for("file.read", ["/tmp/ws/credentials"]), [])
assert d.verdict == "pending", d
print("GREEN A3: fast-path semantics pinned; exact disclosure text -> R5-D (see evidence doc)")
