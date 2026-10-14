"""Interop self-setup — detect external agent harnesses and register Machina.

Machina ships an MCP server (``python -m machina.mcp_server``) that external
agent harnesses — OpenCode, Codex, Hermes — can call as tools. Registering it
used to be a manual, per-host recipe; this module makes that a first-class,
inspectable operation:

``detect()``
    Per platform: installed (``which``), version probe, running check
    (process-list scan), and registration evidence (the platform's own
    ``mcp list`` output plus the known config file for a ``machina`` entry).

``enable(platform, scope=..., dry_run=...)``
    Idempotent registration through the platform CLI, then verification by
    re-listing, then state persistence in ``~/.machina/interop.json``.

``disable(platform)``
    Removal where the platform CLI has a remove subcommand; platforms
    without one (OpenCode) report the exact manual step instead.

``enable_detected(...)``
    One best-effort sweep for setup/installer flows: detect every harness,
    print a summary line per platform, and act only on the installed but
    unregistered ones — prompting on a TTY (or via an ask-callback), enabling
    an explicit ``only`` list non-interactively, and never raising.

Design constraints (mirrors ``machina/mcp_server``):

* Module imports are stdlib only; the ``machina.core.child_process`` leaf is
  bound lazily per call (``_child_process``) so importing this module never
  pulls the agent runtime.
* Every spawn is bounded (<= 30s) through the shared runner
  (``child_process.run_bounded``): minimal environment allowlist, its own
  process group, a kill-tree deadline, and size-capped ANSI-stripped output
  that is secret-redacted before it lands in an ActionResult. Platform auth
  files are never touched.
* The state file is written atomically (temp + ``os.replace``) with mode
  0600 under an flock guard covering the read-merge-replace section, so two
  concurrent writers cannot drop each other's keys.
  ``MACHINA_INTEROP_STATE_FILE`` overrides its path (tests).

Scope notes: OpenCode supports project *and* global registration; Codex and
Hermes register user-level only (their CLIs have no project flag), so a
``scope="project"`` request for them is clamped to ``global`` and reported
as such. Project-scope OpenCode files resolve against the Machina checkout
root (``_repo_root``), which is the project the registration belongs to; in
a wheel install that anchor is site-packages, so project scope is refused
with a pointer to ``--scope global`` (round-5 review F3).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# State-file locking follows machina/connectors/store.py: an exclusive OS
# file lock on a sibling ``.lock`` file (POSIX flock; msvcrt on Windows).
_WINDOWS = os.name == "nt"
if _WINDOWS:  # pragma: no cover - platform branch
    import msvcrt
else:
    import fcntl

__all__ = [
    "PLATFORMS",
    "ActionResult",
    "DetectResult",
    "EnableReport",
    "UnknownPlatform",
    "detect",
    "disable",
    "enable",
    "enable_detected",
    "load_state",
    "resolve_platform",
    "server_command",
    "state_path",
    "stdin_is_tty",
]

SERVER_NAME = "machina"
SERVER_MODULE = "machina.mcp_server"

# Bounds: version/list/action spawns stay well under the 30s ceiling.
VERSION_TIMEOUT_SECONDS = 10.0
LIST_TIMEOUT_SECONDS = 20.0
ACTION_TIMEOUT_SECONDS = 30.0
RUNNING_TIMEOUT_SECONDS = 5.0

MAX_OUTPUT_CHARS = 32_000
MAX_STDERR_CHARS = 8_000
MAX_VERSION_CHARS = 200
MAX_DETAIL_CHARS = 500

STATE_FILENAME = "interop.json"
STATE_DIRNAME = ".machina"
STATE_ENV_OVERRIDE = "MACHINA_INTEROP_STATE_FILE"

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")


class UnknownPlatform(ValueError):
    """The requested platform key is not one of the supported harnesses."""

    def __init__(self, platform: str) -> None:
        self.platform = platform
        self.known = tuple(PLATFORMS)
        super().__init__(f"Unknown platform {platform!r}; choose one of: {', '.join(self.known)}")


# ---------------------------------------------------------------------------
# Platform descriptors
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlatformSpec:
    """Declarative descriptor for one external harness.

    ``binaries`` are candidate executable names in PATH order; ``list_args``
    produce that platform's MCP server listing (``list_json`` adds ``--json``
    for Codex); ``remove_args`` is ``None`` when the CLI has no remove
    subcommand (disable becomes a manual-step report). ``supports_scopes``
    names the registration scopes the CLI actually writes; ``enable_stdin`` /
    ``disable_stdin`` feed the one-line stdin the platform's interactive
    confirmation expects (Hermes).
    """

    key: str
    label: str
    binaries: tuple[str, ...]
    version_args: tuple[str, ...]
    list_args: tuple[str, ...]
    supports_scopes: tuple[str, ...]
    remove_args: tuple[str, ...] | None
    list_json: bool = False
    enable_stdin: str | None = None
    disable_stdin: str | None = None
    env_passthrough: tuple[str, ...] = field(default_factory=tuple)


PLATFORMS: dict[str, PlatformSpec] = {
    "opencode": PlatformSpec(
        key="opencode",
        label="OpenCode",
        binaries=("opencode",),
        version_args=("--version",),
        list_args=("mcp", "list"),
        supports_scopes=("project", "global"),
        remove_args=None,  # v2.0.26 `opencode mcp` has list/add/auth/logout only.
        env_passthrough=("XDG_CONFIG_HOME", "XDG_DATA_HOME"),
    ),
    "codex": PlatformSpec(
        key="codex",
        label="Codex",
        binaries=("codex",),
        version_args=("--version",),
        list_args=("mcp", "list"),
        list_json=True,  # `codex mcp list --json` — stable machine output.
        supports_scopes=("global",),  # `codex mcp add` writes ~/.codex/config.toml.
        remove_args=("mcp", "remove", SERVER_NAME),
        env_passthrough=("CODEX_HOME",),
    ),
    "hermes": PlatformSpec(
        key="hermes",
        label="Hermes",
        binaries=("hermes",),
        version_args=("--version",),
        list_args=("mcp", "list"),
        supports_scopes=("global",),  # `hermes mcp add` writes config.yaml (user-level).
        remove_args=("mcp", "remove", SERVER_NAME),
        # `hermes mcp add` probes the server ("discovery-first") and then asks
        # one enable-all question; `mcp remove` asks one confirm. A newline
        # answers both with their defaults (enable all / remove yes).
        enable_stdin="\n",
        disable_stdin="\n",
        env_passthrough=("HERMES_HOME",),
    ),
}


def resolve_platform(platform: str) -> PlatformSpec:
    """Return the descriptor for *platform*, or raise ``UnknownPlatform``."""
    key = (platform or "").strip().lower()
    if key not in PLATFORMS:
        raise UnknownPlatform(platform)
    return PLATFORMS[key]


def _repo_root() -> Path:
    """Checkout/install root this module runs from (project-scope anchor)."""
    return Path(__file__).resolve().parent.parent


# Installed wheels resolve ``_repo_root()`` to a site-packages directory;
# registering project scope there would write next to the installed package
# instead of a real checkout (round-5 review F3).
_SITE_PACKAGES_DIRS = frozenset({"site-packages", "dist-packages"})

PROJECT_SCOPE_NEEDS_CHECKOUT = (
    "project scope needs a source checkout; use --scope global or run from the checkout"
)


def _in_site_packages(path: Path) -> bool:
    """True when *path* resolves inside an installed-package tree."""
    return any(part in _SITE_PACKAGES_DIRS for part in path.resolve().parts)


def _hermes_home() -> Path:
    override = os.environ.get("HERMES_HOME", "").strip()
    return Path(override) if override else Path.home() / ".hermes"


def _codex_home() -> Path:
    override = os.environ.get("CODEX_HOME", "").strip()
    return Path(override) if override else Path.home() / ".codex"


def _opencode_global_config() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "opencode" / "opencode.json"


def _config_files(spec: PlatformSpec) -> dict[str, Path]:
    """Known registration files per scope (used for pre-checks and fallback)."""
    if spec.key == "opencode":
        return {
            "project": _repo_root() / "opencode.json",
            "global": _opencode_global_config(),
        }
    if spec.key == "codex":
        return {"global": _codex_home() / "config.toml"}
    if spec.key == "hermes":
        return {"global": _hermes_home() / "config.yaml"}
    return {}


# ---------------------------------------------------------------------------
# Registration argv templates (parameterized by interpreter + repo root)
# ---------------------------------------------------------------------------


def server_command() -> list[str]:
    """The stdio server command registered with every harness."""
    return [sys.executable, "-m", SERVER_MODULE]


def add_argv(spec: PlatformSpec, binary: str, scope: str) -> list[str]:
    """Argv that registers the Machina MCP server through the platform CLI."""
    command = server_command()
    if spec.key == "opencode":
        argv = [binary, "mcp", "add"]
        if scope == "global":
            argv.append("--global")
        return [*argv, SERVER_NAME, "--", *command]
    if spec.key == "codex":
        return [binary, "mcp", "add", SERVER_NAME, "--", *command]
    if spec.key == "hermes":
        return [
            binary,
            "mcp",
            "add",
            SERVER_NAME,
            "--command",
            sys.executable,
            "--args",
            "-m",
            SERVER_MODULE,
        ]
    raise UnknownPlatform(spec.key)  # pragma: no cover — descriptors are fixed


def remove_argv(spec: PlatformSpec, binary: str) -> list[str] | None:
    """Argv that removes the registration, or ``None`` when unsupported."""
    if spec.remove_args is None:
        return None
    return [binary, *spec.remove_args]


# ---------------------------------------------------------------------------
# Bounded child process runner (thin adapter over child_process.run_bounded)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunResult:
    """Outcome of one bounded child process.

    Public shape kept from before the F2 extraction; ``_run`` maps the
    shared runner's :class:`~machina.core.child_process.BoundedResult` onto
    it.
    """

    returncode: int | None
    output: str
    stderr: str
    timed_out: bool
    truncated: bool


def _child_process():
    """Lazily bound stdlib-only child-process mechanics (import-light)."""
    from machina.core import child_process

    return child_process


def _child_env(spec: PlatformSpec) -> dict[str, str]:
    """Minimal child environment without secrets, plus config-location vars.

    The shared allowlist deliberately drops unfamiliar variables; a few
    platform home/config overrides must survive so the CLI we talk to sees
    the same layout the user does (never credentials).
    """
    env = _child_process().sanitized_env(None)
    env["NO_COLOR"] = "1"
    for name in spec.env_passthrough:
        value = os.environ.get(name)
        if value:
            env[name] = value
    return env


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _bounded(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len("\n[truncated]"))] + "\n[truncated]"


def _redact_output(text: str) -> str:
    """ANSI-stripped, secret-redacted diagnostics (round-5 review N2').

    Platform CLI output can echo credentials (a token in an error message);
    the redactor is bound lazily (same idiom as ``machina/mcp_server``) so
    importing this module stays stdlib-only, and a redaction failure
    degrades to the raw text rather than dropping the diagnostic.
    """
    text = _strip_ansi(text).strip()
    try:
        from machina.memory.redaction import redact_secrets

        return redact_secrets(text).text
    except Exception:
        return text


def _run(
    argv: list[str],
    *,
    env: dict[str, str],
    timeout: float,
    stdin_text: str | None = None,
    cwd: Path | None = None,
) -> RunResult:
    """Run one child through the shared bounded runner (round-5 review F2).

    Thin adapter over ``machina.core.child_process.run_bounded``: process
    group + kill-tree deadline, twin capped readers, optional stdin text,
    and spawn failures returned in ``stderr`` (never raised). The project
    anchor ``_repo_root()`` stays this module's default child cwd; a missing
    anchor falls back to the call-time launch directory inside the leaf.
    """
    anchor: Path | None = cwd if cwd is not None else _repo_root()
    if anchor is not None and not anchor.is_dir():
        anchor = None
    result = _child_process().run_bounded(
        argv,
        env=env,
        timeout=timeout,
        stdin_text=stdin_text,
        cwd=anchor,
        max_stdout=MAX_OUTPUT_CHARS,
        max_stderr=MAX_STDERR_CHARS,
    )
    return RunResult(
        returncode=result.returncode,
        output=result.stdout,
        stderr=result.stderr,
        timed_out=result.timed_out,
        truncated=result.truncated,
    )


def _which(spec: PlatformSpec) -> str | None:
    for name in spec.binaries:
        found = shutil.which(name)
        if found:
            return found
    return None


# ---------------------------------------------------------------------------
# Output parsing (per platform)
# ---------------------------------------------------------------------------

# A listing row starts with optional status marks then the server name.
_ROW_RE = re.compile(r"^[^\w.-]*([A-Za-z0-9][\w.-]*)")
_SKIP_NAMES = frozenset({"name", "mcp", "servers", "server", "status", "transport", "tools"})
_MARKS_RE = re.compile(r"[^\w.-]+")
_STATUS_RE = re.compile(r"\b(connected|enabled|disabled|failed|error|unavailable)\b")


def _clean_row(line: str) -> tuple[str, str] | None:
    """Split one listing row into (name, status-ish trailing text)."""
    stripped = line.strip()
    if not stripped or set(stripped) <= {"─", "-", "="}:
        return None
    match = _ROW_RE.match(stripped)
    if not match:
        return None
    name = match.group(1)
    if name.lower() in _SKIP_NAMES:
        return None
    rest = stripped[match.end() :]
    status_match = _STATUS_RE.search(rest)
    if status_match:
        return name, status_match.group(1)
    status = " ".join(part for part in _MARKS_RE.split(rest) if part)
    return name, status[:80]


def _parse_text_listing(text: str) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for line in _strip_ansi(text).splitlines():
        row = _clean_row(line)
        if row is not None:
            rows.append(row)
    return rows


def _parse_opencode_listing(text: str) -> list[tuple[str, str]]:
    return _parse_text_listing(text)


def _parse_hermes_listing(text: str) -> list[tuple[str, str]]:
    return _parse_text_listing(text)


def _parse_codex_listing(text: str, *, json_mode: bool) -> list[tuple[str, str]]:
    if not json_mode:
        return _parse_text_listing(text)
    payload = json.loads(_strip_ansi(text))
    if isinstance(payload, dict):
        payload = payload.get("servers", payload.get("mcpServers"))
    if not isinstance(payload, list):
        raise ValueError("unexpected codex mcp list --json shape")
    rows: list[tuple[str, str]] = []
    for entry in payload:
        if not isinstance(entry, dict) or not entry.get("name"):
            continue
        enabled = entry.get("enabled", True)
        status = "enabled" if enabled else "disabled"
        if entry.get("disabled_reason"):
            status = f"{status} ({entry['disabled_reason']})"
        rows.append((str(entry["name"]), status))
    return rows


def _parse_listing(spec: PlatformSpec, text: str) -> list[tuple[str, str]]:
    if spec.key == "codex":
        return _parse_codex_listing(text, json_mode=spec.list_json)
    if spec.key == "opencode":
        return _parse_opencode_listing(text)
    if spec.key == "hermes":
        return _parse_hermes_listing(text)
    return _parse_text_listing(text)  # pragma: no cover


@dataclass(frozen=True)
class _ListProbe:
    """Outcome of one ``<platform> mcp list`` probe."""

    ran: bool
    found: bool
    status: str
    detail: str


def _probe_list(spec: PlatformSpec, binary: str | None) -> _ListProbe:
    """Run the platform's list command and look for the machina entry."""
    if not binary:
        return _ListProbe(False, False, "", "platform CLI not installed")
    argv = [binary, *spec.list_args]
    if spec.list_json:
        argv.append("--json")
    result = _run(argv, env=_child_env(spec), timeout=LIST_TIMEOUT_SECONDS)
    if result.timed_out:
        return _ListProbe(False, False, "", "list command timed out")
    if result.returncode != 0:
        detail = _bounded(_strip_ansi(result.stderr or result.output).strip(), MAX_DETAIL_CHARS)
        return _ListProbe(False, False, "", f"list command failed ({detail or 'no output'})")
    try:
        rows = _parse_listing(spec, result.output)
    except ValueError as error:
        return _ListProbe(False, False, "", f"could not parse list output ({error})")
    for name, status in rows:
        if name == SERVER_NAME:
            return _ListProbe(True, True, status, f"listed ({status or 'present'})")
    return _ListProbe(True, False, "", "not listed")


def _probe_version(spec: PlatformSpec, binary: str) -> str | None:
    result = _run(
        [binary, *spec.version_args], env=_child_env(spec), timeout=VERSION_TIMEOUT_SECONDS
    )
    if result.timed_out or result.returncode != 0:
        return None
    for line in _strip_ansi(result.output).splitlines():
        line = line.strip()
        if line:
            return line[:MAX_VERSION_CHARS]
    return None


def _line_matches_binary(line: str, names: tuple[str, ...]) -> bool:
    try:
        pid_text, _, command = line.strip().partition(" ")
        if pid_text.isdigit() and int(pid_text) == os.getpid():
            return False
    except (ValueError, AttributeError):
        command = line
    if not command.strip():
        return False
    for name in names:
        if re.search(rf"(^|[/\s]){re.escape(name)}(\s|$)", command):
            return True
    return False


def _is_running(spec: PlatformSpec) -> bool:
    """Best-effort process-list scan for the platform binary. Never raises."""
    pattern = "|".join(re.escape(name) for name in spec.binaries)
    try:
        pgrep = shutil.which("pgrep")
        if os.name != "nt" and pgrep:
            result = _run(
                [pgrep, "-fl", pattern], env=_child_env(spec), timeout=RUNNING_TIMEOUT_SECONDS
            )
            lines = result.output.splitlines() if result.returncode in (0, 1) else []
        else:
            ps = shutil.which("ps")
            if not ps:
                return False
            result = _run(
                [ps, "-axo", "pid=,command="], env=_child_env(spec), timeout=RUNNING_TIMEOUT_SECONDS
            )
            lines = result.output.splitlines()
        return any(_line_matches_binary(line, spec.binaries) for line in lines)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Config-file evidence (fallback + scope-aware idempotency pre-check)
# ---------------------------------------------------------------------------

_TOML_SECTION_RE = re.compile(r'^\s*\[mcp_servers\.(?:machina|"machina")\]\s*$', re.MULTILINE)


def _registered_in_file(spec: PlatformSpec, path: Path) -> bool:
    """True when *path* holds a machina entry (best effort, never raises)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    if spec.key == "opencode":
        try:
            data = json.loads(text)
        except ValueError:
            return False
        mcp = data.get("mcp") if isinstance(data, dict) else None
        if not isinstance(mcp, dict):
            return False
        servers = mcp.get("servers")
        if isinstance(servers, dict) and SERVER_NAME in servers:
            return True
        return SERVER_NAME in {k for k, v in mcp.items() if isinstance(v, dict)}
    if spec.key == "codex":
        return _TOML_SECTION_RE.search(text) is not None
    if spec.key == "hermes":
        # Indentation scan: a `machina:` child of the top-level `mcp_servers:`
        # block (the shape `hermes mcp add` writes).
        inside = False
        for line in text.splitlines():
            if re.match(r"^mcp_servers\s*:", line):
                inside = True
                continue
            if inside:
                if line.strip() and not line[0].isspace():
                    break
                if re.match(r"^\s+machina\s*:", line) and not re.match(r"^\s{8,}", line):
                    return True
        return False
    return False


def _file_scopes(spec: PlatformSpec) -> list[str]:
    return [scope for scope, path in _config_files(spec).items() if _registered_in_file(spec, path)]


# ---------------------------------------------------------------------------
# State file (~/.machina/interop.json)
# ---------------------------------------------------------------------------


def state_path() -> Path:
    """Path of the interop state file (env override for tests)."""
    override = os.environ.get(STATE_ENV_OVERRIDE, "").strip()
    if override:
        return Path(override)
    return Path.home() / STATE_DIRNAME / STATE_FILENAME


def load_state() -> dict[str, Any]:
    """Read the state file; missing/corrupt content reads as empty."""
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(state: dict[str, Any]) -> None:
    """Atomic write (temp + os.replace), mode 0600."""
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".interop-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@contextlib.contextmanager
def _state_lock() -> Iterator[None]:
    """Exclusive lock around one state read-modify-write (round-5 review N1').

    ``_record_state`` is load -> merge -> atomic replace; two concurrent
    writers could interleave and the last replace would drop the other's
    keys. Mirroring ``machina/connectors/store.py``, an exclusive OS lock on
    a sibling ``.lock`` file serializes the whole section (POSIX flock;
    msvcrt on Windows). The lock file is created but never holds state, and
    the atomic replace keeps the state file always readable.
    """
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    try:
        if _WINDOWS:  # pragma: no cover - platform branch
            # msvcrt locks a byte range from the current file position.
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX)
        acquired = True
        yield
    finally:
        try:
            if acquired:
                if _WINDOWS:  # pragma: no cover - platform branch
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _record_state(
    spec: PlatformSpec,
    *,
    enabled: bool,
    scope: str,
    verify: dict[str, Any],
    binary: str | None,
    version: str | None,
) -> None:
    with _state_lock():
        state = load_state()
        previous = state.get(spec.key)
        previous = previous if isinstance(previous, dict) else {}
        registered_at = (
            previous.get("registered_at")
            if not enabled
            else previous.get("registered_at") or _now_iso()
        )
        state[spec.key] = {
            "enabled": bool(enabled),
            "scope": scope,
            "registered_at": registered_at,
            "verify": {"ok": bool(verify.get("ok")), "detail": str(verify.get("detail", ""))},
            "binary": binary,
            "version": version,
        }
        _save_state(state)


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


@dataclass
class DetectResult:
    """One platform's live interop picture."""

    platform: str
    label: str
    installed: bool
    binary: str | None
    version: str | None
    running: bool
    registered: bool
    registered_detail: str
    scopes: list[str]
    enabled: bool | None
    state_scope: str | None
    notes: list[str]

    @property
    def enabled_cell(self) -> str:
        """Compact display for the status table: yes / no / manual."""
        if self.enabled is not None:
            return "yes" if self.enabled else "no"
        return "manual" if self.registered else "no"


def detect_platform(spec: PlatformSpec, state: dict[str, Any] | None = None) -> DetectResult:
    """Detect one platform: installed, version, running, registered, enabled."""
    if state is None:
        state = load_state()
    binary = _which(spec)
    installed = binary is not None
    version = _probe_version(spec, binary) if binary else None
    probe = _probe_list(spec, binary) if binary else _ListProbe(False, False, "", "not installed")
    file_scopes = _file_scopes(spec)
    registered = probe.found or bool(file_scopes)
    notes: list[str] = []
    if not installed:
        notes.append(f"not installed ({spec.binaries[0]} not on PATH)")
    if file_scopes and not probe.found:
        notes.append(f"config entry in {', '.join(file_scopes)} scope")
    entry = state.get(spec.key)
    entry = entry if isinstance(entry, dict) else None
    enabled = bool(entry.get("enabled")) if entry else None
    state_scope = str(entry.get("scope")) if entry and entry.get("scope") else None
    if registered and entry is None:
        notes.append("registered outside Machina's state (shows as manual)")
    if not registered and entry is not None and entry.get("enabled"):
        notes.append("state says enabled but no registration found")
    return DetectResult(
        platform=spec.key,
        label=spec.label,
        installed=installed,
        binary=binary,
        version=version,
        running=_is_running(spec),
        registered=registered,
        registered_detail=probe.detail,
        scopes=file_scopes,
        enabled=enabled,
        state_scope=state_scope,
        notes=notes,
    )


def detect(platforms: list[str] | tuple[str, ...] | None = None) -> dict[str, DetectResult]:
    """Detect every supported platform (or the requested subset), in order."""
    keys = list(platforms) if platforms else list(PLATFORMS)
    state = load_state()
    results: dict[str, DetectResult] = {}
    for key in keys:
        spec = resolve_platform(key)
        results[spec.key] = detect_platform(spec, state)
    return results


# ---------------------------------------------------------------------------
# Enable / disable
# ---------------------------------------------------------------------------


@dataclass
class ActionResult:
    """Outcome of an enable/disable attempt."""

    platform: str
    action: str
    ok: bool
    changed: bool
    message: str
    steps: list[str] = field(default_factory=list)
    verify: dict[str, Any] = field(default_factory=dict)
    argv: list[list[str]] = field(default_factory=list)


def _effective_scope(spec: PlatformSpec, scope: str) -> tuple[str, list[str]]:
    """Clamp the requested scope to what the platform CLI actually supports."""
    requested = (scope or "project").strip().lower()
    if requested not in ("project", "global"):
        raise ValueError(f"scope must be project or global, got {scope!r}")
    if requested in spec.supports_scopes:
        return requested, []
    if "global" in spec.supports_scopes:
        return "global", [
            f"{spec.label} registers user-level only (no project scope); using global scope"
        ]
    return requested, []  # unreachable with current descriptors


def _verify_presence(spec: PlatformSpec, scope: str) -> dict[str, Any]:
    """Confirm a machina entry exists (platform listing or config file)."""
    binary = _which(spec)
    probe = _probe_list(spec, binary) if binary else _ListProbe(False, False, "", "not installed")
    if binary:
        list_args = [*spec.list_args, *(["--json"] if spec.list_json else [])]
        list_display = f"{Path(binary).name} {shlex.join(list_args)}"
    else:
        list_display = "mcp list"
    if probe.ran and probe.found:
        return {"ok": True, "detail": f"present in `{list_display}` ({probe.status or 'found'})"}
    path = _config_files(spec).get(scope)
    if path is not None and _registered_in_file(spec, path):
        return {"ok": True, "detail": f"present in {path}"}
    if probe.ran:
        target = f" or {path}" if path is not None else ""
        return {"ok": False, "detail": f"not present in `{list_display}`{target}"}
    return {"ok": False, "detail": probe.detail or "could not verify registration"}


def _verify_absence(spec: PlatformSpec, scope: str) -> dict[str, Any]:
    """Confirm no machina entry remains (listing clean and config file clean)."""
    binary = _which(spec)
    probe = _probe_list(spec, binary) if binary else _ListProbe(False, False, "", "not installed")
    if probe.ran and probe.found:
        return {"ok": False, "detail": f"still listed ({probe.status or 'present'})"}
    path = _config_files(spec).get(scope)
    if path is not None and _registered_in_file(spec, path):
        return {"ok": False, "detail": f"still present in {path}"}
    if probe.ran:
        checked = f"config file {path}" if path is not None else "config files"
        return {"ok": True, "detail": f"absent from `mcp list` and {checked}"}
    if path is not None:
        return {"ok": True, "detail": f"absent from config file {path} (list unavailable)"}
    return {"ok": False, "detail": probe.detail or "could not verify removal"}


def enable(
    platform: str,
    *,
    scope: str = "project",
    dry_run: bool = False,
) -> ActionResult:
    """Idempotently register Machina's MCP server with *platform*.

    Returns an :class:`ActionResult`; never raises for platform-side failures
    (only ``UnknownPlatform``/``ValueError`` for bad input). With
    ``dry_run=True`` no subprocess is spawned — the planned argv is returned.
    """
    spec = resolve_platform(platform)
    try:
        effective_scope, scope_notes = _effective_scope(spec, scope)
    except ValueError as error:
        return ActionResult(spec.key, "enable", False, False, str(error))

    binary = _which(spec)
    steps: list[str] = list(scope_notes)
    if not binary:
        return ActionResult(
            spec.key,
            "enable",
            False,
            False,
            f"{spec.label} is not installed ({spec.binaries[0]} not on PATH).",
            steps=steps,
        )

    # F3: in a wheel install the project anchor is site-packages, so a
    # project-scope registration would target the installed package instead
    # of a real checkout. Refuse with the actionable alternative; global
    # scope carries no anchor and keeps working everywhere.
    if effective_scope == "project" and _in_site_packages(_repo_root()):
        steps.append(f"project scope anchor is an installed package: {_repo_root()}")
        return ActionResult(
            spec.key,
            "enable",
            False,
            False,
            PROJECT_SCOPE_NEEDS_CHECKOUT,
            steps=steps,
        )

    argv = add_argv(spec, binary, effective_scope)
    if dry_run:
        steps.append(f"would run: {shlex.join(argv)}")
        return ActionResult(
            spec.key,
            "enable",
            True,
            False,
            f"dry run: would register `{SERVER_NAME}` for {spec.label} ({effective_scope} scope).",
            steps=steps,
            argv=[argv],
        )

    version = _probe_version(spec, binary)

    # Idempotency: a machina entry already in this scope's config file means
    # registration is done — refresh verification and state, mutate nothing.
    path = _config_files(spec).get(effective_scope)
    if path is not None and _registered_in_file(spec, path):
        verify = _verify_presence(spec, effective_scope)
        _record_state(
            spec, enabled=True, scope=effective_scope, verify=verify, binary=binary, version=version
        )
        steps.append(f"already registered in {path}; no changes made")
        return ActionResult(
            spec.key,
            "enable",
            verify.get("ok", True),
            False,
            f"`{SERVER_NAME}` is already registered for {spec.label} "
            f"({effective_scope} scope); no changes made.",
            steps=steps,
            verify=verify,
            argv=[argv],
        )

    steps.append(f"run: {shlex.join(argv)}")
    result = _run(
        argv, env=_child_env(spec), timeout=ACTION_TIMEOUT_SECONDS, stdin_text=spec.enable_stdin
    )
    if result.timed_out:
        steps.append("registration command timed out (killed)")
    else:
        steps.append(f"registration command exited {result.returncode}")
    tail = _bounded(_redact_output((result.output or "") + (result.stderr or "")), 800)
    if tail:
        steps.append(f"output: {tail}")

    verify = _verify_presence(spec, effective_scope)
    ok = bool(verify.get("ok")) and not result.timed_out
    _record_state(
        spec, enabled=ok, scope=effective_scope, verify=verify, binary=binary, version=version
    )
    message = (
        f"registered `{SERVER_NAME}` for {spec.label} ({effective_scope} scope) and verified it."
        if ok
        else f"registration of `{SERVER_NAME}` for {spec.label} did not verify: {verify.get('detail', '')}"
    )
    return ActionResult(
        spec.key, "enable", ok, True, message, steps=steps, verify=verify, argv=[argv]
    )


def disable(platform: str) -> ActionResult:
    """Remove the Machina registration where the CLI supports it.

    Platforms without a remove subcommand (OpenCode) return ``ok=False`` with
    the exact manual step; an already-absent entry is a successful no-op.
    """
    spec = resolve_platform(platform)
    binary = _which(spec)
    if not binary:
        return ActionResult(
            spec.key,
            "disable",
            False,
            False,
            f"{spec.label} is not installed ({spec.binaries[0]} not on PATH).",
        )
    version = _probe_version(spec, binary)
    probe = _probe_list(spec, binary)
    file_scopes = _file_scopes(spec)
    present = probe.found or bool(file_scopes)

    if not present:
        verify = _verify_absence(spec, "global" if "global" in spec.supports_scopes else "project")
        _record_state(
            spec,
            enabled=False,
            scope=spec.supports_scopes[0],
            verify=verify,
            binary=binary,
            version=version,
        )
        return ActionResult(
            spec.key,
            "disable",
            True,
            False,
            f"`{SERVER_NAME}` is not registered for {spec.label}; nothing to remove.",
            verify=verify,
        )

    argv = remove_argv(spec, binary)
    if argv is None:
        files = ", ".join(str(p) for p in _config_files(spec).values())
        return ActionResult(
            spec.key,
            "disable",
            False,
            False,
            f"{spec.label}'s CLI has no MCP remove subcommand; delete the "
            f"`{SERVER_NAME}` entry manually from: {files}",
            steps=[f"edit {files} and remove the `{SERVER_NAME}` entry"],
        )

    steps = [f"run: {shlex.join(argv)}"]
    result = _run(
        argv, env=_child_env(spec), timeout=ACTION_TIMEOUT_SECONDS, stdin_text=spec.disable_stdin
    )
    if result.timed_out:
        steps.append("remove command timed out (killed)")
    else:
        steps.append(f"remove command exited {result.returncode}")
    tail = _bounded(_redact_output((result.output or "") + (result.stderr or "")), 800)
    if tail:
        steps.append(f"output: {tail}")

    scope = "global" if "global" in spec.supports_scopes else "project"
    verify = _verify_absence(spec, scope)
    ok = bool(verify.get("ok")) and not result.timed_out
    if ok:
        _record_state(
            spec, enabled=False, scope=scope, verify=verify, binary=binary, version=version
        )
    message = (
        f"removed `{SERVER_NAME}` from {spec.label} and verified it is gone."
        if ok
        else f"removal of `{SERVER_NAME}` from {spec.label} did not verify: {verify.get('detail', '')}"
    )
    return ActionResult(
        spec.key, "disable", ok, True, message, steps=steps, verify=verify, argv=[argv]
    )


# ---------------------------------------------------------------------------
# Best-effort sweep for setup/installer flows: detect → choose → enable
# ---------------------------------------------------------------------------


def stdin_is_tty() -> bool:
    """True when stdin is an interactive terminal (safe to prompt on)."""
    try:
        return bool(sys.stdin.isatty())
    except (AttributeError, ValueError, OSError):
        return False


@dataclass
class EnableReport:
    """Outcome of one :func:`enable_detected` sweep.

    ``results`` holds one entry per *attempted* enable; ``skipped`` names the
    installed-but-unregistered harnesses left alone (not requested, declined,
    or non-interactive summary-only); ``notes`` is the human-readable trace
    of both. ``detected`` is the full per-platform detection map.
    """

    detected: dict[str, DetectResult]
    results: list[ActionResult] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def enabled(self) -> list[str]:
        """Platform keys registered and verified during this sweep."""
        return [result.platform for result in self.results if result.ok]

    @property
    def failed(self) -> list[str]:
        """Platform keys whose enable attempt did not verify."""
        return [result.platform for result in self.results if not result.ok]


def _detection_summary_line(result: DetectResult) -> str:
    """One plain-text summary line for a platform's detection state."""
    if not result.installed:
        return f"  {result.label}: not installed (`{result.platform}` not on PATH)"
    parts = [f"installed ({result.version})" if result.version else "installed"]
    parts.append("running" if result.running else "not running")
    if result.registered:
        scopes = "+".join(result.scopes)
        parts.append(f"registered ({scopes})" if scopes else "registered")
    else:
        parts.append("not registered")
    return f"  {result.label}: " + ", ".join(parts)


def _enable_later_hint(result: DetectResult, scope: str) -> str:
    return (
        f"  {result.label}: enable later with "
        f"`machina interop enable {result.platform} --scope {scope}`"
    )


def _prompt_enable(result: DetectResult, emit: Callable[[str], None]) -> bool:
    """Ask the one enable question; default yes, EOF/abort means no."""
    emit(f"Enable Machina in {result.label}? [Y/n]")
    try:
        answer = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in ("", "y", "yes")


def enable_detected(
    *,
    scope: str = "global",
    only: Iterable[str] | None = None,
    interactive: bool | Callable[[DetectResult], bool] | None = None,
    out: Callable[[str], None] | None = None,
) -> EnableReport:
    """Detect harnesses, then idempotently enable Machina for the chosen ones.

    Behavior matrix (``interactive``):
      * ``True`` or a callable — every installed-but-unregistered harness is
        prompted ("Enable Machina in <platform>? [Y/n]"); the callable
        receives its :class:`DetectResult` and returns the decision, and
        ``only`` is ignored.
      * ``False`` — only harnesses explicitly listed in ``only`` are enabled;
        ``only=None``/empty enables nothing (detection summary only, never a
        silent registration).
      * ``None`` — prompts when stdin is a TTY, otherwise behaves like
        ``interactive=False``.

    A detection summary line is printed for every platform through ``out``
    (default ``print``). Enable failures, detection failures, unknown ``only``
    keys, and callback errors are captured in the returned :class:`EnableReport`
    (and in ``notes``) — this function never raises for them.
    """
    emit = out if out is not None else print
    report = EnableReport(detected={})

    try:
        detection = detect()
    except Exception as error:  # detection must never abort a setup flow
        report.notes.append(f"detection failed: {error}")
        emit(f"Interop detection failed: {error}")
        return report
    report.detected = detection

    wanted: set[str] = set()
    keys = [only] if isinstance(only, str) else list(only or ())
    for key in keys:
        try:
            wanted.add(resolve_platform(str(key)).key)
        except (AttributeError, TypeError, UnknownPlatform):
            report.notes.append(f"ignoring unknown platform {key!r} in `only`")

    ask_callback = interactive if callable(interactive) else None
    prompting = interactive is True or ask_callback is not None
    if interactive is None:
        prompting = stdin_is_tty()

    emit("Interop harness detection:")
    candidates: list[DetectResult] = []
    for result in detection.values():
        emit(_detection_summary_line(result))
        if result.installed and not result.registered:
            candidates.append(result)

    if not candidates:
        emit("  nothing to register (no installed harness is missing Machina)")
        report.notes.append("no action needed: every installed harness is registered")
        return report

    planned: list[DetectResult] = []
    for result in candidates:
        approved = False
        if prompting:
            try:
                approved = (
                    bool(ask_callback(result))
                    if ask_callback is not None
                    else _prompt_enable(result, emit)
                )
            except Exception as error:  # a broken prompt is a no, never a crash
                report.notes.append(
                    f"{result.label}: prompt failed ({error}); treating as declined"
                )
        else:
            approved = result.platform in wanted
        if approved:
            planned.append(result)
            continue
        report.skipped.append(result.platform)
        report.notes.append(
            f"{result.label}: left unregistered; enable later with "
            f"`machina interop enable {result.platform} --scope {scope}`"
        )
        emit(_enable_later_hint(result, scope))

    for result in planned:
        emit(f"Enabling Machina for {result.label} (scope: {scope})...")
        try:
            action = enable(result.platform, scope=scope)
        except Exception as error:  # an enable bug must not kill setup/install
            action = ActionResult(
                result.platform,
                "enable",
                False,
                False,
                f"unexpected error while enabling: {error}",
            )
            report.notes.append(f"{result.label}: unexpected error: {error}")
        report.results.append(action)
        mark = "✓" if action.ok else "✗"
        emit(f"  {mark} {action.message}")
    return report
