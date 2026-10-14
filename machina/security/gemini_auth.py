"""Gemini (gemini-cli) subscription credentials — OAuth tokens + refresh.

Machina can run turns against Google's Cloud Code Assist backend the same way
the Gemini CLI does, on a Gemini Code Assist license (Standard/Enterprise or
an equivalent Workspace seat). Tokens live in ``~/.machina/auth/gemini.json``
(0600, atomic replace); ``import_from_gemini_cli`` copies a pair from
``~/.gemini/oauth_creds.json`` (read-only on the CLI's file).

The OAuth client below (client id + the installed-app secret Google documents
as non-secret) is the same one embedded in the Apache-2.0 ``gemini-cli``
sources; the loopback + user-code flows mirror
``packages/core/src/code_assist/oauth2.ts``. Scope note (2026-10-14): Google
stopped serving consumer tiers (free / AI Pro / Ultra) through this flow on
2026-06-18; only Code Assist Standard/Enterprise licenses still work, and
calls will surface a typed eligibility error otherwise. See
``docs/verification/proofs/oauth_research_2026-10-14/``.

No token value is ever logged or embedded in an error message; callers get
:class:`GeminiAuthError` with a stable ``code`` and an actionable hint.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
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

# Public gemini-cli OAuth registration (installed application; the "secret" is
# explicitly documented as not secret by Google's installed-app OAuth docs).
GEMINI_CLIENT_ID = "REDACTED"
GEMINI_CLIENT_SECRET = "REDACTED"
AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
CODE_ASSIST_REDIRECT_URI = "https://codeassist.google.com/authcode"
SCOPES = (
    "https://www.googleapis.com/auth/cloud-platform "
    "https://www.googleapis.com/auth/userinfo.email "
    "https://www.googleapis.com/auth/userinfo.profile"
)
CALLBACK_PORT = 42813
CALLBACK_PATH = "/oauth2callback"

AUTH_FILE_ENV = "MACHINA_GEMINI_AUTH_FILE"
IMPORT_FILE_ENV = "MACHINA_GEMINI_IMPORT_FILE"
REFRESH_SKEW_SECONDS = 120
LOGIN_HINT = "run `gemini` and sign in with Google, then `machina auth gemini login --import`"
_STORE_FIELDS = (
    "access_token",
    "refresh_token",
    "id_token",
    "scope",
    "email",
    "project",
    "expires_at",
    "last_refresh",
)
_DEFAULT_TIMEOUT = 20.0


class GeminiAuthError(ProviderError):
    """Typed Gemini auth failure. Carries a stable ``code``, never tokens."""

    def __init__(self, message: str, *, code: str = "gemini_auth_error") -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Token store (~/.machina/auth/gemini.json)
# ---------------------------------------------------------------------------


def auth_path() -> Path:
    """Resolve the Machina Gemini token file without touching the disk."""
    override = os.environ.get(AUTH_FILE_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    home = ""
    with suppress(Exception):
        home = str(config.get("machina_home") or "")
    root = Path(home).expanduser() if home else Path.home() / ".machina"
    return root / "auth" / "gemini.json"


def gemini_cli_auth_path() -> Path:
    """The Gemini CLI's own credential file (``~/.gemini/oauth_creds.json``)."""
    override = os.environ.get(IMPORT_FILE_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".gemini" / "oauth_creds.json"


def _utc_now_z() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _decode_jwt_claims(token: Any) -> dict[str, Any]:
    """Decode a JWT payload without verifying the signature; {} when malformed."""
    if not isinstance(token, str) or token.count(".") != 2:
        return {}
    payload = token.split(".")[1]
    payload += "=" * ((4 - len(payload) % 4) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("utf-8")))
    except Exception:
        return {}
    return claims if isinstance(claims, dict) else {}


def extract_email(token: Any) -> str | None:
    """Google account email from an id/access token, tolerantly."""
    email = _decode_jwt_claims(token).get("email")
    if isinstance(email, str) and email.strip():
        return email.strip()
    return None


def extract_expiry(token: Any) -> float | None:
    """Unix expiry (seconds) from a JWT ``exp`` claim, or None."""
    exp = _decode_jwt_claims(token).get("exp")
    if isinstance(exp, (int, float)) and not isinstance(exp, bool):
        return float(exp)
    return None


