"""Read and update the project ``.env`` file (git-ignored local secrets).

Secrets live in ``.env`` / the environment, never in ``config.yaml``.
This helper preserves comments, blank lines, and unrelated entries while
updating or appending ``KEY=value`` lines. Values with spaces, ``#`` or
quotes are written double-quoted.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

__all__ = [
    "PROVIDER_KEYS",
    "find_env_file",
    "read_env_values",
    "set_env_values",
    "is_set",
]

# Curated provider keys offered by the `/keys` menu and `ah setup`.
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
    ("COHERE_API_KEY", "Cohere (reranking)"),
)

_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")

_ENV_MAX_BYTES = 256 * 1024
_LINE_MAX_BYTES = 4096


def _allowed_roots() -> tuple[Path, ...]:
    import tempfile

    cwd = Path.cwd().resolve()
    repo = Path(__file__).resolve().parents[2]
    roots: list[Path] = [cwd, repo]
    # Home and temp so tests (pytest tmp_path) and user ~/.env keep working.
    try:
        roots.append(Path.home().resolve())
    except Exception:
        pass
    try:
        roots.append(Path(tempfile.gettempdir()).resolve())
    except Exception:
        pass
    # AH_ENV_FILE explicit override parent is allowed.
    override = os.environ.get("AH_ENV_FILE", "").strip()
    if override:
        try:
            p = Path(override).expanduser()
            if not p.is_absolute():
                p = (cwd / p).resolve()
            roots.append(p.parent.resolve() if p.suffix else p.resolve())
        except Exception:
            pass
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


def find_env_file(start: Path | None = None) -> Path:
    """Locate the ``.env`` file: ``$AH_ENV_FILE``, else nearest ``.env`` upward.

    Falls back to ``<repo>/.env`` (two levels above this file) when nothing
    is found, so callers always have a writable default.
    """
    override = os.environ.get("AH_ENV_FILE", "").strip()
    if override:
        p = Path(override).expanduser()
        if not p.is_absolute():
            p = (Path.cwd() / p).resolve()
        else:
            p = Path(os.path.abspath(p))
        if p.name != ".env" and not p.name.endswith(".env") and not p.is_file():
            raise ValueError(f"AH_ENV_FILE must end with .env or be an existing file: {override!r}")
        return p.resolve() if p.exists() else p
    here = (start or Path.cwd()).resolve()
    for candidate in (here, *here.parents):
        env = candidate / ".env"
        if env.is_file():
            return env
    return Path(__file__).resolve().parents[2] / ".env"


def read_env_values(path: Path | None = None) -> dict[str, str]:
    """Parse ``KEY=value`` pairs from a dotenv file (no interpolation)."""
    target = path or find_env_file()
    values: dict[str, str] = {}
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
    for line in text.splitlines():
        if len(line.encode("utf-8")) > _LINE_MAX_BYTES:
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        raw_key, _, raw_value = stripped.partition("=")
        key = raw_key.strip()
        if not _KEY_RE.match(key):
            continue
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key] = value
    return values


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
        lines = target.read_text(encoding="utf-8").splitlines()
    elif not create:
        raise OSError(f".env not found: {target}")
    seen: set[str] = set()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.partition("=")[0].strip()
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
    parent = target.parent.resolve() if target.parent.exists() else (Path.cwd() / target.parent).resolve()
    if not explicit and not _is_inside_allowed(parent):
        raise ValueError(f".env parent escapes allowed roots: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    data = "\n".join(lines) + "\n"
    tmp = target.parent / (target.name + f".tmp.{os.getpid()}")
    tmp.write_text(data, encoding="utf-8")
    try:
        try:
            import fcntl  # type: ignore

            with open(tmp, "rb") as _f:
                try:
                    fcntl.flock(_f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    pass
        except ImportError:
            pass
        os.replace(tmp, target)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
    return target


def is_set(key: str) -> bool:
    """True when *key* has a non-blank value in the process environment."""
    try:
        return bool(os.environ.get(_validate_key(key), "").strip())
    except ValueError:
        return False
