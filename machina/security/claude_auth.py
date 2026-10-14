"""Claude Pro/Max subscription credentials — the Claude Code OAuth flow.

Machina can run turns on a Claude Pro/Max subscription through the same public
OAuth client the Claude Code CLI uses (confirmed against the installed
``claude`` 2.1.150 binary and the reference Hermes implementation): PKCE
authorization at ``claude.com/cai/oauth/authorize``, manual or loopback
callback, JSON token requests to ``platform.claude.com/v1/oauth/token``.
Tokens live in ``~/.machina/auth/claude.json`` (0600, atomic replace);
``import_from_claude_code`` copies a pair from ``~/.claude/.credentials.json``
(or the macOS Keychain item, both read-only) so a user who already ran
``claude`` can select ``provider=claude`` with no extra step.

Refresh tokens are single-use with rotation: the store adopts a peer's newer
pair instead of replaying a consumed token. Machina never writes to Claude
Code's own stores; a refresh performed only here can leave Claude Code's
copy stale, so the login hint recommends ``machina auth claude login`` for
long-lived use.

No token value is ever logged or embedded in an error message; callers get
:class:`ClaudeAuthError` with a stable ``code`` and an actionable hint.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import secrets
import subprocess
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from machina.core.config import config
from machina.core.exceptions import ProviderError

# Public Claude Code OAuth registration (production config of claude 2.1.150).
CLAUDE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
AUTHORIZE_URL = "https://claude.com/cai/oauth/authorize"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CONSOLE_TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"  # historical fallback
MANUAL_REDIRECT_URI = "https://platform.claude.com/oauth/code/callback"
SCOPES = "user:profile user:inference user:sessions:claude_code user:mcp_servers user:file_upload"
OAUTH_BETA = "oauth-2025-04-20"
# Anthropic 429s token-endpoint requests whose UA starts with ``claude-code/``
# (reference observation); the real CLI posts with a bare axios client there.
TOKEN_USER_AGENT = "axios/1.7.9"
CALLBACK_PORT = 41519
CALLBACK_PATH = "/callback"
KEYCHAIN_SERVICE = "Claude Code-credentials"

AUTH_FILE_ENV = "MACHINA_CLAUDE_AUTH_FILE"
REFRESH_SKEW_SECONDS = 120
LOGIN_HINT = "run `claude` and sign in, then `machina auth claude login --import`"
_STORE_FIELDS = (
    "access_token",
    "refresh_token",
    "expires_at",
    "scopes",
    "subscription_type",
    "last_refresh",
)
_DEFAULT_TIMEOUT = 20.0


class ClaudeAuthError(ProviderError):
    """Typed Claude auth failure. Carries a stable ``code``, never tokens."""

    def __init__(self, message: str, *, code: str = "claude_auth_error") -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Token store (~/.machina/auth/claude.json)
# ---------------------------------------------------------------------------


def auth_path() -> Path:
    """Resolve the Machina Claude token file without touching the disk."""
    override = os.environ.get(AUTH_FILE_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    home = ""
    with suppress(Exception):
        home = str(config.get("machina_home") or "")
    root = Path(home).expanduser() if home else Path.home() / ".machina"
    return root / "auth" / "claude.json"


def claude_code_auth_path() -> Path:
    """Claude Code's shared credential file (honours ``CLAUDE_CONFIG_DIR``)."""
    override = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    root = Path(override).expanduser() if override else Path.home() / ".claude"
    return root / ".credentials.json"


def _utc_now_z() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _normalize_scopes(value: Any) -> list[str]:
    if isinstance(value, str):
        return [scope for scope in value.split() if scope]
    if isinstance(value, list):
        return [str(scope) for scope in value if str(scope).strip()]
    return []


