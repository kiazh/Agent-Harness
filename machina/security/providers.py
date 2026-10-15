"""User-added named providers — a small persistent registry with atomic writes.

Storage decision (Phase B): a dedicated 0600 JSON file at
``~/.machina/providers.json`` (override: ``MACHINA_PROVIDERS_FILE``) holds the
non-secret metadata — name, base URL, the derived environment key name, and
the last pulled model catalog with its fetch time. Keys follow the repo's
secret idiom instead of living here: the key is written through
:mod:`machina.security.env_file` to the same git-ignored ``.env`` the ``/keys``
menu manages, under a name derived from the provider
(``MACHINA_PROVIDER_<NAME>_API_KEY``), and resolved through
:func:`machina.security.secrets.get_secret` (env -> ``*_FILE`` mount ->
Vault/AWS backend). A keyless local endpoint (127.0.0.1/::1/localhost) is a
supported mode, exactly like the generic ``custom`` provider.

Writes are atomic (mkstemp + fsync + os.replace) and always land 0600, the
same discipline as the ChatGPT token store; the parent directory is created
0700. Names are validated lowercase so they compose into env keys, and
built-in provider names are reserved (see :data:`RESERVED_NAMES`).

Plaintext ``http://`` base URLs are a loopback-only default: a non-loopback
http endpoint would carry bearer keys in cleartext, so it is refused unless
the operator explicitly opts in with ``MACHINA_ALLOW_PLAINTEXT_HTTP=1``
(logged as a warning), both when storing and when fetching models. Store
mutations are serialized by a bounded per-store ``flock`` that raises
:class:`ProviderStoreLockTimeout` instead of blocking forever.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from machina.observability.diagnostics import record_failure
from machina.security import store_fence
from machina.security.env_file import (
    find_env_file,
    read_env_values,
    remove_env_values,
    set_env_values,
)
from machina.security.secrets import get_secret, invalidate_secret_cache

__all__ = [
    "RESERVED_NAMES",
    "ProviderStoreError",
    "ProviderStoreLockTimeout",
    "add_provider",
    "api_key_env",
    "fetch_models",
    "get_provider_entry",
    "is_loopback_base_url",
    "is_named_provider",
    "load_store",
    "providers_path",
    "provider_names",
    "pulled_at_age_seconds",
    "remove_provider",
    "resolve_provider_key",
    "save_models",
    "validate_name",
]

logger = logging.getLogger(__name__)

#: Explicit operator opt-in that permits non-loopback plaintext http
#: endpoints (bearer keys then travel in cleartext).
_PLAINTEXT_HTTP_ENV = "MACHINA_ALLOW_PLAINTEXT_HTTP"

#: The lock is delegated to :func:`machina.security.store_fence.store_lock`, so
#: the bound is the shared ``store_fence.STORE_LOCK_TIMEOUT_SECONDS``. The alias
#: keeps the provider-store error message self-describing.
_STORE_LOCK_TIMEOUT_SECONDS = store_fence.STORE_LOCK_TIMEOUT_SECONDS


@contextmanager
def _store_lock() -> Iterator[None]:
    """Exclusive per-store lock delegated to :func:`store_fence.store_lock`.

    One authoritative bounded-flock implementation (code_review P2-2): the
    mechanics live in ``machina.security.store_fence``; this wrapper only maps
    the shared :class:`store_fence.StoreLockTimeout` onto the provider store's
    typed :class:`ProviderStoreLockTimeout` so callers keep the same error
    surface. It serializes whole load -> mutate -> atomic-replace sections
    across threads and processes, so concurrent store writers cannot drop one
    side's update (review round 6, R6-2). The lock file holds no state and the
    atomic replace keeps the store itself always readable.
    """
    try:
        with store_fence.store_lock(providers_path()):
            yield
    except store_fence.StoreLockTimeout as exc:
        raise ProviderStoreLockTimeout(
            "provider store lock timed out after "
            f"{_STORE_LOCK_TIMEOUT_SECONDS:g}s; another process holds it"
        ) from exc


_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_BASE_URL_MAX = 500
_KEY_ENV_MAX = 64
_MODELS_MAX = 2000
_MODEL_ID_MAX = 200

#: Built-in provider names a user entry must not shadow (the registry check
#: in ``core.provider.specs`` prefers built-ins, but a duplicate name would
#: still be confusing in listings). Pinned against PROVIDER_SPECS by tests.
RESERVED_NAMES: frozenset[str] = frozenset(
    {
        "openrouter",
        "openai",
        "anthropic",
        "google",
        "mistral",
        "groq",
        "together",
        "deepseek",
        "xai",
        "opencode",
        "kimi",
        "chatgpt",
        "copilot",
        "custom",
        "ollama",
    }
)


class ProviderStoreError(ValueError):
    """Invalid provider entry, name, or store content."""


class ProviderStoreLockTimeout(ProviderStoreError):
    """The provider-store lock could not be acquired within its bound."""


def providers_path() -> Path:
    """Resolve the store path: ``MACHINA_PROVIDERS_FILE`` or ``~/.machina``."""
    override = os.environ.get("MACHINA_PROVIDERS_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".machina" / "providers.json"


def api_key_env(name: str) -> str:
    """Derived environment key for a provider name (``foo-bar`` -> caps)."""
    return f"MACHINA_PROVIDER_{name.upper().replace('-', '_')}_API_KEY"


def validate_name(name: str) -> str:
    """Validate and normalize a provider name; raise ProviderStoreError."""
    normalized = str(name or "").strip().lower()
    if not _NAME_RE.match(normalized):
        raise ProviderStoreError(
            "provider name must start with a letter and use lowercase "
            "letters, digits, and dashes (max 32 characters)"
        )
    if normalized in RESERVED_NAMES:
        raise ProviderStoreError(f"provider name {normalized!r} is reserved by a built-in provider")
    return normalized


def _require_plaintext_allowed(raw: str) -> None:
    """Refuse non-loopback plaintext ``http://`` unless explicitly opted in.

    A bearer key sent over plaintext http is readable by any network peer;
    loopback endpoints are the only exempt targets. Applied both when a base
    URL is stored (:func:`_validate_base_url`) and when a bearer is about to
    leave the process (:func:`fetch_models`), so a hand-edited or pre-fix
    store entry cannot leak its key either. ``MACHINA_ALLOW_PLAINTEXT_HTTP=1``
    is the explicit operator opt-in and logs a visible warning each time it
    applies.
    """
    try:
        parsed = urlparse(str(raw or ""))
    except ValueError:
        return  # malformed shapes are handled by the caller's own checks
    if parsed.scheme != "http" or is_loopback_base_url(raw):
        return
    if os.environ.get(_PLAINTEXT_HTTP_ENV) == "1":
        logger.warning(
            "plaintext http to non-loopback host %s: bearer API keys travel in cleartext (%s=1)",
            parsed.hostname or "",
            _PLAINTEXT_HTTP_ENV,
        )
        return
    raise ProviderStoreError(
        f"plaintext http:// to {parsed.hostname or 'the host'} is only allowed for loopback "
        f"endpoints (127.0.0.1, localhost, ::1); use https:// or set {_PLAINTEXT_HTTP_ENV}=1 "
        "to send bearer keys in cleartext"
    )


def _validate_base_url(base_url: str) -> str:
    raw = str(base_url or "").strip().rstrip("/")
    if not raw or len(raw) > _BASE_URL_MAX:
        raise ProviderStoreError("base URL is required (max 500 characters)")
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ProviderStoreError("base URL must be an http(s) URL with a host")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ProviderStoreError("base URL must not contain credentials, a query, or a fragment")
    _require_plaintext_allowed(raw)
    return raw


def is_loopback_base_url(base_url: str) -> bool:
    """True when the endpoint host is loopback (keyless-local mode)."""
    try:
        host = (urlparse(str(base_url)).hostname or "").lower()
    except ValueError:
        return False
    # String comparison against known loopback spellings (never a bind call):
    return host in ("127.0.0.1", "localhost", "::1", "0.0.0.0")  # nosec B104 # loopback comparison, not a bind


def _empty_store() -> dict[str, Any]:
    return {"version": 1, "providers": {}}


def load_store() -> dict[str, Any]:
    """Read the store; a missing/corrupt file reads as empty (never raises).

    Corrupt content is reported through the diagnostics boundary so an
    operator can see it, but reads must not take the runtime down.
    """
    path = providers_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _empty_store()
    except (OSError, json.JSONDecodeError) as exc:
        record_failure("security.providers.load", exc)
        return _empty_store()
    if not isinstance(raw, dict) or not isinstance(raw.get("providers"), dict):
        return _empty_store()
    return {"version": int(raw.get("version") or 1), "providers": raw["providers"]}


def _save_store(store: dict[str, Any]) -> Path:
    """Atomic 0600 write (mkstemp + fsync + replace), parent dir 0700."""
    path = providers_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        store_fence.chmod_temp_file(fd, tmp_name, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(store, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        os.chmod(path, 0o600)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp_name)
        raise
    return path


def _entry_or_none(store: dict[str, Any], name: str) -> dict[str, Any] | None:
    entry = store["providers"].get(str(name or "").strip().lower())
    return entry if isinstance(entry, dict) else None


def get_provider_entry(name: str) -> dict[str, Any] | None:
    """One stored provider (copy), or None when absent."""
    entry = _entry_or_none(load_store(), name)
    return dict(entry) if entry is not None else None


def provider_names() -> tuple[str, ...]:
    """Sorted names of user-added providers."""
    return tuple(sorted(load_store()["providers"]))


def is_named_provider(name: str) -> bool:
    return str(name or "").strip().lower() in load_store()["providers"]


def resolve_provider_key(entry: dict[str, Any] | str) -> str:
    """Resolve the provider's key through the shared secret idiom.

    Accepts the store entry or its env name. Environment first, then a
    ``<NAME>_FILE`` mount, then the configured external backend — the same
    resolver every other credential uses.
    """
    env_name = entry if isinstance(entry, str) else str(entry.get("api_key_env") or "")
    if not env_name or len(env_name) > _KEY_ENV_MAX:
        return ""
    with suppress(Exception):
        return str(get_secret(env_name) or "")
    return ""


def add_provider(
    name: str,
    base_url: str,
    *,
    api_key: str | None = None,
    persist_key: bool = True,
    env_file: Path | None = None,
) -> dict[str, Any]:
    """Create/update a named provider; optionally store its key in ``.env``.

    Returns the stored entry. The key never enters the JSON store: it is
    written through :func:`machina.security.env_file.set_env_values` (0600,
    atomic) and applied to the live process immediately.
    """
    normalized = validate_name(name)
    url = _validate_base_url(base_url)
    with _store_lock():
        store = load_store()
        previous = _entry_or_none(store, normalized) or {}
        entry: dict[str, Any] = {
            "base_url": url,
            "api_key_env": api_key_env(normalized),
            "models": previous.get("models") if isinstance(previous.get("models"), list) else [],
            "models_fetched_at": previous.get("models_fetched_at"),
            "default_model": previous.get("default_model"),
            "created_at": previous.get("created_at")
            or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        if api_key:
            key = str(api_key).strip()
            if not key or len(key) > 2000 or any(ch in key for ch in ("\r", "\n", "\x00")):
                raise ProviderStoreError("api key must be a single line of 1-2000 characters")
            if persist_key:
                set_env_values({entry["api_key_env"]: key}, path=env_file)
            os.environ[entry["api_key_env"]] = key
            invalidate_secret_cache()
        store["providers"][normalized] = entry
        _save_store(store)
    return dict(entry)


def remove_provider(name: str) -> dict[str, Any] | None:
    """Remove a provider and every trace it left behind.

    Deleting the store entry also drops the cached model list and the
    dynamic registry membership (``PROVIDERS`` consults the store live).
    The derived ``MACHINA_PROVIDER_<NAME>_API_KEY`` env name is resolved and
    its line is *deleted* from ``.env`` (not blanked, unlike
    ``secrets.clear``) so removal leaves no trace; the live process variable
    and the secret cache are cleared too.

    Returns a cleanup report, or ``None`` when the provider was not
    registered (idempotent: a second removal cleans nothing and says so by
    reporting ``None``). The report stays honest when a trace was already
    gone (e.g. ``envLineRemoved`` is False when ``.env`` had no line).

    The store entry removal AND the credential cleanup (``.env`` line,
    process environment, secret cache) share one store-lock hold: a
    concurrent re-add of the same provider blocks until the removal is fully
    done (or completes first, in which case this removal takes the fresh
    entry down as the newest operation). Either serial order is safe; what
    cannot happen is the old removal deleting a replacement credential while
    the provider stays registered.
    """
    normalized = str(name or "").strip().lower()
    with _store_lock():
        store = load_store()
        entry = _entry_or_none(store, normalized)
        if entry is None:
            return None
        env_name = str(entry.get("api_key_env") or "")
        cached_models = (
            len(entry.get("models") or []) if isinstance(entry.get("models"), list) else 0
        )
        del store["providers"][normalized]
        _save_store(store)
        env_line_removed = False
        env_var_removed = False
        if env_name:
            try:
                if find_env_file().is_file() and env_name in read_env_values():
                    env_line_removed = bool(remove_env_values([env_name]))
            except (OSError, ValueError) as exc:
                # Best-effort, but never silent: the report says the line survived.
                record_failure("security.providers.remove_env", exc)
            env_var_removed = os.environ.pop(env_name, None) is not None
            invalidate_secret_cache()
    return {
        "provider": normalized,
        "storeEntry": True,
        "apiKeyEnv": env_name or None,
        "envLineRemoved": env_line_removed,
        "envVarRemoved": env_var_removed,
        "cachedModels": cached_models,
        "registryEntry": True,
    }


def _normalize_models(models: Any) -> list[dict[str, Any]]:
    """Keep only ``{id, contextWindow, maxContextWindow}`` rows, bounded."""
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    if not isinstance(models, list):
        return normalized
    for item in models[: _MODELS_MAX * 2]:
        identifier = ""
        context: int | None = None
        max_context: int | None = None
        if isinstance(item, dict):
            identifier = str(item.get("id") or item.get("model") or "").strip()
            for key in ("contextLength", "context_length", "context_window", "max_model_len"):
                value = item.get(key)
                if type(value) is int and 0 < value < 2**31:
                    context = value
                    break
            value = item.get("maxContextWindow") or item.get("max_context_window")
            if type(value) is int and 0 < value < 2**31:
                max_context = value
        elif isinstance(item, str):
            identifier = item.strip()
        if not identifier or len(identifier) > _MODEL_ID_MAX or identifier in seen:
            continue
        seen.add(identifier)
        row: dict[str, Any] = {"id": identifier}
        if context:
            row["contextWindow"] = context
        if max_context:
            row["maxContextWindow"] = max_context
        normalized.append(row)
        if len(normalized) >= _MODELS_MAX:
            break
    return normalized


def save_models(name: str, models: Any) -> list[dict[str, Any]]:
    """Persist a pulled model catalog (bounded) for one provider."""
    normalized = str(name or "").strip().lower()
    with _store_lock():
        store = load_store()
        entry = _entry_or_none(store, normalized)
        if entry is None:
            raise ProviderStoreError(f"provider {normalized!r} is not registered")
        rows = _normalize_models(models)
        entry["models"] = rows
        entry["models_fetched_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        if rows and not entry.get("default_model"):
            entry["default_model"] = rows[0]["id"]
        _save_store(store)
    return rows


def fetch_models(
    base_url: str,
    *,
    api_key: str = "",
    timeout: float = 10.0,
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    """GET ``{base_url}/models`` (OpenAI-compatible) and normalize the rows.

    Honest failure: non-200 and transport errors raise ``ProviderStoreError``
    with a reason suitable for the UI; the key is never included in any
    message. Keyless endpoints simply get no Authorization header. Plaintext
    ``http://`` to a non-loopback host is refused unless
    ``MACHINA_ALLOW_PLAINTEXT_HTTP=1`` is set (see
    :func:`_require_plaintext_allowed`).
    """
    _require_plaintext_allowed(str(base_url or ""))
    url = str(base_url or "").strip().rstrip("/") + "/models"
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    owned = client is None
    http = client or httpx.Client(timeout=httpx.Timeout(timeout, connect=min(5.0, timeout)))
    try:
        response = http.get(url, headers=headers)
    except Exception as exc:
        raise ProviderStoreError(f"could not reach {base_url} ({type(exc).__name__})") from exc
    finally:
        if owned:
            http.close()
    if response.status_code != 200:
        raise ProviderStoreError(
            f"model list request returned HTTP {response.status_code} from {base_url}"
        )
    try:
        payload = response.json()
    except Exception as exc:
        raise ProviderStoreError("model list response was not JSON") from exc
    rows: Any = None
    if isinstance(payload, dict):
        rows = payload.get("data")
        if rows is None:
            rows = payload.get("models")
    elif isinstance(payload, list):
        rows = payload
    if not isinstance(rows, list):
        raise ProviderStoreError("model list response had no 'data' array")
    models: list[dict[str, Any]] = []
    for item in rows:
        models.append(item if isinstance(item, dict) else {"id": str(item)})
    return _normalize_models(models)


def pulled_at_age_seconds(entry: dict[str, Any]) -> float | None:
    """Age of the cached model list in seconds (None when never pulled)."""
    raw = str(entry.get("models_fetched_at") or "").strip()
    if not raw:
        return None
    try:
        fetched = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None
    return max(0.0, time.time() - fetched.timestamp())
