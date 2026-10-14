"""Permission policy: modes, normalization, decisions (Phase D).

Execution backend (host/container) is independent from permission policy
(workspace/ask/sandbox/full). User modes are presets, not security claims:
host execution is never a sandbox, and read-only text inspection never
authorizes opaque code execution.
"""

from __future__ import annotations

import hashlib
import importlib
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from machina.observability.diagnostics import record_failure
from machina.permissions.commands import executable_name

MODES = ("ask", "workspace", "sandbox", "full")

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
    # Redacted display-only preview of content/extension arguments: shown
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
    h.update(f"machina-action-digest-{DIGEST_VERSION}".encode() + b"\x00")
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
    from machina.core.execution_context import configured_workspace

    return configured_workspace()


#: Credential-store modules that resolve their own on-disk identity through
#: ``auth_path()`` (env override -> configured machina_home -> ~/.machina).
#: Bound through importlib so this policy module never pulls the auth/httpx
#: import stack, and so the deferred-import census gains no ``import``
#: statements (same seam idiom as the leaf-independence conversions).
_CREDENTIAL_STORE_MODULES = (
    "machina.security.chatgpt_auth",
    "machina.security.copilot_auth",
)


def credential_store_paths() -> frozenset[Path]:
    """Resolved identity of every Machina credential store (R3-1).

    Each store resolves its own path the same way it does when it opens the
    file, so every configured override is honored identically by the policy.
    A module that cannot be imported or resolved is skipped; the remaining
    stores still gate their paths.
    """
    stores: set[Path] = set()
    for module_name in _CREDENTIAL_STORE_MODULES:
        try:
            raw = importlib.import_module(module_name).auth_path()
            stores.add(Path(raw).expanduser().resolve())
        except Exception:
            continue
    return frozenset(stores)


def _is_credential_store(value: str, root: Path) -> bool:
    """Exact resolved store identity (plus its atomic temp/backup siblings).

    The store file IS the secret: when a workspace contains the store, its
    reads must gate exactly like a ``credentials`` read instead of being a
    trusted project read. Sibling names created by the store's own atomic
    replace (``<name>.<rand>.tmp``), lock, generation, or backups gate too.
    Comparison is case-folded, matching the case-insensitive aliasing the
    policy already fails closed on for `.git`/`.machina`.
    """
    stores = credential_store_paths()
    if not stores:
        return False
    candidate = Path(str(value).replace("\\", "/")).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve()
    except (OSError, ValueError):
        return False
    text = str(resolved).lower()
    parent_text = str(resolved.parent).lower()
    for store in stores:
        if text == str(store).lower():
            return True
        if parent_text == str(store.parent).lower() and resolved.name.lower().startswith(
            store.name.lower() + "."
        ):
            return True
    return False


def sensitive_path(value: str, root: Path | None = None) -> bool:
    """Shared sensitive-path identity for credential material (R01).

    This is the ONE definition of "sensitive path" used both by the policy's
    credential_access capability and by tools that open file contents while
    searching (``search_files``): a descendant ``credentials`` /
    ``production.env`` must be gated exactly like a direct read, and the
    resolved credential-store files gate wherever they live (R3-1). Matches
    credential path components, never incidental words/usernames. R4-060:
    ``REV:path`` operands are split on the first colon (a drive-letter prefix
    is not a revspec) and backup/editor spellings of a name are matched
    through their decoration, so `git show HEAD:credentials` and `.env~`
    gate like `cat credentials`.
    """
    root = root if root is not None else workspace_root()
    if _is_credential_store(value, root):
        return True
    text = value.replace("\\", "/")
    path = Path(text).expanduser()
    # Ignore ancestors of the configured project (e.g. /home/user).
    if path.is_absolute():
        try:
            text = str(path.relative_to(root))
        except ValueError:
            text = str(path)
    parts = [p.lower() for p in text.replace("\\", "/").split("/") if p]
    # Git revspec operands name the file after the first colon
    # (`HEAD:credentials`, `HEAD:deploy/.env`); include that half so a
    # revspec can never hide a credential file. `C:` is a drive letter, not
    # a revspec.
    if ":" in text and not (len(text) > 1 and text[1] == ":"):
        parts.extend(p.lower() for p in text.split(":", 1)[1].split("/") if p)
    parts = [_backup_stripped(p) for p in parts]
    private_dirs = {".ssh", ".aws", ".gnupg", ".azure", ".kube"}
    private_files = {
        ".npmrc",
        ".pypirc",
        ".envrc",
        ".git-credentials",
        ".netrc",
        "id_rsa",
        "id_ed25519",
        "credentials",
    }
    if private_dirs.intersection(parts) or any(p in private_files for p in parts):
        return True
    # Dotenv family (FM3-F9): exact ".env", any ".env.<suffix>" (except the
    # documented ".env.example" template) plus any "*.env" file.
    if any(
        (p == ".env" or (p.startswith(".env.") and p != ".env.example") or p.endswith(".env"))
        for p in parts
    ):
        return True
    return any(
        parts[i : i + 2] in (["etc", "passwd"], ["etc", "shadow"]) for i in range(len(parts) - 1)
    )