def _normalize_store(raw: Any) -> dict[str, Any] | None:
    """Shape a stored/Claude Code payload into the Machina token dict."""
    if not isinstance(raw, dict):
        return None
    tokens = raw.get("tokens") if isinstance(raw.get("tokens"), dict) else raw
    if not isinstance(tokens, dict):
        return None
    oauth = tokens.get("claudeAiOauth")
    if isinstance(oauth, dict):
        tokens = oauth
    access = tokens.get("access_token") or tokens.get("accessToken")
    if not isinstance(access, str) or not access.strip():
        return None
    refresh = tokens.get("refresh_token") or tokens.get("refreshToken")
    expires_at = tokens.get("expires_at")
    if not isinstance(expires_at, (int, float)) or isinstance(expires_at, bool):
        # Claude Code keeps ``expiresAt`` in milliseconds.
        expires_ms = tokens.get("expiresAt")
        if isinstance(expires_ms, (int, float)) and not isinstance(expires_ms, bool):
            expires_at = float(expires_ms) / 1000.0
        else:
            expires_at = None
    subscription = tokens.get("subscription_type") or tokens.get("subscriptionType")
    return {
        "access_token": access.strip(),
        "refresh_token": refresh.strip() if isinstance(refresh, str) else "",
        "expires_at": float(expires_at) if isinstance(expires_at, (int, float)) else None,
        "scopes": _normalize_scopes(tokens.get("scopes") or tokens.get("scope")),
        "subscription_type": str(subscription).strip() if isinstance(subscription, str) else None,
        "last_refresh": tokens.get("last_refresh")
        if isinstance(tokens.get("last_refresh"), str)
        else None,
    }


def load_tokens(path: str | Path | None = None) -> dict[str, Any] | None:
    """Read the Machina token store; None when absent or unusable."""
    target = Path(path) if path is not None else auth_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return _normalize_store(raw)


