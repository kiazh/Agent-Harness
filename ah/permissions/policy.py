"""Permission policy: modes, normalization, decisions (Phase D).

Execution backend (host/container) is independent from permission policy
(workspace/ask/sandbox/full). User modes are presets, not security claims:
host execution is never a sandbox, and read-only text inspection never
authorizes opaque code execution.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from pathlib import Path

MODES = ("ask", "workspace", "sandbox", "full")

# Capabilities requiring explicit approval even in full mode (product policy).
CONFLICTING_CAPABILITIES = frozenset(
    {
        "credential_access",
        "privilege_escalation",
        "security_settings",
        "destructive_system",
    }
)

# Opaque execution: npm/pip/build/test scripts can run arbitrary code. Never
# classify as harmless by name alone.
OPAQUE_EXECUTABLES = frozenset(
    {"npm", "pip", "pip3", "python", "python3", "node", "npx", "uv", "poetry"}
)


@dataclass
class ActionRequest:
    request_id: str = ""
    session_id: str = ""
    turn_id: str = ""
    agent_id: str = ""
    parent_authority_id: str = ""
    mode: str = "ask"
    backend: str = "host"
    operation: str = ""  # file.read | file.write | process.exec | network.fetch | ...
    capabilities: list[str] = field(default_factory=list)
    argv: list[str] = field(default_factory=list)
    shell_payload: str = ""
    shell: str = ""
    cwd: str = ""
    targets: list[str] = field(default_factory=list)
    content_digest: str = ""
    network: list[str] = field(default_factory=list)
    opaque_network: bool = False
    timeout: int = 60
    digest: str = ""


@dataclass
class Decision:
    verdict: str  # allowed | pending | denied
    reason: str = ""
    request: ActionRequest | None = None


def _digest_of(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(str(p).encode("utf-8", errors="replace"))
        h.update(b"\x00")
    return h.hexdigest()


def workspace_root() -> Path:
    try:
        from ah.core.config import config

        root = config.get("workspace_root") or config.get("agent_harness_home") or Path.cwd()
        return Path(root).resolve()
    except Exception:
        return Path.cwd().resolve()


def _sensitive_capabilities(req: ActionRequest) -> list[str]:
    found = [c for c in req.capabilities if c in CONFLICTING_CAPABILITIES]
    # Heuristic tripwires (explicit approval, never silent):
    blob = " ".join([req.shell_payload, *req.argv, *req.targets]).lower()
    if any(k in blob for k in (".ssh", ".aws", "id_rsa", ".gnupg", "secrets", "credential")):
        found.append("credential_access")
    if any(
        k in blob
        for k in ("sudo", "runas", "set-mppreference", "iptables", "format ", "mkfs", "diskpart")
    ):
        found.append("privilege_escalation")
    return sorted(set(found))


def normalize_request(req: ActionRequest) -> ActionRequest:
    """Canonicalize paths/cwd/digest; separate validation from authorization."""
    if not req.request_id:
        req.request_id = uuid.uuid4().hex
    try:
        req.cwd = str(Path(req.cwd or ".").resolve())
    except Exception:
        pass
    canon_targets = []
    for t in req.targets:
        try:
            canon_targets.append(str(Path(t).resolve()))
        except Exception:
            canon_targets.append(str(t))
    req.targets = canon_targets
    req.digest = _digest_of(
        req.mode,
        req.backend,
        req.operation,
        "|".join(req.argv),
        req.shell_payload,
        req.cwd,
        "|".join(req.targets),
        req.content_digest,
        "|".join(req.network),
    )
    return req


def _inside_workspace(path: str) -> bool:
    try:
        return Path(path).resolve().is_relative_to(workspace_root())
    except Exception:
        return False


def decide(req: ActionRequest, grants: list[dict] | None = None) -> Decision:
    """Policy decision without side effects. Grants checked by digest/scope."""
    grants = grants or []
    sens = _sensitive_capabilities(req)
    if sens:
        # Sensitive exclusions remain explicit in EVERY mode, including full.
        for g in grants:
            if g.get("digest") == req.digest and not g.get("revoked"):
                if any(s in (g.get("capability") or "") for s in sens):
                    return Decision("allowed", f"scoped grant covers {','.join(sens)}", req)
        return Decision(
            "pending", f"sensitive capability requires explicit approval: {','.join(sens)}", req
        )
    # In-process, side-effect-free operations are always allowed: the broker
    # governs filesystem/process/network, not pure computation or reads.
    if req.operation.startswith("tool.") or req.operation in (
        "context.read",
        "skill.read",
        "memory",
        "delegate",
    ):
        return Decision("allowed", "in-process operation", req)
    if req.mode == "full":
        # Ordinary host ops covered by the disclosed full-mode grant proceed.
        # Approval is bound at activation (session grant), not per action.
        return Decision("allowed", "full-mode session grant", req)
    if req.mode == "sandbox" and req.backend != "sandbox":
        return Decision(
            "pending",
            "sandbox mode requires isolated backend; approve backend change or switch mode",
            req,
        )
    # ask (default) / workspace: trusted read-only project ops proceed.
    if req.operation == "file.read" and all(_inside_workspace(t) for t in req.targets):
        return Decision("allowed", "trusted project read", req)
    if (
        req.mode == "workspace"
        and req.operation in ("file.read", "file.write")
        and all(_inside_workspace(t) for t in req.targets)
    ):
        # Opaque scripts still request approval even for project writes below.
        return Decision("allowed", "workspace project write", req)
    # Scoped grant match (exact digest, unrevoked, unexpired).
    for g in grants:
        if g.get("revoked"):
            continue
        if g.get("digest") == req.digest:
            return Decision("allowed", "matching scoped grant", req)
        scope = g.get("scope_path")
        if scope and req.targets and all(str(t).startswith(str(scope)) for t in req.targets):
            if g.get("capability") in (req.operation, "session", "workspace"):
                return Decision("allowed", "scope grant covers target", req)
    if (
        req.operation in ("file.write", "process.exec")
        or req.targets
        and not all(_inside_workspace(t) for t in req.targets)
    ):
        return Decision("pending", "write/exec or out-of-scope access requires approval", req)
    return Decision("pending", "action requires approval", req)


def build_request(
    *,
    operation: str,
    targets: list[str] | None = None,
    argv: list[str] | None = None,
    shell_payload: str = "",
    cwd: str = "",
    content: str = "",
    mode: str | None = None,
    backend: str = "host",
    agent_id: str = "",
    session_id: str = "",
    turn_id: str = "",
    capabilities: list[str] | None = None,
    network: list[str] | None = None,
) -> ActionRequest:
    from ah.core.config import config

    req = ActionRequest(
        session_id=str(session_id or ""),
        turn_id=str(turn_id or ""),
        agent_id=str(agent_id or ""),
        mode=(mode or config.get("execution_mode") or "ask"),
        backend=backend,
        operation=operation,
        argv=list(argv or []),
        shell_payload=shell_payload,
        cwd=cwd or str(workspace_root()),
        targets=list(targets or []),
        content_digest=hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()
        if content
        else "",
        capabilities=list(capabilities or []),
        network=list(network or []),
    )
    if operation == "process.exec" and argv:
        base = Path(argv[0]).name.lower() if argv else ""
        if base in OPAQUE_EXECUTABLES:
            req.capabilities.append("opaque_execution")
    return normalize_request(req)
