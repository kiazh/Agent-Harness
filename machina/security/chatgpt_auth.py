"""ChatGPT-account credentials for the Codex protocol (tokens + OAuth/refresh).

Machina talks to the same backend the Codex CLI uses, on the user's ChatGPT
subscription. Tokens live in ``~/.machina/auth/chatgpt.json`` (0600, atomic
replace). ``import_from_codex`` copies a pair from ``~/.codex/auth.json``
(read-only on the Codex CLI's file): the Codex OAuth client is public and its
loopback redirect (``http://localhost:1455/auth/callback``) is fixed by the
client registration, so the PKCE helpers here implement the same flow the
Codex CLI exposes.

Refresh exchanges are serialized per store with the repository's flock idiom
(``machina/interop.py`` / ``machina/connectors/store.py``) and all credential
publication is fenced by an authentication generation: a logout bumps the
generation (durably, next to the store) and removes the store, so a late
callback, exchange, or refresh response cannot resurrect revoked credentials.
Token values read from or written to the store are registered with the
process-wide secret-redaction registry, so they are redactable in tool
results and logs even when they were never resolved as named secrets.

No token value is ever logged or embedded in an error message; callers get
:class:`ChatGPTAuthError` with a stable ``code`` and an actionable hint.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from machina.core.config import config
from machina.core.exceptions import ProviderError
from machina.security.secrets import remember_secret_value

# Store locking follows machina/interop.py: an exclusive OS file lock on a
# sibling ``.lock`` file (POSIX flock; msvcrt on Windows).
_WINDOWS = os.name == "nt"
if _WINDOWS:  # pragma: no cover - platform branch
    import msvcrt
else:
    import fcntl

# Public Codex CLI OAuth registration (client id + fixed loopback redirect).
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"
SCOPES = "openid profile email offline_access"
CALLBACK_PORT = 1455
CALLBACK_PATH = "/auth/callback"
REDIRECT_URI = f"http://localhost:{CALLBACK_PORT}{CALLBACK_PATH}"

AUTH_FILE_ENV = "MACHINA_CHATGPT_AUTH_FILE"
REFRESH_SKEW_SECONDS = 120
AUTH_NAMESPACE = "https://api.openai.com/auth"
LOGIN_HINT = "run `codex login` (Machina imports its tokens automatically) or `machina auth login`"
_STORE_FIELDS = ("access_token", "refresh_token", "id_token", "account_id", "expires_at")
_DEFAULT_TIMEOUT = 20.0


class ChatGPTAuthError(ProviderError):
    """Typed ChatGPT auth failure. Carries a stable ``code``, never tokens."""

    def __init__(self, message: str, *, code: str = "chatgpt_auth_error") -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Token store (~/.machina/auth/chatgpt.json)
# ---------------------------------------------------------------------------


def auth_path() -> Path:
    """Resolve the Machina ChatGPT token file without touching the disk."""
    override = os.environ.get(AUTH_FILE_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    home = ""
    with suppress(Exception):
        home = str(config.get("machina_home") or "")
    root = Path(home).expanduser() if home else Path.home() / ".machina"
    return root / "auth" / "chatgpt.json"


# ---------------------------------------------------------------------------
# Store locking + authentication generation (round-3 review R3-2/R3-3)
# ---------------------------------------------------------------------------

_generation_lock = threading.Lock()
#: In-process fallback bumps for stores whose generation file is unwritable.
_generations: dict[str, int] = {}


def _store_target(path: str | Path | None = None) -> Path:
    """Resolved store identity used for keys, locks, and generation files."""
    target = Path(path) if path is not None else auth_path()
    try:
        return target.expanduser().resolve()
    except (OSError, ValueError):
        return target.expanduser()


def _generation_path(target: Path) -> Path:
    return target.with_name(target.name + ".generation")


def _read_generation(target: Path) -> int:
    try:
        return int(_generation_path(target).read_text(encoding="ascii").strip() or "0")
    except (OSError, ValueError):
        return 0


def credential_generation(path: str | Path | None = None) -> int:
    """Current authentication generation for a store.

    A login flow or refresh captures this before doing work and refuses to
    publish when it changed: any logout in between invalidates the work. The
    durable component (a small file next to the store) fences callbacks that
    arrive in OTHER processes; the in-process fallback covers stores whose
    directory is not writable when logout runs.
    """
    target = _store_target(path)
    with _generation_lock:
        local = _generations.get(str(target), 0)
    return _read_generation(target) + local


def invalidate_credentials(path: str | Path | None = None) -> int:
    """Bump a store's authentication generation; returns the new value.

    Called by logout paths before removing the store. Failing to persist the
    durable counter still fences every in-process caller.
    """
    target = _store_target(path)
    file = _generation_path(target)
    durable = _read_generation(target) + 1
    persisted = False
    try:
        file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=file.name + ".", suffix=".tmp", dir=str(file.parent))
        try:
            os.chmod(tmp_name, 0o600)
            with os.fdopen(fd, "w", encoding="ascii") as handle:
                handle.write(f"{durable}\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, file)
        except BaseException:
            with suppress(OSError):
                os.unlink(tmp_name)
            raise
        persisted = True
    except OSError:
        durable = _read_generation(target)
    with _generation_lock:
        if not persisted:
            _generations[str(target)] = _generations.get(str(target), 0) + 1
        local = _generations.get(str(target), 0)
    return durable + local


@contextmanager
def _store_lock(target: Path) -> Iterator[None]:
    """Exclusive per-store lock (same flock idiom as machina/interop.py).

    Serializes whole read -> exchange -> publish sections across threads AND
    processes, so two concurrent refreshes can never submit the same rotating
    refresh token. The lock file holds no state and the atomic store replace
    keeps the store itself always readable.
    """
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(str(target) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
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


def _store_fingerprint(target: Path) -> tuple[bool, bytes]:
    """(present, content digest) of the raw store; absent -> (False, b"").

    Used to detect an out-of-band change (e.g. logout removing the store
    while a refresh response is in flight) before publishing. Unreadable
    stores report present with an empty digest, so a change in readability
    still invalidates the snapshot instead of silently publishing over it.
    """
    try:
        return True, hashlib.sha256(target.read_bytes()).digest()
    except FileNotFoundError:
        return False, b""
    except OSError:
        return True, b""


def _register_token_values(tokens: dict[str, Any] | None) -> None:
    """Remember exact token bytes for outbound redaction (R3-1)."""
    if not isinstance(tokens, dict):
        return
    for field in ("access_token", "refresh_token", "id_token"):
        value = tokens.get(field)
        if isinstance(value, str) and value.strip():
            remember_secret_value(value.strip())


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


def extract_account_id(token: Any) -> str | None:
    """ChatGPT account id from a JWT, tolerantly.

    Primary claim: ``https://api.openai.com/auth`` -> ``chatgpt_account_id``
    (present on both Codex access and id tokens). Fallbacks accept a top-level
    claim for stores that flatten the namespace.
    """
    claims = _decode_jwt_claims(token)
    if not claims:
        return None
    scoped = claims.get(AUTH_NAMESPACE)
    if isinstance(scoped, dict):
        account = scoped.get("chatgpt_account_id")
        if isinstance(account, str) and account.strip():
            return account.strip()
    top_level = claims.get("chatgpt_account_id")
    if isinstance(top_level, str) and top_level.strip():
        return top_level.strip()
    return None


def extract_expiry(token: Any) -> float | None:
    """Unix expiry (seconds) from a JWT ``exp`` claim, or None."""
    exp = _decode_jwt_claims(token).get("exp")
    if isinstance(exp, (int, float)) and not isinstance(exp, bool):
        return float(exp)
    return None


def _normalize_store(raw: Any) -> dict[str, Any] | None:
    """Shape a stored/auth.json payload into the Machina token dict."""
    if not isinstance(raw, dict):
        return None
    tokens = raw.get("tokens") if isinstance(raw.get("tokens"), dict) else raw
    if not isinstance(tokens, dict):
        return None
    access = tokens.get("access_token")
    refresh = tokens.get("refresh_token")
    id_token = tokens.get("id_token")
    if not isinstance(access, str) or not access.strip():
        return None
    account = tokens.get("account_id")
    if not isinstance(account, str) or not account.strip():
        account = extract_account_id(id_token) or extract_account_id(access)
    expires_at = tokens.get("expires_at")
    if not isinstance(expires_at, (int, float)) or isinstance(expires_at, bool):
        expires_at = extract_expiry(access)
    last_refresh = raw.get("last_refresh") or tokens.get("last_refresh")
    return {
        "access_token": access.strip(),
        "refresh_token": refresh.strip() if isinstance(refresh, str) else "",
        "id_token": id_token.strip() if isinstance(id_token, str) else "",
        "account_id": account,
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
    normalized = _normalize_store(raw)
    _register_token_values(normalized)
    return normalized


def save_tokens(tokens: dict[str, Any], path: str | Path | None = None) -> Path:
    """Atomically write the token store with 0600 permissions."""
    target = Path(path) if path is not None else auth_path()
    normalized = _normalize_store(tokens) or {}
    for field in _STORE_FIELDS:
        normalized.setdefault(field, None)
    if not normalized.get("last_refresh"):
        normalized["last_refresh"] = _utc_now_z()
    _register_token_values(normalized)
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
    A missing access token is expired by definition.
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
# Codex CLI import (read-only on ~/.codex/auth.json)
# ---------------------------------------------------------------------------


def _codex_auth_file(codex_home: str | Path | None = None) -> Path:
    root = str(codex_home).strip() if codex_home else os.environ.get("CODEX_HOME", "").strip()
    base = Path(root).expanduser() if root else Path.home() / ".codex"
    return base / "auth.json"


def _read_codex_tokens(codex_home: str | Path | None = None) -> dict[str, Any] | None:
    """Read ~/.codex/auth.json tokens without writing anything."""
    try:
        raw = json.loads(_codex_auth_file(codex_home).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return _normalize_store(raw)


def import_from_codex(
    *,
    path: str | Path | None = None,
    codex_home: str | Path | None = None,
    force: bool = False,
) -> dict[str, Any] | None:
    """Copy the Codex CLI's ChatGPT tokens into the Machina store.

    The Codex file itself is never modified. Existing Machina tokens win
    unless *force* is set, so a provider call cannot clobber a fresher
    Machina rotation with a stale CLI pair.
    """
    target = Path(path) if path is not None else auth_path()
    generation = credential_generation(target)
    # Read the Codex file before touching the store directory: an import
    # with nothing to copy must leave no trace (no store dir, no lock file).
    tokens = _read_codex_tokens(codex_home)
    if not tokens:
        return None
    with _store_lock(target):
        if credential_generation(target) != generation:
            # A logout invalidated this store while the import was reading
            # the Codex file: never republish revoked credentials.
            return None
        if not force and load_tokens(target) is not None:
            return None
        save_tokens(tokens, target)
        return load_tokens(target)


def logout(*, path: str | Path | None = None) -> bool:
    """Remove the Machina token store and invalidate pending authentication work.

    The generation is bumped under the store lock BEFORE the unlink, so any
    login exchange or refresh already in flight can no longer publish
    credentials after this point. Returns True when a store existed and was
    removed. ``OSError`` from the unlink propagates (callers translate it to
    their own error surface).
    """
    target = Path(path) if path is not None else auth_path()
    with _store_lock(target):
        invalidate_credentials(target)
        try:
            target.unlink()
            return True
        except FileNotFoundError:
            return False


def status(
    *, path: str | Path | None = None, codex_home: str | Path | None = None
) -> dict[str, Any]:
    """Read-only status: signed in?, expiry, account, source. Never tokens.

    Reports the Machina store first; otherwise peeks at the Codex CLI file
    (without importing) so ``machina auth status`` can be honest before the
    first call.
    """
    result: dict[str, Any] = {
        "signed_in": False,
        "source": None,
        "expired": None,
        "expires_at": None,
        "account_id": None,
        "last_refresh": None,
    }
    tokens = load_tokens(path)
    source = "machina"
    if tokens is None:
        tokens = _read_codex_tokens(codex_home)
        source = "codex"
    if tokens is None:
        return result
    result.update(
        signed_in=True,
        source=source,
        expired=is_expired(tokens, skew_seconds=0),
        expires_at=tokens.get("expires_at"),
        account_id=tokens.get("account_id"),
        last_refresh=tokens.get("last_refresh"),
    )
    return result


# ---------------------------------------------------------------------------
# Refresh (POST auth.openai.com/oauth/token, refresh_token grant)
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


def _refresh_failure(response: httpx.Response) -> ChatGPTAuthError:
    code, detail = _error_detail(response)
    hint = ""
    if response.status_code in (401, 403) or code in {
        "invalid_grant",
        "invalid_token",
        "refresh_token_reused",
    }:
        hint = f"; {LOGIN_HINT}"
    message = (
        f"ChatGPT token refresh failed (HTTP {response.status_code}"
        + (f", {code}" if code else "")
        + (f": {detail}" if detail else "")
        + f"){hint}"
    )
    return ChatGPTAuthError(message, code=code or "chatgpt_refresh_failed")


def refresh(
    tokens: dict[str, Any] | None = None,
    *,
    path: str | Path | None = None,
    client: httpx.Client | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Exchange the refresh token for fresh tokens and persist the rotation.

    Refresh tokens are single-use with rotation: if the store already holds a
    different pair (a peer process refreshed first), that pair is adopted
    instead of replaying a consumed token. The whole read -> exchange ->
    publish section is serialized per store (flock), so concurrent callers
    submit the rotating token exactly once, and publication is fenced against
    a logout that removed the store or bumped its generation while the
    response was in flight.
    """
    target = Path(path) if path is not None else auth_path()
    with _store_lock(target):
        generation = credential_generation(target)
        # Reread under the lock: a peer may have rotated between our load and
        # our turn, and replaying a consumed token would end in invalid_grant.
        stored = load_tokens(target)
        current = _normalize_store(tokens) if tokens else stored
        if current is None:
            raise ChatGPTAuthError(
                f"not signed in to ChatGPT: no tokens stored and nothing to refresh; {LOGIN_HINT}",
                code="chatgpt_auth_missing",
            )
        # Peer adoption: a store rotation that we have not seen means our
        # refresh token may already be consumed; use the store's pair when it
        # is usable.
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
            raise ChatGPTAuthError(
                f"ChatGPT credential has no refresh token; {LOGIN_HINT}",
                code="chatgpt_auth_missing_refresh_token",
            )
        snapshot = _store_fingerprint(target)

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
                    "client_id": CODEX_CLIENT_ID,
                },
            )
        except httpx.HTTPError as exc:
            raise ChatGPTAuthError(
                f"ChatGPT token refresh could not reach {TOKEN_URL} "
                f"({type(exc).__name__}); {LOGIN_HINT}",
                code="chatgpt_refresh_unreachable",
            ) from exc
        finally:
            if owns_client:
                http.close()

        if response.status_code == 429:
            raise ChatGPTAuthError(
                "ChatGPT token refresh was rate limited (HTTP 429); credentials are still "
                "valid — retry later.",
                code="chatgpt_refresh_rate_limited",
            )
        if response.status_code != 200:
            raise _refresh_failure(response)

        try:
            payload = response.json()
        except ValueError as exc:
            raise ChatGPTAuthError(
                "ChatGPT token refresh returned invalid JSON (HTTP 200); retry, then "
                f"re-authenticate if it persists; {LOGIN_HINT}",
                code="chatgpt_refresh_invalid_json",
            ) from exc
        if not isinstance(payload, dict) or not payload.get("access_token"):
            raise ChatGPTAuthError(
                f"ChatGPT token refresh response was missing access_token; {LOGIN_HINT}",
                code="chatgpt_refresh_missing_access_token",
            )
        updated = {
            # Rotation: the server's new refresh token wins; otherwise keep ours.
            "access_token": str(payload["access_token"]),
            "refresh_token": str(payload.get("refresh_token") or refresh_token),
            "id_token": str(payload.get("id_token") or current.get("id_token") or ""),
            "account_id": current.get("account_id"),
            "expires_at": None,
            "last_refresh": _utc_now_z(),
        }
        normalized = _normalize_store(updated)
        assert normalized is not None  # access_token is non-empty by construction above
        if credential_generation(target) != generation:
            # A logout invalidated this store while the exchange was in
            # flight: hand the caller the fresh in-memory pair (the running
            # call may use it) but never republish revoked credentials.
            return normalized
        if _store_fingerprint(target) != snapshot:
            # The store changed under us without a generation bump (e.g. a
            # raw file removal): adopt a usable peer pair, otherwise keep the
            # response in memory and leave the missing store missing.
            latest = load_tokens(target)
            if latest is not None and not is_expired(latest):
                return latest
            return normalized
        save_tokens(normalized, target)
        return normalized


