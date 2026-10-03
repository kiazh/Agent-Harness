"""API-key authentication for the HTTP server.

The key is read from the ``AGENT_HARNESS_API_KEY`` environment variable (via
``.env``). If it is unset the server refuses every authenticated request with
503 rather than silently running wide open — opt into no-auth explicitly with
``AGENT_HARNESS_API_KEY=disabled`` for local development.
"""

from __future__ import annotations

import hmac
import os

from fastapi import Header, HTTPException, status

API_KEY_ENV = "AGENT_HARNESS_API_KEY"
_NO_AUTH = "disabled"


def configured_key() -> str | None:
    key = os.environ.get(API_KEY_ENV, "").strip()
    return key or None


def _extract(authorization: str | None, x_api_key: str | None) -> str | None:
    if x_api_key:
        return x_api_key.strip()
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


async def require_api_key(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    """FastAPI dependency: accept the request only with the right key."""
    expected = configured_key()
    if expected is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"API key not configured; set {API_KEY_ENV} (or {API_KEY_ENV}={_NO_AUTH} to disable auth)",
        )
    if expected == _NO_AUTH:
        return
    presented = _extract(authorization, x_api_key)
    # Constant-time comparison so a wrong key cannot be guessed by timing.
    if presented is None or not hmac.compare_digest(presented, expected):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "invalid or missing API key",
            headers={"WWW-Authenticate": "Bearer"},
        )