# R4-060: backup/editor/rotation decorations that do not change which
# credential family a name belongs to (`.env~`, `.env-backup`,
# `credentials.bak`, `.netrc.old`, `credentials.1`, `id_rsa_backup`). The
# family matcher sees through them, so an editor or rsync copy of a secret
# gates exactly like the original.
_BACKUP_DECORATION_RE = re.compile(
    r"(?:~+\d*|[._-](?:bak|backup|old|orig|save|swp|swo|tmp|temp)\d*|[._-]\d+)+$",
    re.IGNORECASE,
)


def _backup_stripped(part: str) -> str:
    """Strip trailing backup decorations from one path component."""
    stripped = part
    while True:
        shortened = _BACKUP_DECORATION_RE.sub("", stripped)
        if shortened == stripped:
            return stripped
        stripped = shortened


# ripgrep options that consume the NEXT argv value (rg accepts both
# ``--opt value`` and ``--opt=value`` for every option below; audited against
# `rg --help`, ripgrep 15.x). Unconsumed values were misread as operands:
# `rg -g '*' PATTERN` treated the glob '*' as the pattern and the real
# pattern as a (non-directory) path, hiding the default directory walk.
_RG_VALUE_LONG_OPTIONS = frozenset(
    {
        "--after-context",
        "--before-context",
        "--color",
        "--colors",
        "--context",
        "--context-separator",
        "--dfa-size-limit",
        "--encoding",
        "--engine",
        "--field-context-separator",
        "--field-match-separator",
        "--file",
        "--generate",
        "--glob",
        "--hostname-bin",
        "--hyperlink-format",
        "--iglob",
        "--ignore-file",
        "--max-columns",
        "--max-count",
        "--max-depth",
        "--max-filesize",
        "--path-separator",
        "--pre",
        "--pre-glob",
        "--regex-size-limit",
        "--regexp",
        "--replace",
        "--sort",
        "--sortr",
        "--threads",
        "--type",
        "--type-add",
        "--type-clear",
        "--type-not",
    }
)
_RG_VALUE_SHORT_OPTIONS = frozenset("ABCdEefgjmMrtT")
_RG_PATTERN_OPTIONS = frozenset({"-e", "--regexp", "-f", "--file"})