def save_tokens(tokens: dict[str, Any], path: str | Path | None = None) -> Path:
    """Atomically write the token store with 0600 permissions."""
    target = Path(path) if path is not None else auth_path()
    normalized = _normalize_store(tokens) or {}
    for field in _STORE_FIELDS:
        normalized.setdefault(field, None)
    if not normalized.get("last_refresh"):
        normalized["last_refresh"] = _utc_now_z()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=str(target.parent))
    try:
        os.chmod(tmp_name, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(normalized, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
        os.chmod(target, 0o600)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp_name)
        raise
    return target


def is_expired(tokens: dict[str, Any] | None, *, skew_seconds: int = REFRESH_SKEW_SECONDS) -> bool:
    """True when the access token is (nearly) expired.

    Unknown expiry is NOT treated as expired: the call proceeds and a 401
    drives the refresh, which is the only place the server's verdict exists.
    """
    if not isinstance(tokens, dict) or not tokens.get("access_token"):
        return True
    expires_at = tokens.get("expires_at")
    if not isinstance(expires_at, (int, float)) or isinstance(expires_at, bool):
        return False
    return float(expires_at) <= time.time() + max(0, int(skew_seconds))


# ---------------------------------------------------------------------------
# Claude Code import (read-only file + macOS Keychain)
# ---------------------------------------------------------------------------


def _decode_keychain_blob(text: str) -> str:
    """Decode the ``security`` hex form ``0x<hex>  "…"`` when present."""
    stripped = text.strip()
    if stripped.startswith("0x"):
        hex_part = stripped[2:].split('"', 1)[0].strip()
        with suppress(ValueError):
            return bytes.fromhex(hex_part).decode("utf-8")
    return stripped


def _read_claude_code_keychain() -> dict[str, Any] | None:
    """Read the "Claude Code-credentials" Keychain item (macOS only, read-only).

    Best-effort: a locked/denied Keychain must never break status or a call —
    it simply yields no tokens. The credential is never written back.
    """
    if platform.system() != "Darwin":
        return None
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        payload = json.loads(_decode_keychain_blob(result.stdout))
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _read_claude_code_tokens() -> dict[str, Any] | None:
    """Claude Code tokens from its file (preferred) or the macOS Keychain."""
    try:
        raw = json.loads(claude_code_auth_path().read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        raw = None
    tokens = _normalize_store(raw) if raw is not None else None
    if tokens is not None:
        return tokens
    return _normalize_store(_read_claude_code_keychain())


def import_from_claude_code(
    *,
    path: str | Path | None = None,
    force: bool = False,
) -> dict[str, Any] | None:
    """Copy Claude Code's tokens into the Machina store (read-only there).

    Existing Machina tokens win unless *force* is set, so a provider call
    cannot clobber a fresher Machina rotation with a stale CLI pair.
    """
    target = Path(path) if path is not None else auth_path()
    if not force and load_tokens(target) is not None:
        return None
    tokens = _read_claude_code_tokens()
    if not tokens:
        return None
    save_tokens(tokens, target)
    return load_tokens(target)


def status(*, path: str | Path | None = None) -> dict[str, Any]:
    """Read-only status: signed in?, expiry, plan, source. Never tokens."""
    result: dict[str, Any] = {
        "signed_in": False,
        "source": None,
        "expired": None,
        "expires_at": None,
        "subscription_type": None,
        "scopes": [],
        "last_refresh": None,
    }
    tokens = load_tokens(path)
    source = "machina"
    if tokens is None:
        tokens = _read_claude_code_tokens()
        source = "claude"
    if tokens is None:
        return result
    result.update(
        signed_in=True,
        source=source,
        expired=is_expired(tokens, skew_seconds=0),
        expires_at=tokens.get("expires_at"),
        subscription_type=tokens.get("subscription_type"),
        scopes=tokens.get("scopes") or [],
        last_refresh=tokens.get("last_refresh"),
    )
    return result


# ---------------------------------------------------------------------------
# Token endpoint (platform.claude.com/v1/oauth/token)
# ---------------------------------------------------------------------------


def _client_or_new(client: httpx.Client | None, timeout: float) -> tuple[httpx.Client, bool]:
    if client is not None:
        return client, False
    return httpx.Client(
        timeout=httpx.Timeout(float(timeout), connect=min(10.0, float(timeout)))
    ), True


def _error_detail(response: httpx.Response) -> tuple[str, str]:
    """(code, human detail) from an OAuth error body; never echoes secrets."""
    try:
        payload = response.json()
    except Exception:
        return "", ""
    if not isinstance(payload, dict):
        return "", ""
    error = payload.get("error")
    if isinstance(error, dict):
        code = error.get("code") or error.get("type") or ""
        message = error.get("message") or ""
        return str(code).strip(), str(message).strip()
    if isinstance(error, str):
        detail = payload.get("error_description") or payload.get("message") or ""
        return error.strip(), str(detail).strip()
    return "", ""


def _refresh_failure(response: httpx.Response) -> ClaudeAuthError:
    code, detail = _error_detail(response)
    hint = ""
    if response.status_code in (400, 401, 403) or code in {
        "invalid_grant",
        "invalid_token",
        "refresh_token_reused",
    }:
        hint = f"; {LOGIN_HINT}"
    message = (
        f"Claude token refresh failed (HTTP {response.status_code}"
        + (f", {code}" if code else "")
        + (f": {detail}" if detail else "")
        + f"){hint}"
    )
    return ClaudeAuthError(message, code=code or "claude_refresh_failed")


def refresh(
    tokens: dict[str, Any] | None = None,
    *,
    path: str | Path | None = None,
    client: httpx.Client | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Exchange the refresh token for fresh tokens and persist the rotation.

    Refresh tokens are single-use with rotation: if the store already holds a
    different pair (the CLI or a peer process refreshed first), that pair is
    adopted instead of replaying a consumed token.
    """
    target = Path(path) if path is not None else auth_path()
    stored = load_tokens(target)
    current = _normalize_store(tokens) if tokens else stored
    if current is None:
        raise ClaudeAuthError(
            f"not signed in to Claude: no tokens stored and nothing to refresh; {LOGIN_HINT}",
            code="claude_auth_missing",
        )
    if (
        stored
        and stored.get("refresh_token")
        and stored["refresh_token"] != current.get("refresh_token")
    ):
        if not is_expired(stored):
            return stored
        current = stored
    refresh_token = str(current.get("refresh_token") or "").strip()
    if not refresh_token:
        raise ClaudeAuthError(
            f"Claude credential has no refresh token; {LOGIN_HINT}",
            code="claude_auth_missing_refresh_token",
        )
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": CLAUDE_CLIENT_ID,
        "scope": SCOPES,
    }
    http, owns_client = _client_or_new(client, timeout)
    try:
        response = _post_token(http, payload)
    except httpx.HTTPError as exc:
        raise ClaudeAuthError(
            f"Claude token refresh could not reach {TOKEN_URL} ({type(exc).__name__}); "
            f"{LOGIN_HINT}",
            code="claude_refresh_unreachable",
        ) from exc
    finally:
        if owns_client:
            http.close()

    if response.status_code == 429:
        raise ClaudeAuthError(
            "Claude token refresh was rate limited (HTTP 429); credentials are still valid — "
            "retry later.",
            code="claude_refresh_rate_limited",
        )
    if response.status_code != 200:
        raise _refresh_failure(response)

    try:
        body = response.json()
    except ValueError as exc:
        raise ClaudeAuthError(
            "Claude token refresh returned invalid JSON (HTTP 200); retry, then "
            f"re-authenticate if it persists; {LOGIN_HINT}",
            code="claude_refresh_invalid_json",
        ) from exc
    if not isinstance(body, dict) or not body.get("access_token"):
        raise ClaudeAuthError(
            f"Claude token refresh response was missing access_token; {LOGIN_HINT}",
            code="claude_refresh_missing_access_token",
        )
    expires_at = current.get("expires_at")
    expires_in = body.get("expires_in")
    if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool):
        expires_at = time.time() + float(expires_in)
    updated = {
        # Rotation: the server's new refresh token wins; otherwise keep ours.
        "access_token": str(body["access_token"]),
        "refresh_token": str(body.get("refresh_token") or refresh_token),
        "expires_at": expires_at,
        "scopes": _normalize_scopes(body.get("scope")) or current.get("scopes") or [],
        "subscription_type": current.get("subscription_type"),
        "last_refresh": _utc_now_z(),
    }
    normalized = _normalize_store(updated)
    assert normalized is not None  # access_token is non-empty by construction above
    save_tokens(normalized, target)
    return normalized


def _post_token(http: httpx.Client, payload: dict[str, Any]) -> httpx.Response:
    """POST a JSON token request, falling back to the historic token host."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "anthropic-beta": OAUTH_BETA,
        "User-Agent": TOKEN_USER_AGENT,
    }
    response = http.post(TOKEN_URL, headers=headers, json=payload)
    if response.status_code == 404:
        # Historical host; only retried on a hard 404 from the primary.
        response = http.post(CONSOLE_TOKEN_URL, headers=headers, json=payload)
    return response


def resolve_tokens(
    *,
    path: str | Path | None = None,
    refresh_when_expiring: bool = True,
    timeout: float = _DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Provider-facing credential resolution: store -> Claude Code import -> refresh.

    Raises :class:`ClaudeAuthError` with the login hint when nothing usable
    is available. Importing is part of resolution, so a user who already ran
    ``claude`` can select ``provider=claude`` with no extra step.
    """
    target = Path(path) if path is not None else auth_path()
    tokens = load_tokens(target)
    if tokens is None:
        tokens = import_from_claude_code(path=target)
    if tokens is None:
        raise ClaudeAuthError(
            "not signed in to Claude: no Machina token store and no Claude Code "
            f"credentials found; {LOGIN_HINT}",
            code="claude_auth_missing",
        )
    if refresh_when_expiring and is_expired(tokens) and tokens.get("refresh_token"):
        tokens = refresh(tokens, path=target, timeout=timeout)
    return tokens


# ---------------------------------------------------------------------------
# Browser PKCE login (Claude Code flow; driven by `machina auth claude login`)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LoginRequest:
    """Everything the login command needs to open and complete the PKCE flow."""

    url: str
    state: str
    code_verifier: str
    redirect_uri: str


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def login_url(
    *,
    manual: bool = False,
    redirect_uri: str | None = None,
    state: str | None = None,
    code_verifier: str | None = None,
) -> LoginRequest:
    """Build the authorization URL + PKCE secrets.

    Loopback by default (``http://localhost:<port>/callback``); with
    *manual* the redirect is Claude's code-display page and the user pastes
    the ``code#state`` string back (the same escape hatch Claude Code uses on
    headless machines).
    """
    uri = (
        MANUAL_REDIRECT_URI
        if manual
        else (redirect_uri or f"http://localhost:{CALLBACK_PORT}{CALLBACK_PATH}")
    )
    verifier = code_verifier or secrets.token_urlsafe(64)
    flow_state = state or secrets.token_urlsafe(32)
    query = urlencode(
        {
            "code": "true",
            "client_id": CLAUDE_CLIENT_ID,
            "response_type": "code",
            "redirect_uri": uri,
            "scope": SCOPES,
            "code_challenge": _pkce_challenge(verifier),
            "code_challenge_method": "S256",
            "state": flow_state,
        }
    )
    return LoginRequest(
        url=f"{AUTHORIZE_URL}?{query}",
        state=flow_state,
        code_verifier=verifier,
        redirect_uri=uri,
    )


def parse_manual_code(text: str) -> tuple[str, str]:
    """Split a pasted manual callback string into ``(code, state)``.

    Claude's callback shows ``<code>#<state>``; a bare code is accepted with
    an empty state (the exchange then relies on the PKCE verifier alone).
    """
    parts = str(text or "").strip().split("#", 1)
    code = parts[0].strip()
    state = parts[1].strip() if len(parts) > 1 else ""
    return code, state


def exchange_code(
    code: str,
    *,
    code_verifier: str,
    redirect_uri: str | None = None,
    state: str = "",
    path: str | Path | None = None,
    client: httpx.Client | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Swap an authorization code (+PKCE verifier) for tokens and persist them."""
    if not str(code).strip():
        raise ClaudeAuthError("authorization code is empty", code="claude_code_missing")
    target = Path(path) if path is not None else auth_path()
    payload: dict[str, Any] = {
        "grant_type": "authorization_code",
        "code": str(code).strip(),
        "redirect_uri": redirect_uri or MANUAL_REDIRECT_URI,
        "client_id": CLAUDE_CLIENT_ID,
        "code_verifier": code_verifier,
    }
    if state:
        payload["state"] = state
    http, owns_client = _client_or_new(client, timeout)
    try:
        response = _post_token(http, payload)
    except httpx.HTTPError as exc:
        raise ClaudeAuthError(
            f"Claude token exchange could not reach {TOKEN_URL} ({type(exc).__name__}); "
            f"{LOGIN_HINT}",
            code="claude_exchange_unreachable",
        ) from exc
    finally:
        if owns_client:
            http.close()

    if response.status_code == 429:
        raise ClaudeAuthError(
            "Claude token exchange was rate limited (HTTP 429); retry later.",
            code="claude_exchange_rate_limited",
        )
    if response.status_code != 200:
        code_value, detail = _error_detail(response)
        raise ClaudeAuthError(
            f"Claude token exchange failed (HTTP {response.status_code}"
            + (f", {code_value}" if code_value else "")
            + (f": {detail}" if detail else "")
            + ").",
            code=code_value or "claude_exchange_failed",
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise ClaudeAuthError(
            "Claude token exchange returned invalid JSON (HTTP 200).",
            code="claude_exchange_invalid_json",
        ) from exc
    if not isinstance(body, dict) or not body.get("access_token"):
        raise ClaudeAuthError(
            "Claude token exchange response was missing access_token.",
            code="claude_exchange_missing_access_token",
        )
    expires_at = None
    expires_in = body.get("expires_in")
    if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool):
        expires_at = time.time() + float(expires_in)
    # Claude's OAuth response carries the plan on the account object (when
    # present); keep it as display metadata only.
    account = body.get("account") if isinstance(body.get("account"), dict) else {}
    subscription = account.get("subscription_type") or body.get("subscription_type")
    normalized = _normalize_store(
        {
            "access_token": str(body["access_token"]),
            "refresh_token": str(body.get("refresh_token") or ""),
            "expires_at": expires_at,
            "scopes": _normalize_scopes(body.get("scope")),
            "subscription_type": str(subscription).strip() if subscription else None,
            "last_refresh": _utc_now_z(),
        }
    )
    assert normalized is not None  # access_token is non-empty by the check above
    save_tokens(normalized, target)
    return normalized