def _normalize_store(raw: Any) -> dict[str, Any] | None:
    """Shape a stored/gemini-cli payload into the Machina token dict."""
    if not isinstance(raw, dict):
        return None
    tokens = raw.get("tokens") if isinstance(raw.get("tokens"), dict) else raw
    if not isinstance(tokens, dict):
        return None
    access = tokens.get("access_token")
    if not isinstance(access, str) or not access.strip():
        return None
    refresh = tokens.get("refresh_token")
    id_token = tokens.get("id_token")
    scope = tokens.get("scope")
    expires_at = tokens.get("expires_at")
    if not isinstance(expires_at, (int, float)) or isinstance(expires_at, bool):
        # The gemini-cli store keeps ``expiry_date`` in milliseconds.
        expiry_ms = tokens.get("expiry_date")
        if isinstance(expiry_ms, (int, float)) and not isinstance(expiry_ms, bool):
            expires_at = float(expiry_ms) / 1000.0
        else:
            expires_at = extract_expiry(access)
    email = tokens.get("email")
    if not isinstance(email, str) or not email.strip():
        email = extract_email(id_token) or extract_email(access)
    project = tokens.get("project")
    last_refresh = raw.get("last_refresh") or tokens.get("last_refresh")
    return {
        "access_token": access.strip(),
        "refresh_token": refresh.strip() if isinstance(refresh, str) else "",
        "id_token": id_token.strip() if isinstance(id_token, str) else "",
        "scope": scope.strip() if isinstance(scope, str) else "",
        "email": email,
        "project": project.strip() if isinstance(project, str) and project.strip() else None,
        "expires_at": float(expires_at) if isinstance(expires_at, (int, float)) else None,
        "last_refresh": str(last_refresh) if isinstance(last_refresh, str) else None,
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


def update_project(project: str, *, path: str | Path | None = None) -> None:
    """Persist the resolved Cloud Code Assist project id onto the store."""
    target = Path(path) if path is not None else auth_path()
    tokens = load_tokens(target)
    if tokens is None:
        return
    tokens["project"] = str(project).strip() or None
    save_tokens(tokens, target)


def is_expired(tokens: dict[str, Any] | None, *, skew_seconds: int = REFRESH_SKEW_SECONDS) -> bool:
    """True when the access token is (nearly) expired.

    Unknown expiry is NOT treated as expired: the call proceeds and a 401
    drives the refresh, which is the only place the server's verdict exists.
    """
    if not isinstance(tokens, dict) or not tokens.get("access_token"):
        return True
    expires_at = tokens.get("expires_at")
    if not isinstance(expires_at, (int, float)) or isinstance(expires_at, bool):
        expires_at = extract_expiry(tokens.get("access_token"))
    if expires_at is None:
        return False
    return float(expires_at) <= time.time() + max(0, int(skew_seconds))


# ---------------------------------------------------------------------------
# Gemini CLI import (read-only on ~/.gemini/oauth_creds.json)
# ---------------------------------------------------------------------------


def _read_gemini_cli_tokens(path: str | Path | None = None) -> dict[str, Any] | None:
    target = Path(path) if path is not None else gemini_cli_auth_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return _normalize_store(raw)


def import_from_gemini_cli(
    *,
    path: str | Path | None = None,
    source: str | Path | None = None,
    force: bool = False,
) -> dict[str, Any] | None:
    """Copy the Gemini CLI's tokens into the Machina store.

    The CLI's file itself is never modified. Existing Machina tokens win
    unless *force* is set.
    """
    target = Path(path) if path is not None else auth_path()
    if not force and load_tokens(target) is not None:
        return None
    tokens = _read_gemini_cli_tokens(source)
    if not tokens:
        return None
    save_tokens(tokens, target)
    return load_tokens(target)


def status(*, path: str | Path | None = None, source: str | Path | None = None) -> dict[str, Any]:
    """Read-only status: signed in?, expiry, account, project, source. Never tokens."""
    result: dict[str, Any] = {
        "signed_in": False,
        "source": None,
        "expired": None,
        "expires_at": None,
        "email": None,
        "project": None,
        "last_refresh": None,
    }
    tokens = load_tokens(path)
    origin = "machina"
    if tokens is None:
        tokens = _read_gemini_cli_tokens(source)
        origin = "gemini-cli"
    if tokens is None:
        return result
    result.update(
        signed_in=True,
        source=origin,
        expired=is_expired(tokens, skew_seconds=0),
        expires_at=tokens.get("expires_at"),
        email=tokens.get("email"),
        project=tokens.get("project"),
        last_refresh=tokens.get("last_refresh"),
    )
    return result


# ---------------------------------------------------------------------------
# Token endpoint (oauth2.googleapis.com)
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


def _refresh_failure(response: httpx.Response) -> GeminiAuthError:
    code, detail = _error_detail(response)
    hint = ""
    if response.status_code in (400, 401, 403) or code in {
        "invalid_grant",
        "invalid_token",
        "unauthorized_client",
    }:
        hint = f"; {LOGIN_HINT}"
    message = (
        f"Gemini token refresh failed (HTTP {response.status_code}"
        + (f", {code}" if code else "")
        + (f": {detail}" if detail else "")
        + f"){hint}"
    )
    return GeminiAuthError(message, code=code or "gemini_refresh_failed")


def refresh(
    tokens: dict[str, Any] | None = None,
    *,
    path: str | Path | None = None,
    client: httpx.Client | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Exchange the refresh token for a fresh access token and persist it.

    Google's desktop clients do not rotate refresh tokens; if the server does
    return a new one, it wins over the stored value.
    """
    target = Path(path) if path is not None else auth_path()
    stored = load_tokens(target)
    current = _normalize_store(tokens) if tokens else stored
    if current is None:
        raise GeminiAuthError(
            f"not signed in to Gemini: no tokens stored and nothing to refresh; {LOGIN_HINT}",
            code="gemini_auth_missing",
        )
    refresh_token = str(current.get("refresh_token") or "").strip()
    if not refresh_token:
        raise GeminiAuthError(
            f"Gemini credential has no refresh token; {LOGIN_HINT}",
            code="gemini_auth_missing_refresh_token",
        )
    http, owns_client = _client_or_new(client, timeout)
    try:
        response = http.post(
            TOKEN_URL,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": GEMINI_CLIENT_ID,
                "client_secret": GEMINI_CLIENT_SECRET,
            },
        )
    except httpx.HTTPError as exc:
        raise GeminiAuthError(
            f"Gemini token refresh could not reach {TOKEN_URL} ({type(exc).__name__}); {LOGIN_HINT}",
            code="gemini_refresh_unreachable",
        ) from exc
    finally:
        if owns_client:
            http.close()

    if response.status_code == 429:
        raise GeminiAuthError(
            "Gemini token refresh was rate limited (HTTP 429); credentials are still valid — "
            "retry later.",
            code="gemini_refresh_rate_limited",
        )
    if response.status_code != 200:
        raise _refresh_failure(response)

    try:
        payload = response.json()
    except ValueError as exc:
        raise GeminiAuthError(
            "Gemini token refresh returned invalid JSON (HTTP 200); retry, then re-authenticate "
            f"if it persists; {LOGIN_HINT}",
            code="gemini_refresh_invalid_json",
        ) from exc
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise GeminiAuthError(
            f"Gemini token refresh response was missing access_token; {LOGIN_HINT}",
            code="gemini_refresh_missing_access_token",
        )
    expires_at = current.get("expires_at")
    expires_in = payload.get("expires_in")
    if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool):
        expires_at = time.time() + float(expires_in)
    updated = {
        # Rotation: the server's new refresh token wins; otherwise keep ours.
        "access_token": str(payload["access_token"]),
        "refresh_token": str(payload.get("refresh_token") or refresh_token),
        "id_token": str(payload.get("id_token") or current.get("id_token") or ""),
        "scope": str(payload.get("scope") or current.get("scope") or ""),
        "email": current.get("email"),
        "project": current.get("project"),
        "expires_at": expires_at,
        "last_refresh": _utc_now_z(),
    }
    normalized = _normalize_store(updated)
    assert normalized is not None  # access_token is non-empty by construction above
    save_tokens(normalized, target)
    return normalized


def resolve_tokens(
    *,
    path: str | Path | None = None,
    source: str | Path | None = None,
    refresh_when_expiring: bool = True,
    timeout: float = _DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Provider-facing credential resolution: store -> CLI import -> refresh.

    Raises :class:`GeminiAuthError` with the login hint when nothing usable
    is available. Importing is part of resolution, so a user who already ran
    ``gemini`` can select ``provider=gemini`` with no extra step.
    """
    target = Path(path) if path is not None else auth_path()
    tokens = load_tokens(target)
    if tokens is None:
        tokens = import_from_gemini_cli(path=target, source=source)
    if tokens is None:
        raise GeminiAuthError(
            "not signed in to Gemini: no Machina token store and no gemini-cli "
            f"credentials found; {LOGIN_HINT}",
            code="gemini_auth_missing",
        )
    if refresh_when_expiring and is_expired(tokens) and tokens.get("refresh_token"):
        tokens = refresh(tokens, path=target, timeout=timeout)
    return tokens


# ---------------------------------------------------------------------------
# Browser PKCE login (gemini-cli flow; driven by `machina auth gemini login`)
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
    redirect_uri: str | None = None,
    state: str | None = None,
    code_verifier: str | None = None,
) -> LoginRequest:
    """Build the authorization URL + PKCE secrets for the loopback flow.

    ``redirect_uri`` defaults to the fixed loopback callback; the CLI binds an
    ephemeral port and passes its own ``http://127.0.0.1:<port>/oauth2callback``
    (the same policy gemini-cli uses).
    """
    uri = redirect_uri or f"http://127.0.0.1:{CALLBACK_PORT}{CALLBACK_PATH}"
    verifier = code_verifier or secrets.token_urlsafe(64)
    flow_state = state or secrets.token_urlsafe(32)
    query = urlencode(
        {
            "response_type": "code",
            "client_id": GEMINI_CLIENT_ID,
            "redirect_uri": uri,
            "scope": SCOPES,
            "access_type": "offline",
            "prompt": "consent",
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


def exchange_code(
    code: str,
    *,
    code_verifier: str,
    redirect_uri: str | None = None,
    path: str | Path | None = None,
    client: httpx.Client | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Swap an authorization code (+PKCE verifier) for tokens and persist them."""
    if not str(code).strip():
        raise GeminiAuthError("authorization code is empty", code="gemini_code_missing")
    target = Path(path) if path is not None else auth_path()
    http, owns_client = _client_or_new(client, timeout)
    try:
        response = http.post(
            TOKEN_URL,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            data={
                "grant_type": "authorization_code",
                "code": str(code).strip(),
                "redirect_uri": redirect_uri or CODE_ASSIST_REDIRECT_URI,
                "client_id": GEMINI_CLIENT_ID,
                "client_secret": GEMINI_CLIENT_SECRET,
                "code_verifier": code_verifier,
            },
        )
    except httpx.HTTPError as exc:
        raise GeminiAuthError(
            f"Gemini token exchange could not reach {TOKEN_URL} ({type(exc).__name__}); "
            f"{LOGIN_HINT}",
            code="gemini_exchange_unreachable",
        ) from exc
    finally:
        if owns_client:
            http.close()

    if response.status_code == 429:
        raise GeminiAuthError(
            "Gemini token exchange was rate limited (HTTP 429); retry later.",
            code="gemini_exchange_rate_limited",
        )
    if response.status_code != 200:
        code_value, detail = _error_detail(response)
        raise GeminiAuthError(
            f"Gemini token exchange failed (HTTP {response.status_code}"
            + (f", {code_value}" if code_value else "")
            + (f": {detail}" if detail else "")
            + ").",
            code=code_value or "gemini_exchange_failed",
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise GeminiAuthError(
            "Gemini token exchange returned invalid JSON (HTTP 200).",
            code="gemini_exchange_invalid_json",
        ) from exc
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise GeminiAuthError(
            "Gemini token exchange response was missing access_token.",
            code="gemini_exchange_missing_access_token",
        )
    expires_at = None
    expires_in = payload.get("expires_in")
    if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool):
        expires_at = time.time() + float(expires_in)
    normalized = _normalize_store(
        {
            "access_token": str(payload["access_token"]),
            "refresh_token": str(payload.get("refresh_token") or ""),
            "id_token": str(payload.get("id_token") or ""),
            "scope": str(payload.get("scope") or ""),
            "expires_at": expires_at,
            "last_refresh": _utc_now_z(),
        }
    )
    assert normalized is not None  # access_token is non-empty by the check above
    save_tokens(normalized, target)
    return normalized