def _rg_directory_walk(args: list[str], cwd: str) -> bool:
    """True when rg will read descendants rather than named regular files.

    The pattern never names the files the walk reaches, so a default
    directory walk can surface a descendant credential file
    (``credentials``, ``production.env``). Only reads whose operand set is
    made entirely of regular files stay routine. Value-taking options must
    consume their values before operand classification, otherwise a flag
    value (or the shifted pattern) is mistaken for a path operand.
    """
    positionals: list[str] = []
    pattern_from_flag = False
    index = 0
    while index < len(args):
        arg = args[index]
        index += 1
        if arg == "--":
            positionals.extend(args[index:])
            break
        if arg.startswith("--"):
            option = arg.partition("=")[0]
            if option in _RG_PATTERN_OPTIONS:
                pattern_from_flag = True
                if "=" not in arg:
                    index += 1  # the pattern itself
            elif "=" not in arg and option in _RG_VALUE_LONG_OPTIONS:
                index += 1  # the option's value
            continue
        if arg.startswith("-") and len(arg) > 1:
            cluster = arg[1:]
            for position, char in enumerate(cluster):
                if char in {"e", "f"}:
                    pattern_from_flag = True
                if char in _RG_VALUE_SHORT_OPTIONS:
                    if position == len(cluster) - 1:
                        index += 1  # value is the next argv
                    break
            continue
        positionals.append(arg)
    paths = positionals if pattern_from_flag else positionals[1:]
    if not paths:
        # No path operand: rg walks the cwd.
        return True
    for value in paths:
        path = Path(value.replace("\\", "/")).expanduser()
        base = path if path.is_absolute() else Path(cwd or ".") / path
        try:
            if base.is_dir():
                return True
        except OSError:
            return True
    return False


# `git grep` options (R4-060) whose values must be consumed before operand
# classification, mirroring the rg tables above; audited against `git grep -h`.
_GIT_GREP_VALUE_LONG_OPTIONS = frozenset(
    {"--context", "--file", "--max-count", "--regexp", "--threads"}
)
_GIT_GREP_VALUE_SHORT_OPTIONS = frozenset("ABCefm")
_GIT_GREP_PATTERN_OPTIONS = frozenset({"-e", "--regexp", "-f", "--file"})


def _git_grep_directory_walk(args: list[str], cwd: str) -> bool:
    """True when ``git grep`` reads repository descendants, not named files.

    ``git grep`` searches every tracked file when its pathspecs do not
    restrict it, and ``--untracked``/``--no-exclude-standard`` additionally
    reach untracked and ignored files (``.env``). Pathspecs are git patterns,
    not filesystem paths: glob/magic/exclude forms match unknown sets and
    review. Only invocations whose operands are all plain regular files stay
    routine (same doctrine as ``_rg_directory_walk``).
    """
    operands: list[str] = []
    pathspecs: list[str] = []
    pattern_from_flag = False
    index = 0
    while index < len(args):
        arg = args[index]
        index += 1
        if arg == "--":
            pathspecs.extend(args[index:])
            break
        if arg.startswith("--"):
            option = arg.partition("=")[0]
            if option == "--open-files-in-pager":
                # Opens matches in a pager command (code execution).
                return True
            if option in _GIT_GREP_PATTERN_OPTIONS:
                pattern_from_flag = True
                if "=" not in arg:
                    index += 1  # the pattern/file value
            elif "=" not in arg and option in _GIT_GREP_VALUE_LONG_OPTIONS:
                index += 1  # the option's value
            continue
        if arg.startswith("-") and len(arg) > 1:
            cluster = arg[1:]
            if "O" in cluster:
                # -O opens matches in a pager command (code execution).
                return True
            for position, char in enumerate(cluster):
                if char in {"e", "f"}:
                    pattern_from_flag = True
                if char in _GIT_GREP_VALUE_SHORT_OPTIONS:
                    if position == len(cluster) - 1:
                        index += 1  # value is the next argv
                    break
            continue
        operands.append(arg)
    paths = (operands if pattern_from_flag else operands[1:]) + pathspecs
    if not paths:
        # No pathspec operand: git grep walks the whole tracked tree.
        return True
    for value in paths:
        if not value or value.startswith((":", "!", "(")) or any(ch in value for ch in "*?["):
            # Magic/glob/exclude pathspecs match an unknown set of files.
            return True
        path = Path(value.replace("\\", "/")).expanduser()
        base = path if path.is_absolute() else Path(cwd or ".") / path
        try:
            if base.is_dir():
                return True
        except OSError:
            return True
    return False


