"""Read and update the project ``.env`` file (git-ignored local secrets).

Secrets live in ``.env`` / the environment, never in ``config.yaml``.
This helper preserves comments, blank lines, and unrelated entries while
updating or appending ``KEY=value`` lines. Values with spaces, ``#`` or
quotes are written double-quoted.
"""

from __future__ import annotations

import os
import re
import stat
import tempfile
from pathlib import Path

from machina.observability.diagnostics import record_failure

__all__ = [
    "PROVIDER_KEYS",
    "find_env_file",
    "read_env_values",
    "remove_env_values",
    "set_env_values",
]

# Curated provider keys offered by the `/keys` menu and `machina setup`.
# Users may store any well-formed KEY (see _validate_key); this list is
# just the menu suggestions — one entry per model family they use.
PROVIDER_KEYS: tuple[tuple[str, str], ...] = (
    ("OPENROUTER_API_KEY", "OpenRouter (cloud models, default provider)"),
    ("OPENAI_API_KEY", "OpenAI (GPT models, embeddings)"),
    ("ANTHROPIC_API_KEY", "Anthropic (Claude models)"),
    ("GOOGLE_API_KEY", "Google (Gemini models)"),
    ("MISTRAL_API_KEY", "Mistral models"),
    ("GROQ_API_KEY", "Groq (fast inference)"),
    ("TOGETHER_API_KEY", "Together AI (open models)"),
    ("DEEPSEEK_API_KEY", "DeepSeek models"),
    ("XAI_API_KEY", "xAI (Grok models)"),
    ("OPENCODE_API_KEY", "OpenCode Zen (free + paid models gateway)"),
    ("KIMI_API_KEY", "Kimi Code (K3 coding models)"),
    ("CUSTOM_API_KEY", "Custom OpenAI-compatible endpoint"),
    ("COHERE_API_KEY", "Cohere (reranking)"),
)

_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
# Case-insensitive matcher for reading/updating pre-existing lines, whose
# spellings may predate the canonical uppercase form (R4-046). Writes always
# normalize through _validate_key/_KEY_RE, so new lines stay uppercase.
_KEY_RE_CI = re.compile(r"^[A-Za-z][A-Za-z0-9_]{1,63}$")

_ENV_MAX_BYTES = 256 * 1024
_LINE_MAX_BYTES = 4096


def _checkout_root() -> Path | None:
    """The source-checkout root two levels above this file, or None.

    In a source checkout ``parents[2]`` is the repo root; inside an installed
    wheel it is the interpreter's ``site-packages``. The repo-marker check
    keeps both the ``find_env_file`` fallback and the allowed-roots list from
    ever targeting the package directory (R4-048).
    """
    root = Path(__file__).resolve().parents[2]
    if (root / "pyproject.toml").is_file() or (root / ".git").exists():
        return root
    return None


def _allowed_roots() -> tuple[Path, ...]:
    cwd = Path.cwd().resolve()
    roots: list[Path] = [cwd]
    repo = _checkout_root()
    if repo is not None:
        roots.append(repo)
    # Home and temp so tests (pytest tmp_path) and user ~/.env keep working.
    try:
        roots.append(Path.home().resolve())
    except Exception as _boundary_error:
        # Optional fallback preserves the primary outcome; report no payload.
        record_failure("env_file._allowed_roots", _boundary_error)
    try:
        roots.append(Path(tempfile.gettempdir()).resolve())
    except Exception as _boundary_error:
        # Optional fallback preserves the primary outcome; report no payload.
        record_failure("env_file._allowed_roots", _boundary_error)
    # MACHINA_ENV_FILE explicit override parent is allowed.
    override = os.environ.get("MACHINA_ENV_FILE", "").strip()
    if override:
        try:
            p = Path(override).expanduser()
            if not p.is_absolute():
                p = (cwd / p).resolve()
            roots.append(p.parent.resolve() if p.suffix else p.resolve())
        except Exception as _boundary_error:
            # Optional fallback preserves the primary outcome; report no payload.
            record_failure("env_file._allowed_roots", _boundary_error)
    return tuple(roots)


def _is_inside_allowed(target: Path) -> bool:
    try:
        resolved = target.resolve() if target.is_absolute() else (Path.cwd() / target).resolve()
    except OSError:
        return False
    for root in _allowed_roots():
        try:
            if resolved.is_relative_to(root):
                return True
        except Exception:
            continue
    return False


def _ensure_inside_allowed(target: Path) -> Path:
    resolved = target.resolve() if target.is_absolute() else (Path.cwd() / target).resolve()
    if not _is_inside_allowed(resolved):
        raise ValueError(f".env path escapes allowed roots: {target}")
    return resolved


def _validate_key(key: str) -> str:
    """Normalize and validate an env key; raise ValueError if malformed."""
    normalized = (key or "").strip().upper()
    if not _KEY_RE.match(normalized):
        raise ValueError(f"invalid key {key!r}: use CAPS_WITH_UNDERSCORES, e.g. OPENROUTER_API_KEY")
    return normalized


