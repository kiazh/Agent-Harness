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
        return Path(override).expanduser()
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
        text = target.read_text(encoding="utf-8")
    except OSError:
        return values
    for line in text.splitlines():
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
    if value == "":
        return ""
    if re.search(r"""[\s#"']""", value):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
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
    target = path or find_env_file()
    normalized = {_validate_key(k): v for k, v in updates.items()}
    if not normalized:
        return target
    lines: list[str] = []
    if target.is_file():
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
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def is_set(key: str) -> bool:
    """True when *key* has a non-blank value in the process environment."""
    return bool(os.environ.get(_validate_key(key), "").strip())