def _recursive_traversal(argv: list[str], cwd: str) -> bool:
    """True when an execution walks directory descendants.

    Recursive reads have unknown match sets: the pattern argument never names
    the files it reaches, so an otherwise-routine command can surface a
    descendant credential file (e.g. ``grep -R TOKEN .`` matching ``.env``,
    or ripgrep's default directory walk reaching ``credentials``, or
    ``git grep --untracked`` reaching ignored ``.env`` files). These require
    explicit review; reads whose operand set is all regular files stay
    routine.
    """
    # `uv run <tool>` must classify as the tool it launches; otherwise the
    # recursive forms below escape review by one level of indirection.
    while len(argv) > 2 and executable_name(argv[0]) == "uv" and argv[1] == "run":
        argv = argv[2:]
    if not argv:
        return False
    name = executable_name(argv[0])
    args = argv[1:]
    if name == "grep":
        for index, arg in enumerate(args):
            if arg in {"--recursive", "--dereference-recursive"}:
                return True
            # R01: -d/--directories=recurse walks descendants exactly like
            # -R but used to slip past the compact-flag check below.
            if arg in {"-d", "--directories"}:
                if index + 1 < len(args) and args[index + 1] == "recurse":
                    return True
            if arg.startswith("--directories=") and arg.partition("=")[2] == "recurse":
                return True
            if arg.startswith("-d") and not arg.startswith("--") and arg[2:] == "recurse":
                return True
            if (
                arg.startswith("-")
                and not arg.startswith("--")
                and any(ch in arg[1:] for ch in ("r", "R"))
            ):
                return True
        return False
    if name == "rg":
        # ripgrep searches recursively by default but skips hidden files.
        # These flags defeat that skip, so dotfiles become reachable.
        for arg in args:
            if arg in {"--hidden", "--unrestricted", "--no-ignore"}:
                return True
            if arg.startswith("-") and not arg.startswith("--") and "u" in arg[1:]:
                return True
        # R01: even without hidden-file flags, every directory path operand
        # (or the cwd when no path operand is given) is walked by default,
        # so visible credential files (credentials, production.env) are
        # reachable. Read sets made only of regular-file operands stay
        # routine; value-taking options are consumed so flag values never
        # masquerade as operands (R01 follow-up).
        return _rg_directory_walk(args, cwd)
    if name == "find":
        # find walks its roots; any directory operand (or no operand at all)
        # descends into potentially sensitive dotfiles.
        roots: list[str] = []
        for arg in args:
            if arg.startswith("-"):
                break
            roots.append(arg)
        if not roots:
            return True
        for value in roots:
            path = Path(value.replace("\\", "/")).expanduser()
            base = path if path.is_absolute() else Path(cwd or ".") / path
            try:
                if base.is_dir():
                    return True
            except OSError:
                return True
        return False
    if name == "git":
        # R4-060: `git grep` reads tracked file contents recursively, and
        # `--untracked`/`--no-exclude-standard` reach ignored files (.env);
        # it gates exactly like the grep/rg walks. Other git reads are
        # name/diff output and keep their existing classification.
        if args and args[0] == "grep":
            return _git_grep_directory_walk(args[1:], cwd)
        return False
    return False


_PROTECTED_COMPONENTS = frozenset({".git", ".machina"})


def _tree_contains_component(
    root: Path,
    names: frozenset[str],
    *,
    max_entries: int = 4096,
    follow_symlinks: bool = False,
) -> bool:
    """Scan for protected path components reachable by a copy (R02).

    Descendant symlinks are traversed only when the effective copy
    dereferences them (``cp -L``); a copied link alone writes no descendants.
    A truncated scan (inspection cap) cannot prove the absence of protected
    components, so it reports True (fail closed) and forces review instead of
    allowing the copy. R4-062: an unreadable directory (listing or entry stat
    failure) cannot be proven clear either; it reports True as well. Symlink
    cycles are guarded by a visited-target set.
    """
    if not root.is_dir():
        return False
    seen = 0
    stack = [root]
    visited: set[str] = set()
    while stack:
        current = stack.pop()
        try:
            identity = str(current.resolve())
        except OSError:
            identity = str(current)
        if identity in visited:
            continue
        visited.add(identity)
        try:
            entries = list(current.iterdir())
        except OSError:
            # Cannot prove absence without listing: fail closed.
            return True
        for entry in entries:
            seen += 1
            if seen > max_entries:
                # Cannot prove absence beyond the cap: fail closed.
                return True
            if entry.name.lower() in names:
                return True
            try:
                if entry.is_dir() and (follow_symlinks or not entry.is_symlink()):
                    stack.append(entry)
            except OSError:
                # A failed stat hides whether this entry is a directory to
                # descend into: fail closed.
                return True
    return False