def resolve_tokens(
    *,
    path: str | Path | None = None,
    codex_home: str | Path | None = None,
    refresh_when_expiring: bool = True,
    timeout: float = _DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Provider-facing credential resolution: store -> Codex import -> refresh.

    Raises :class:`ChatGPTAuthError` with the login hint when nothing usable
    is available. Importing is part of resolution, so a user who already ran
    ``codex login`` can select ``provider=chatgpt`` with no extra step.
    """
    target = Path(path) if path is not None else auth_path()
    tokens = load_tokens(target)
    if tokens is None:
        tokens = import_from_codex(path=target, codex_home=codex_home)
    if tokens is None:
        raise ChatGPTAuthError(
            "not signed in to ChatGPT: no Machina token store and no Codex CLI "
            f"credentials found; {LOGIN_HINT}",
            code="chatgpt_auth_missing",
        )
    if refresh_when_expiring and is_expired(tokens) and tokens.get("refresh_token"):
        tokens = refresh(tokens, path=target, timeout=timeout)
    return tokens


# ---------------------------------------------------------------------------
# Browser PKCE login (Codex CLI flow; driven by `machina auth login`)
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
    redirect_uri: str = REDIRECT_URI,
    state: str | None = None,
    code_verifier: str | None = None,
) -> LoginRequest:
    """Build the authorization URL + PKCE secrets for the loopback flow."""
    verifier = code_verifier or secrets.token_urlsafe(64)
    flow_state = state or secrets.token_urlsafe(32)
    query = urlencode(
        {
            "response_type": "code",
            "client_id": CODEX_CLIENT_ID,
            "redirect_uri": redirect_uri,
            "scope": SCOPES,
            "code_challenge": _pkce_challenge(verifier),
            "code_challenge_method": "S256",
            "id_token_add_organizations": "true",
            "state": flow_state,
        }
    )
    return LoginRequest(
        url=f"{AUTHORIZE_URL}?{query}",
        state=flow_state,
        code_verifier=verifier,
        redirect_uri=redirect_uri,
    )


def exchange_code(
    code: str,
    *,
    code_verifier: str,
    redirect_uri: str = REDIRECT_URI,
    path: str | Path | None = None,
    client: httpx.Client | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
    generation: int | None = None,
) -> dict[str, Any]:
    """Swap an authorization code (+PKCE verifier) for tokens and persist them.

    Publication is fenced: when a caller passes the *generation* it captured
    when the login flow started (and the store snapshot taken here), a logout
    that landed while the exchange was in flight raises
    ``chatgpt_auth_cancelled`` instead of recreating revoked credentials.
    """
    if not str(code).strip():
        raise ChatGPTAuthError("authorization code is empty", code="chatgpt_code_missing")
    target = Path(path) if path is not None else auth_path()
    if generation is not None and credential_generation(target) != generation:
        # Logout already invalidated this flow: do not even consume the code.
        raise ChatGPTAuthError(
            "ChatGPT sign-in was cancelled before the token exchange completed; "
            f"start it again. {LOGIN_HINT}",
            code="chatgpt_auth_cancelled",
        )
    snapshot = _store_fingerprint(target)
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
                "redirect_uri": redirect_uri,
                "client_id": CODEX_CLIENT_ID,
                "code_verifier": code_verifier,
            },
        )
    except httpx.HTTPError as exc:
        raise ChatGPTAuthError(
            f"ChatGPT token exchange could not reach {TOKEN_URL} "
            f"({type(exc).__name__}); {LOGIN_HINT}",
            code="chatgpt_exchange_unreachable",
        ) from exc
    finally:
        if owns_client:
            http.close()

    if response.status_code == 429:
        raise ChatGPTAuthError(
            "ChatGPT token exchange was rate limited (HTTP 429); retry later.",
            code="chatgpt_exchange_rate_limited",
        )
    if response.status_code != 200:
        code_value, detail = _error_detail(response)
        raise ChatGPTAuthError(
            f"ChatGPT token exchange failed (HTTP {response.status_code}"
            + (f", {code_value}" if code_value else "")
            + (f": {detail}" if detail else "")
            + ").",
            code=code_value or "chatgpt_exchange_failed",
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise ChatGPTAuthError(
            "ChatGPT token exchange returned invalid JSON (HTTP 200).",
            code="chatgpt_exchange_invalid_json",
        ) from exc
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise ChatGPTAuthError(
            "ChatGPT token exchange response was missing access_token.",
            code="chatgpt_exchange_missing_access_token",
        )
    normalized = _normalize_store(payload)
    assert normalized is not None  # access_token is non-empty by the check above
    normalized["last_refresh"] = _utc_now_z()
    with _store_lock(target):
        cancelled = generation is not None and credential_generation(target) != generation
        if not cancelled and _store_fingerprint(target) != snapshot:
            # The store appeared/disappeared/changed while the exchange ran
            # (logout removed it, or another login wrote it): do not clobber.
            cancelled = True
        if cancelled:
            raise ChatGPTAuthError(
                "ChatGPT sign-in was cancelled before the token exchange completed; "
                f"start it again. {LOGIN_HINT}",
                code="chatgpt_auth_cancelled",
            )
        save_tokens(normalized, target)
    return normalized