def _normalize_key_token(raw: str) -> str:
    """Upper-case a dotenv key token; '' when it is not a well-formed KEY."""
    token = raw.strip()
    if token.startswith("export ") or token.startswith("export\t"):
        token = token[6:].lstrip()
    return token.upper() if _KEY_RE_CI.match(token) else ""


def find_env_file(start: Path | None = None) -> Path:
    """Locate the ``.env`` file: ``$MACHINA_ENV_FILE``, else nearest ``.env`` upward.

    Falls back to ``<checkout-root>/.env`` when nothing is found, so callers
    always have a writable default. An installed wheel (no repo markers) has
    no checkout root and falls back to the walk-up origin (``<cwd>/.env``)
    instead — never ``site-packages`` (R4-048).
    """
    override = os.environ.get("MACHINA_ENV_FILE", "").strip()
    if override:
        p = Path(override).expanduser()
        if not p.is_absolute():
            p = (Path.cwd() / p).resolve()
        else:
            p = Path(os.path.abspath(p))
        if p.name != ".env" and not p.name.endswith(".env") and not p.is_file():
            raise ValueError(
                f"MACHINA_ENV_FILE must end with .env or be an existing file: {override!r}"
            )
        return p.resolve() if p.exists() else p
    here = (start or Path.cwd()).resolve()
    for candidate in (here, *here.parents):
        env = candidate / ".env"
        if env.is_file():
            return env
    repo = _checkout_root()
    if repo is not None:
        return repo / ".env"
    return here / ".env"


def _tighten_existing_mode(target: Path) -> None:
    """Best-effort 0600 repair for a legacy world/group-readable ``.env``.

    Only a regular file owned by the current user is touched; symlinks and
    other owners are left alone. A chmod failure never fails the read (the
    caller still gets its values) but is reported, not swallowed silently.
    """
    if os.name != "posix" or not hasattr(os, "chmod"):
        return
    try:
        info = os.lstat(target)
    except OSError:
        return
    if not stat.S_ISREG(info.st_mode):
        return
    if info.st_uid != os.geteuid():
        return
    if info.st_mode & 0o077:
        try:
            os.chmod(target, 0o600)
        except OSError as _boundary_error:
            record_failure("env_file.tighten_mode", _boundary_error)


def read_env_values(path: Path | None = None) -> dict[str, str]:
    """Read persisted dotenv assignments without resolving interpolation.

    Management callers use these raw parsed values to preserve templates and
    detect/remove assignments, including empty ones. Runtime effective values
    come from Config.get/load_dotenv; the installer env_get likewise resolves
    interpolation with existing process environment taking precedence.
    """
    target = path or find_env_file()
    values: dict[str, str] = {}
    _tighten_existing_mode(target)
    try:
        if target.is_file() and target.stat().st_size > _ENV_MAX_BYTES:
            raise ValueError(f".env file exceeds {_ENV_MAX_BYTES} bytes")
        text = target.read_text(encoding="utf-8")
        if len(text.encode("utf-8")) > _ENV_MAX_BYTES:
            text = text.encode("utf-8")[:_ENV_MAX_BYTES].decode("utf-8", errors="ignore")
    except OSError:
        return values
    except ValueError:
        return values
    from io import StringIO

    from dotenv import dotenv_values

    # Share runtime dotenv syntax, including export, quoting and comments.
    for raw_key, value in dotenv_values(stream=StringIO(text), interpolate=False).items():
        key = _normalize_key_token(raw_key)
        if key and value is not None and len(value.encode("utf-8")) <= _LINE_MAX_BYTES:
            values[key] = value
    return values


def _unescape(value: str) -> str:
    """Decode the ``\\\\`` and ``\\"`` escapes emitted by :func:`_quote`."""
    if "\\" not in value:
        return value
    out: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value) and value[index + 1] in ("\\", '"'):
            out.append(value[index + 1])
            index += 2
        else:
            out.append(char)
            index += 1
    return "".join(out)


def _quote(value: str) -> str:
    """Quote a value only when it needs it (spaces, #, quotes)."""
    if "\n" in value or "\r" in value:
        raise ValueError("env value must not contain newlines")
    if value == "":
        return ""
    if re.search(r"""[\s#"']""", value):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        escaped = escaped.replace("\n", "\\n").replace("\r", "\\r")
        return f'"{escaped}"'
    return value


def _env_line_key(line: str) -> str:
    """The upper-cased KEY of a dotenv line, or '' for comments/blank/non-assignments.

    Case-insensitive (R4-046) so a pre-existing ``openrouter_api_key=...``
    line is updated/removed in place; writers emit the canonical uppercase
    spelling.
    """
    from io import StringIO

    from dotenv.parser import parse_stream

    # Use the same parsed identity for quoted/exported/multiline assignments
    # as the reader and installer; raw partitioning misses quoted keys.
    for binding in parse_stream(StringIO(line)):
        if binding.key is not None:
            return _normalize_key_token(binding.key)
    return ""