def _recursive_copy_sources(argv: list[str], cwd: str) -> list[tuple[Path, bool]]:
    """Directory sources of a recursive ``cp``/``mv`` plus link-follow mode.

    R02: the operands of a recursive directory write name only the roots.
    ``cp -R source/. target`` overwrites ``target/.git/config`` although the
    destination operand is just ``target``; returning the source roots lets
    the policy scan the descendants the command will actually write.

    Follow mode mirrors cp's symlink semantics (last flag wins):
    ``-L/--dereference`` dereferences every source symlink, so descendant
    links are reachable during inspection; ``-P/--no-dereference`` (cp -R's
    default) and ``-d`` keep links as links, as does ``-a/--archive``.
    ``-H`` follows only command-line operands, which the directory check
    below already resolves; ``mv`` renames entries and never dereferences.
    """
    while len(argv) > 2 and executable_name(argv[0]) == "uv" and argv[1] == "run":
        argv = argv[2:]
    if not argv:
        return []
    name = executable_name(argv[0])
    if name not in {"cp", "mv"}:
        return []
    is_cp = name == "cp"
    recursive = name == "mv"  # directory moves recurse without a flag
    follow = False
    target_dir = ""
    operands: list[str] = []
    args = argv[1:]
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in {"-t", "--target-directory"}:
            index += 1
            target_dir = args[index] if index < len(args) else ""
        elif arg.startswith("--target-directory="):
            target_dir = arg.partition("=")[2]
        elif arg == "--dereference":
            if is_cp:
                follow = True
        elif arg == "--no-dereference":
            if is_cp:
                follow = False
        elif arg in {"--recursive", "--archive"}:
            recursive = True
            if arg == "--archive":
                follow = False
        elif arg.startswith("-") and not arg.startswith("--") and len(arg) > 1:
            cluster = arg[1:]
            for position, char in enumerate(cluster):
                if char in {"r", "R", "a"}:
                    recursive = True
                if is_cp:
                    if char == "L":
                        follow = True
                    elif char in {"P", "d", "a"}:
                        follow = False
                if char == "t":
                    # -t inside a cluster: the rest (or the next argv) is
                    # the target directory operand.
                    rest = cluster[position + 1 :]
                    if rest:
                        target_dir = rest
                    else:
                        index += 1
                        target_dir = args[index] if index < len(args) else ""
                    break
        else:
            operands.append(arg)
        index += 1
    if not recursive:
        return []
    if target_dir:
        sources = operands
    elif len(operands) > 1:
        sources = operands[:-1]
    else:
        return []
    roots: list[tuple[Path, bool]] = []
    for value in sources:
        path = Path(value.replace("\\", "/")).expanduser()
        base = path if path.is_absolute() else Path(cwd or ".") / path
        try:
            if base.is_dir():
                roots.append((base, follow))
        except OSError:
            continue
    return roots


