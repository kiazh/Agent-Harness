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
    # Concrete tool name (when invoked through the agent tool loop). Lets the
    # broker enforce inherited parent caps even when session grants exist.
    tool: str = ""
    # Redacted display-only preview of proposed content (AH-AUDIT-004): shown
    # on approval cards, never executed. Execution binds to content_digest
    # via the canonical digest; the preview is truncated with its full
    # length disclosed. Secrets are redacted at build time.
    content_preview: str = ""
    content_length: int = 0
    # Filled by the broker when an approval is claimed for this execution;
    # the agent loop marks it completed/failed/cancelled afterwards.
    approval_id: str = ""
    workspace_root: str = ""


@dataclass
class Decision:
    verdict: str  # allowed | pending | denied
    reason: str = ""
    request: ActionRequest | None = None


DIGEST_VERSION = "v2"


def _digest_of(*parts: str) -> str:
    """Versioned canonical digest (AH-AUDIT-008).

    Each part is length-prefixed (not separator-joined), so distinct arrays
    such as ['echo', 'a|b'] and ['echo', 'a', 'b'] never collide. Callers
    must pass every material authorization field; see normalize_request.
    """
    h = hashlib.sha256()
    h.update(b"ah-action-digest-v2\x00")
    for p in parts:
        encoded = str(p).encode("utf-8", errors="replace")
        h.update(str(len(encoded)).encode("ascii"))
        h.update(b":")
        h.update(encoded)
        h.update(b"\x00")
    return h.hexdigest()


def _canonical_list(values: list[str] | tuple[str, ...] | None) -> str:
    """Length-prefixed encoding preserving array boundaries and order."""
    h = hashlib.sha256()
    for v in values or []:
        encoded = str(v).encode("utf-8", errors="replace")
        h.update(str(len(encoded)).encode("ascii"))
        h.update(b":")
        h.update(encoded)
        h.update(b"\x00")
    return h.hexdigest()


def workspace_root() -> Path:
    from ah.core.execution_context import configured_workspace

    return configured_workspace()


def _sensitive_capabilities(req: ActionRequest) -> list[str]:
    """Exact capability identity plus heuristic tripwires (AH-AUDIT-006/007).

    Structured capabilities in CONFLICTING_CAPABILITIES match exactly.
    Heuristic substring tripwires are a backstop for opaque execution (which
    can never be fully classified by substrings): they force explicit
    approval but never silently authorize.
    """
    found = [c for c in (req.capabilities or []) if c in CONFLICTING_CAPABILITIES]
    # Opaque executables (npm/pip/python/...) can run arbitrary code: their
    # full effects are unknowable statically, so they always need explicit
    # review for mutating operations.
    if "opaque_execution" in (req.capabilities or []) and req.operation in (
        "process.exec",
        "file.write",
        "network.fetch",
    ):
        # Unclassifiable power bucket: the approval card shows the
        # opaque_execution capability itself so reviewers see the real
        # reason. Destructive commands below map to destructive_system.
        found.append("opaque_execution_review")
    blob = " ".join([req.shell_payload, *req.argv, *req.targets]).lower()
    if any(
        k in blob
        for k in (
            ".ssh",
            ".aws",
            "id_rsa",
            "id_ed25519",
            ".gnupg",
            "secrets",
            "credential",
            ".env",
            ".npmrc",
            ".pypirc",
            "passwd",
            "shadow",
        )
    ):
        found.append("credential_access")
    if any(
        k in blob
        for k in (
            "sudo",
            "runas",
            "set-mppreference",
            "iptables",
            "set-executionpolicy",
            "chmod 777",
            "chown ",
            "passwd ",
            "net user",
        )
    ):
        found.append("privilege_escalation")
    if any(
        k in blob
        for k in (
            "rm -rf",
            "rm --no-preserve",
            "mkfs",
            "diskpart",
            "format ",
            "shutdown",
            "reboot",
            "halt",
            ":(){:|:&};:",
            "dd if=",
            "del /f",
            "del /s",
            "rd /s",
            "remove-item",
        )
    ):
        found.append("destructive_system")
    # Bare destructive verbs as argv[0] (rm/del/dd/shutdown/...) — argv
    # boundaries are exact here, not substrings.
    if req.argv:
        first = req.argv[0].lower().rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        if first in ("rm", "rmdir", "dd", "mkfs", "fdisk", "shutdown", "reboot", "halt"):
            found.append("destructive_system")
    return sorted(set(found))