def _env_bindings(text: str) -> list[str]:
    """Keep multiline dotenv assignments together during updates/removal."""
    from io import StringIO

    from dotenv.parser import parse_stream

    return [binding.original.string.rstrip("\r\n") for binding in parse_stream(StringIO(text))]


def set_env_values(
    updates: dict[str, str],
    path: Path | None = None,
    create: bool = True,
) -> Path:
    """Update ``KEY=value`` lines in place, appending missing keys.

    Returns the file path written. Raises ValueError on bad keys,
    OSError on I/O failure. Existing comments and ordering are kept.
    """
    explicit = path is not None
    target = path or find_env_file()
    if not explicit:
        _ensure_inside_allowed(target)
    normalized = {_validate_key(k): v for k, v in updates.items()}
    # Validate values early so newline injection fails before any I/O.
    for v in normalized.values():
        _quote(v)
    if not normalized:
        return target
    lines: list[str] = []
    if target.is_file():
        if target.stat().st_size > _ENV_MAX_BYTES:
            raise ValueError(f".env file exceeds {_ENV_MAX_BYTES} bytes")
        lines = _env_bindings(target.read_text(encoding="utf-8"))
    elif not create:
        raise OSError(f".env not found: {target}")
    seen: set[str] = set()
    for i, line in enumerate(lines):
        key = _env_line_key(line)
        if key in normalized:
            lines[i] = f"{key}={_quote(normalized[key])}"
            seen.add(key)
    missing = [k for k in normalized if k not in seen]
    if missing:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(f"{k}={_quote(normalized[k])}" for k in missing)
    if not target.is_file() and not create:
        raise OSError(f".env not found: {target}")
    parent = (
        target.parent.resolve()
        if target.parent.exists()
        else (Path.cwd() / target.parent).resolve()
    )
    if not explicit and not _is_inside_allowed(parent):
        raise ValueError(f".env parent escapes allowed roots: {target}")
    return _write_env_lines(target, lines)


def _fsync_directory(directory: Path) -> None:
    """Best-effort directory fsync so the replace survives a crash.

    Some platforms do not support fsync on a directory fd (macOS raises
    EINVAL) or refuse to open one (Windows); the rename itself already
    happened, so only the durability hint is lost — never the data.
    """
    try:
        dir_fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        try:
            os.close(dir_fd)
        except OSError:
            pass


def _write_env_lines(target: Path, lines: list[str]) -> Path:
    """Atomically replace *target* with ``lines`` at 0600 (shared writer).

    Same-write discipline for set_env_values and remove_env_values:
    ``tempfile.mkstemp`` in the target directory (unpredictable name created
    O_EXCL, so a pre-planted symlink or file is never followed or reused;
    R4-044 review), 0600, write, fsync the file, os.replace on the same
    filesystem, then fsync the directory so the rename is durable. 0600 comes
    from mkstemp + fchmod, so the replace can never leave a world-readable
    ``.env`` behind (and repairs a legacy 0644 file). No flock: any lock
    would be released before the replace, so it would protect nothing
    (review 2026-10-14); concurrent writers are last-writer-wins by design.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    data = "\n".join(lines) + "\n"
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    tmp = Path(tmp_name)
    try:
        if hasattr(os, "fchmod"):  # not available on Windows
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
            fd = -1  # fdopen owns the descriptor from here on
            tmp_file.write(data)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.replace(tmp, target)
        _fsync_directory(target.parent)
    except BaseException:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return target


def remove_env_values(keys: list[str] | tuple[str, ...], path: Path | None = None) -> list[str]:
    """Delete ``KEY=...`` lines from a dotenv file; returns the removed keys.

    The provider-removal counterpart of :func:`set_env_values`: unlike
    ``secrets.clear`` (which blanks the value), removal deletes the line so
    no trace remains. Comments, blank lines, and unrelated entries survive;
    a missing file or absent keys is a no-op. Writes follow the same atomic
    0600 discipline as :func:`set_env_values`.
    """
    normalized = {_validate_key(k) for k in keys}
    if not normalized:
        return []
    explicit = path is not None
    target = path or find_env_file()
    if not explicit:
        _ensure_inside_allowed(target)
    if not target.is_file():
        return []
    if target.stat().st_size > _ENV_MAX_BYTES:
        raise ValueError(f".env file exceeds {_ENV_MAX_BYTES} bytes")
    removed: set[str] = set()
    kept: list[str] = []
    for line in _env_bindings(target.read_text(encoding="utf-8")):
        key = _env_line_key(line)
        if key in normalized:
            removed.add(key)
            continue
        kept.append(line)
    if not removed:
        return []
    parent = (
        target.parent.resolve()
        if target.parent.exists()
        else (Path.cwd() / target.parent).resolve()
    )
    if not explicit and not _is_inside_allowed(parent):
        raise ValueError(f".env parent escapes allowed roots: {target}")
    _write_env_lines(target, kept)
    return sorted(removed)