def _sensitive_capabilities(req: ActionRequest) -> list[str]:
    """Exact capability identities plus operation-aware path/command checks."""
    from machina.permissions.commands import (
        command_path_values,
        executable_name,
        routine_project_command,
        routine_shell_command,
    )

    found = [c for c in (req.capabilities or []) if c in REVIEW_CAPABILITIES]
    first = executable_name(req.argv[0]) if req.argv else ""
    root = Path(req.workspace_root).resolve() if req.workspace_root else workspace_root()
    if req.shell_payload:
        from dataclasses import replace

        from machina.permissions.shell import simple_shell_commands

        for stage in simple_shell_commands(req.shell_payload) or []:
            found.extend(_sensitive_capabilities(replace(req, argv=stage, shell_payload="")))
        if not routine_shell_command(req.shell_payload, req.cwd, root):
            found.append("opaque_execution_review")
        return sorted(set(found))
    routine = not req.shell_payload and routine_project_command(req.argv, req.cwd, root)
    if "opaque_execution" in (req.capabilities or []) and not routine:
        found.append("opaque_execution_review")
    paths = list(req.targets)
    if req.operation == "process.exec":
        for value in command_path_values(req.argv):
            path = Path(value).expanduser()
            paths.append(str((path if path.is_absolute() else Path(req.cwd) / path).resolve()))
    if req.operation in {"file.read", "file.write", "process.exec"}:
        if any(sensitive_path(v, root) for v in paths):
            found.append("credential_access")
    writes_paths = req.operation == "file.write" or (
        req.operation == "process.exec"
        and first in {"cp", "mv", "mkdir", "touch", "chmod", "chown", "tsc", "ruff"}
    )
    if writes_paths and any(
        # Casefold components (RF3-F1): macOS/Windows filesystems are
        # case-insensitive, so ".GIT" aliases the real ".git"; the previous
        # case-sensitive check let unapproved hook/config writes through.
        {p.lower() for p in Path(t).parts} & {".git", ".machina"}
        for t in paths
    ):
        found.append("security_settings")
    if req.operation == "process.exec" and first in {"cp", "mv", "uv"}:
        # R02: a recursive directory write changes descendants that the
        # operands never name; classify the protected ones the copy will
        # actually reach (e.g. source/.git/config -> target/.git/config).
        # Under `cp -L` the reachable set includes symlink targets.
        if any(
            _tree_contains_component(source, _PROTECTED_COMPONENTS, follow_symlinks=follow_symlinks)
            for source, follow_symlinks in _recursive_copy_sources(req.argv, req.cwd)
        ):
            found.append("security_settings")
    if (
        first
        in {
            "sudo",
            "su",
            "runas",
            "chown",
            "passwd",
            "iptables",
            "set-mppreference",
            "set-executionpolicy",
        }
        or first == "chmod"
        and "777" in req.argv
    ):
        found.append("privilege_escalation")
    if first in {
        "rm",
        "rmdir",
        "dd",
        "mkfs",
        "fdisk",
        "shutdown",
        "reboot",
        "halt",
        "diskpart",
        "format",
        "del",
        "rd",
        "remove-item",
    }:
        found.append("destructive_system")
    if (
        first == "git"
        and len(req.argv) > 1
        and req.argv[1] in {"reset", "clean", "restore", "checkout", "rebase"}
    ):
        found.append("destructive_system")
    if first == "find" and "-delete" in req.argv:
        found.append("destructive_system")
    # A recursive traversal's match set is unknown at approval time; the
    # pattern never names the files it reaches, so descendant dotfiles can
    # leak through otherwise-routine output. Review instead of allow.
    if req.operation == "process.exec" and _recursive_traversal(req.argv, req.cwd):
        found.append("opaque_execution_review")
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
        "mcp_external_review",
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


def _inside_workspace(path: str, root: Path | None = None) -> bool:
    """Request-bound containment: the approved workspace root, not the live global."""
    try:
        base = root if root is not None else workspace_root()
        return Path(path).resolve().is_relative_to(base)
    except Exception as _boundary_error:
        # Optional fallback preserves the primary outcome; report no payload.
        record_failure("policy.inside_workspace", _boundary_error)
        return False


def _has_full_grant(grants: list[dict], req: ActionRequest) -> bool:
    """True when a live full-mode session grant authorizes this session."""
    for g in grants:
        if not _grant_usable(g, req):
            continue
        if (
            g.get("mode") == "full"
            and g.get("capability") == "session"
            and g.get("digest") == "full-mode-session"
        ):
            return True
    return False


