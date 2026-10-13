"""Permission broker package (Phase D).

Policy (modes/backends/roots/capabilities) is separate from execution
backends (host/container). All tool paths go through the broker; approval
binds the exact action digest and is consumed atomically.
"""

from machina.permissions.broker import (
    ApprovalDenied,
    NeedsApproval,
    PermissionBroker,
    permission_broker,
    set_approval_handler,
)
from machina.permissions.policy import (
    MODES,
    ActionRequest,
    Decision,
    build_request,
    normalize_request,
)

__all__ = [
    "ActionRequest",
    "ApprovalDenied",
    "Decision",
    "MODES",
    "NeedsApproval",
    "PermissionBroker",
    "build_request",
    "normalize_request",
    "permission_broker",
    "set_approval_handler",
]