# Capabilities that force explicit approval even when named (never implied by
# a generic session/full grant). opaque_execution_review is the review bucket
# for unclassifiable opaque execution.
REVIEW_CAPABILITIES = frozenset(
    {
        "credential_access",
        "privilege_escalation",
        "security_settings",
        "destructive_system",
        "opaque_execution_review",
    }
)


def normalize_request(req: ActionRequest) -> ActionRequest:
    """Canonicalize paths/cwd/digest; separate validation from authorization.

    AH-AUDIT-011: relative paths resolve against the shared workspace root
    (the same root the file backend executes against), never the process
    CWD. The approved object and the accessed object therefore share one
    identity; runtime configuration changes revalidate at execution.
    """
    if not req.request_id:
        req.request_id = uuid.uuid4().hex
    root = Path(req.workspace_root).resolve() if req.workspace_root else workspace_root()
    req.workspace_root = str(root)
    try:
        cwd_path = Path(req.cwd or ".")
        if not cwd_path.is_absolute():
            cwd_path = root / cwd_path
        req.cwd = str(cwd_path.resolve())
    except (OSError, ValueError) as error:
        raise ValueError("invalid execution working directory") from error
    canon_targets = []
    for t in req.targets:
        try:
            target_path = Path(t)
            if not target_path.is_absolute():
                target_path = root / target_path
            canon_targets.append(str(target_path.resolve()))
        except (OSError, ValueError) as error:
            raise ValueError("invalid execution target") from error
    req.targets = canon_targets
    # AH-AUDIT-008: every material authorization field is bound with
    # array-boundary-preserving encoding (no separator joins). Tool
    # identity, sorted capabilities, and timeout are included: changing any
    # of them invalidates prior approval.
    req.digest = _digest_of(
        req.mode,
        req.backend,
        req.operation,
        req.tool,
        _canonical_list(sorted(req.capabilities or [])),
        _canonical_list(req.argv),
        req.shell_payload,
        req.cwd,
        _canonical_list(req.targets),
        req.content_digest,
        _canonical_list(req.network),
        str(req.timeout),
        str(req.opaque_network),
        req.workspace_root,
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
        # AH-AUDIT-007: sensitive exclusions remain explicit in EVERY mode,
        # including full. EVERY required sensitive capability must be covered
        # by exact capability identity on a live, digest-bound grant for this
        # exact action. One matching grant never authorizes the others, and
        # substring matching is never used. Ordinary operation/session grants
        # and full-mode grants are not sensitive consent.
        needed = set(sens)
        covered: set[str] = set()
        for g in grants:
            if not _grant_usable(g, req):
                continue
            if g.get("digest") != req.digest:
                continue
            cap = str(g.get("capability") or "")
            # Exact identity: the grant capability must equal the required
            # sensitive capability (or be an explicit multi-capability grant
            # recorded as a comma-separated exact set — each element matched
            # exactly, never by substring).
            granted_caps = {c.strip() for c in cap.split(",") if c.strip()}
            covered |= needed.intersection(granted_caps)
        if needed.issubset(covered):
            return Decision("allowed", f"scoped grant covers {','.join(sorted(needed))}", req)
        return Decision(
            "pending", f"sensitive capability requires explicit approval: {','.join(sens)}", req
        )
    # In-process, side-effect-free operations are always allowed: the broker
    # governs filesystem/process/network, not pure computation or reads.
    # Unknown tools declaring mutating effects are NOT in-process work.
    if "mutating-tool" not in (req.capabilities or []) and (
        req.operation.startswith("tool.")
        or req.operation
        in (
            "context.read",
            "skill.read",
            "memory",
            "delegate",
        )
    ):
        return Decision("allowed", "in-process operation", req)
    if req.mode == "full":
        # LP-07: full authority comes from a live full-mode grant bound to
        # THIS session — never from the global config value alone. A global
        # `full` default only auto-grants at session creation (disclosed
        # persistent scope); activating session A never elevates session B.
        for g in grants:
            if not _grant_usable(g, req):
                continue
            if (
                g.get("mode") == "full"
                and g.get("capability") == "session"
                and g.get("digest") == "full-mode-session"
            ):
                return Decision("allowed", "full-mode session grant", req)
        return Decision(
            "pending", "full mode requires explicit session activation (/mode full)", req
        )
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
    # Scoped grants (LP-03/04/05): live + bound + exact scope; mutating ops
    # additionally require the EXACT action digest, so changed content/cwd/
    # argv/backend always re-approve even under a directory grant.
    for g in grants:
        if not _grant_usable(g, req):
            continue
        if not _scope_allows(g, req):
            continue
        if req.operation in ("file.write", "process.exec", "network.fetch"):
            if g.get("digest") != req.digest:
                continue
            return Decision("allowed", "matching scoped grant", req)
        if g.get("capability") in (req.operation, "session", "workspace"):
            return Decision("allowed", "scope grant covers target", req)
    if (
        req.operation in ("file.write", "process.exec")
        or req.targets
        and not all(_inside_workspace(t) for t in req.targets)
    ):
        return Decision("pending", "write/exec or out-of-scope access requires approval", req)
    return Decision("pending", "action requires approval", req)


# Mutating operations always bind the exact action digest.
MUTATING_OPS = frozenset({"file.write", "process.exec", "network.fetch"})


def _grant_usable(grant: dict, req: ActionRequest) -> bool:
    """Expiry + revocation + session/agent binding, enforced at decision time."""
    if grant.get("revoked"):
        return False
    if grant.get("session_id") and grant.get("session_id") != str(req.session_id or ""):
        return False
    if grant.get("agent_id") and grant.get("agent_id") != str(req.agent_id or ""):
        return False
    exp = grant.get("expires_at")
    if exp is not None:
        try:
            from datetime import UTC, datetime

            now = datetime.now(UTC)
            if isinstance(exp, str):
                text = exp[:-1] + "+00:00" if exp.endswith("Z") else exp
                exp = datetime.fromisoformat(text)
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=UTC)
            if exp <= now:
                return False
        except Exception:
            return False
    return True