def decide(req: ActionRequest, grants: list[dict] | None = None) -> Decision:
    """Policy decision without side effects. Grants checked by digest/scope."""
    grants = grants or []
    # FM3-F7: containment is judged against the REQUEST-BOUND workspace root
    # (the same root normalize_request resolved targets against), never the
    # live global setting, which may have changed since the request was built.
    req_root = Path(req.workspace_root).resolve() if req.workspace_root else workspace_root()
    if req.mode == "full" and _has_full_grant(grants, req):
        # Full mode is explicit no-approval authority for THIS session:
        # a live full-mode session grant allows everything, including
        # sensitive capabilities, without approval cards.
        return Decision("allowed", "full-mode session grant", req)
    if req.mode == "full":
        # LP-07: full authority comes from a live full-mode grant bound to
        # THIS session — never from the global config value alone. A global
        # `full` default only auto-grants at session creation (disclosed
        # persistent scope); activating session A never elevates session B.
        return Decision(
            "pending", "full mode requires explicit session activation (/mode full)", req
        )
    sens = _sensitive_capabilities(req)
    if sens:
        # Sensitive exclusions require explicit approval in ask/workspace/
        # sandbox modes. Full mode (with a live session grant, checked above)
        # bypasses this. EVERY required sensitive capability must be covered
        # by exact capability identity on a live, digest-bound grant for this
        # exact action. One matching grant never authorizes the others, and
        # substring matching is never used.
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
    if req.mode == "sandbox" and req.operation == "process.exec" and req.backend != "sandbox":
        return Decision(
            "pending",
            "sandbox mode requires isolated backend; approve backend change or switch mode",
            req,
        )
    # Default ask is risk-based: ordinary project edits and known tool reads
    # proceed. Sensitive actions were handled above in every mode.
    if req.operation == "file.read" and all(_inside_workspace(t, req_root) for t in req.targets):
        return Decision("allowed", "trusted project read", req)
    if (
        req.mode in ("ask", "workspace", "sandbox")
        and req.operation in ("file.read", "file.write")
        and all(_inside_workspace(t, req_root) for t in req.targets)
    ):
        return Decision("allowed", "routine project file operation", req)
    if req.mode in ("ask", "workspace", "sandbox"):
        if req.operation == "network.fetch" and req.tool in {
            "web_search",
            "web_extract",
            "web_research",
            "web_crawl",
        }:
            return Decision("allowed", "built-in web read (backend validates URLs)", req)
        if req.operation == "process.exec":
            from machina.permissions.commands import routine_project_command, routine_shell_command

            root = Path(req.workspace_root).resolve() if req.workspace_root else workspace_root()
            if (
                routine_shell_command(req.shell_payload, req.cwd, root)
                if req.shell_payload
                else routine_project_command(req.argv, req.cwd, root)
            ):
                return Decision("allowed", "routine trusted project command", req)
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
        cap = str(g.get("capability") or "")
        if cap == "session":
            # FM3-F11: "session" capability is full-mode authority by
            # construction. Never let a scope-less session grant blanket-pass
            # every non-mutating operation; require the exact live-full-grant
            # identity (mode + digest) instead.
            if not (g.get("mode") == "full" and g.get("digest") == "full-mode-session"):
                continue
            return Decision("allowed", "full-mode session grant covers target", req)
        if cap in (req.operation, "workspace"):
            return Decision("allowed", "scope grant covers target", req)
    if (
        req.operation in ("file.write", "process.exec")
        or req.targets
        and not all(_inside_workspace(t, req_root) for t in req.targets)
    ):
        return Decision("pending", "write/exec or out-of-scope access requires approval", req)
    return Decision("pending", "action requires approval", req)


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
        except Exception as _boundary_error:
            # Optional fallback preserves the primary outcome; report no payload.
            record_failure("policy.grant_expiry", _boundary_error)
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
    from machina.core.config import config

    # AH-AUDIT-004: redacted display preview bound to the same identity as
    # the executable digest. Truncated with full length disclosed. Attached
    # Where content is the proposal (file.write/extension arguments); other
    # operations show argv/targets, never raw secret-bearing blobs.
    preview = ""
    preview_len = 0
    if content:
        preview_len = len(content)
    if content and (operation == "file.write" or operation.startswith("tool.")):
        try:
            from machina.memory.redaction import redact_secrets as _redact

            preview = _redact(content[:4000]).text
            if preview_len > 4000:
                # FM3-F4: display-only truncation, explicit hidden count; the
                # digest above always binds the FULL payload.
                preview += f"\n…(+{preview_len - 4000} chars)"
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
