"""Secrets feature handlers — runtime API-key management from the UI menu.

Secrets are never returned in full and never written to ``config.yaml``.
``secrets.set`` updates the live process environment immediately (so the
next turn can use the key) and, by default, persists to the git-ignored
``.env`` file. ``persist=false`` keeps the value for this session only.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

from ah.gateway.errors import INVALID_PARAMS, RpcError
from ah.security.env_file import PROVIDER_KEYS, find_env_file, read_env_values

if TYPE_CHECKING:
    from ah.gateway.server import Gateway

logger = logging.getLogger(__name__)

# Extra non-KEY settings the menu may manage (kept out of SECRET_KEYS).
MANAGED_ENV_KEYS: tuple[tuple[str, str], ...] = (
    ("DATABASE_URL", "PostgreSQL connection string"),
    ("SEARXNG_URL", "Self-hosted SearXNG for web search"),
    ("AGENT_HARNESS_MODEL", "Default model identifier"),
)


def _catalog() -> list[dict[str, Any]]:
    """Provider keys + managed env keys with live set/not-set status."""
    file_values = read_env_values()
    items: list[dict[str, Any]] = []
    for key, description in (*PROVIDER_KEYS, *MANAGED_ENV_KEYS):
        live = bool(os.environ.get(key, "").strip())
        items.append(
            {
                "key": key,
                "description": description,
                "set": live or bool(file_values.get(key, "").strip()),
                "liveSet": live,
            }
        )
    return items


async def secrets_list(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """List managed secret keys with set/not-set status (values never sent)."""
    gw.require_db()  # keep auth/db gating consistent with other features
    return {"secrets": _catalog(), "envFile": str(find_env_file())}


async def secrets_set(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Set a secret for this process and, by default, persist to ``.env``."""
    from ah.security.env_file import _validate_key, set_env_values

    gw.require_db()
    raw_key = params.get("key")
    value = params.get("value")
    persist = params.get("persist", True)
    if not isinstance(raw_key, str) or not raw_key.strip():
        raise RpcError(INVALID_PARAMS, "key must be a non-empty string")
    if not isinstance(value, str) or not 1 <= len(value) <= 2000:
        raise RpcError(INVALID_PARAMS, "value must be a string of 1-2000 characters")
    if not isinstance(persist, bool):
        raise RpcError(INVALID_PARAMS, "persist must be a boolean")
    try:
        key = _validate_key(raw_key)
    except ValueError as e:
        raise RpcError(INVALID_PARAMS, str(e)) from None
    value = value.strip()
    if not value:
        raise RpcError(INVALID_PARAMS, "value must not be blank")
    os.environ[key] = value
    env_file: str | None = None
    if persist:
        from ah.core.config import get_config

        try:
            env_file = str(set_env_values({key: value}))
        except (OSError, ValueError) as e:
            logger.warning("secrets.set: .env write failed for %s: %s", key, e)
            raise RpcError(INVALID_PARAMS, f"could not write .env: {e}") from None
        # Refresh the config singleton's in-memory copy for known keys.
        try:
            get_config().set(key.lower(), value)
        except Exception:
            pass
    logger.info("secrets.set: %s updated (persist=%s)", key, persist)
    return {"key": key, "set": True, "persisted": persist, "envFile": env_file}


async def secrets_clear(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Remove a secret from the process env and blank it in ``.env``."""
    from ah.security.env_file import _validate_key, set_env_values

    gw.require_db()
    raw_key = params.get("key")
    if not isinstance(raw_key, str) or not raw_key.strip():
        raise RpcError(INVALID_PARAMS, "key must be a non-empty string")
    try:
        key = _validate_key(raw_key)
    except ValueError as e:
        raise RpcError(INVALID_PARAMS, str(e)) from None
    os.environ.pop(key, None)
    # Best effort: blank the persisted line so a restart stays cleared.
    try:
        if find_env_file().is_file():
            stored = read_env_values()
            if key in stored:
                set_env_values({key: ""})
    except (OSError, ValueError) as e:
        logger.warning("secrets.clear: .env blank failed for %s: %s", key, e)
    try:
        from ah.core.config import get_config

        if key.lower() in get_config().to_dict():
            get_config().set(key.lower(), "")
    except Exception:
        pass
    return {"key": key, "set": False}