def _norm_path(path: str) -> str:
    """Normalized absolute identity (case-insensitive on Windows, UNC-aware).

    Matches the execution-time normalization (Path.resolve): POSIX-style
    "/proj/..." inputs on Windows resolve against the current drive, so
    drive-relative roots are anchored to the workspace drive here too.
    """
    import os as _os

    text = str(path or "")
    text = text.replace("/", _os.sep)
    if _os.path.isabs(text):
        normed = _os.path.normpath(text)
    else:
        normed = _os.path.normpath(_os.path.join(str(workspace_root()), text))
    if _os.name == "nt":
        drive, _rest = _os.path.splitdrive(normed)
        if not drive:
            # Drive-relative ("\proj\x"): anchor to the workspace drive so
            # stored scopes and resolved request targets compare identically.
            wdrive, _ = _os.path.splitdrive(str(workspace_root()))
            normed = wdrive + normed if wdrive else normed
        normed = normed.lower()
    return normed


def _scope_allows(grant: dict, req: ActionRequest) -> bool:
    """Exact file identity vs component-wise directory containment (LP-04)."""
    import os as _os

    scope = grant.get("scope_path")
    stype = grant.get("scope_type") or "file"
    if not scope:
        # Digest-only grant (e.g. once-style exact action): digest checked by caller.
        return True
    if not req.targets:
        return False
    if stype == "dir":
        root = _norm_path(scope)
        prefix = root if root.endswith(_os.sep) else root + _os.sep
        for t in req.targets:
            normed = _norm_path(t)
            if normed != root and not normed.startswith(prefix):
                return False
        return True
    # File scope: exact normalized identity. report.txt never covers
    # report.txt.backup; project never covers project-other.
    wanted = _norm_path(scope)
    return all(_norm_path(t) == wanted for t in req.targets)


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
    tool: str = "",
    timeout: int = 60,
) -> ActionRequest:
    from ah.core.config import config

    # AH-AUDIT-004: redacted display preview bound to the same identity as
    # the executable digest. Truncated with full length disclosed. Attached
    # ONLY where content is the proposal (file.write): for other operations
    # the card shows argv/targets, never raw secret-bearing blobs.
    preview = ""
    preview_len = 0
    if content:
        preview_len = len(content)
    if content and operation == "file.write":
        try:
            from ah.memory.redaction import redact_secrets as _redact

            preview = _redact(content[:4000]).text
            if preview_len > 4000:
                preview += f"\n… [truncated: showing 4000 of {preview_len} chars]"
        except Exception:
            preview = ""
            preview_len = len(content)
    try:
        timeout = int(timeout)
    except (TypeError, ValueError):
        timeout = 60
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
        timeout=timeout,
        targets=list(targets or []),
        content_digest=hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()
        if content
        else "",
        content_preview=preview,
        content_length=preview_len,
        capabilities=list(capabilities or []),
        network=list(network or []),
        tool=str(tool or ""),
    )
    if operation == "process.exec" and argv:
        base = Path(argv[0]).name.lower() if argv else ""
        if base in OPAQUE_EXECUTABLES:
            req.capabilities.append("opaque_execution")
    return normalize_request(req)
