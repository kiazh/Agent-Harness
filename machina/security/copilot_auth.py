"""GitHub Copilot subscription credentials — device flow + ``gh`` import.

Machina can run turns on a GitHub Copilot seat against the same public API the
Copilot CLI/VS Code use (``https://api.githubcopilot.com``). The credential is
a GitHub user token (``gho_`` / ``ghu_`` / fine-grained ``github_pat_``); the
API accepts it directly as a Bearer token — the legacy
``api.github.com/copilot_internal/v2/token`` JWT exchange answers HTTP 403 for
non-first-party clients (probed live 2026-10-14) and is not used. Tokens live
in ``~/.machina/auth/copilot.json`` (0600, atomic replace);
``import_from_gh_cli`` reads the token ``gh auth token`` already holds
(read-only; the keyring is never modified).

The interactive login is GitHub's standard device flow with VS Code's public
GitHub App client id (published in OSS; the same id the reference Hermes
implementation uses), so no browser listener and no client secret are needed.

No token value is ever logged or embedded in an error message; callers get
:class:`CopilotAuthError` with a stable ``code`` and an actionable hint.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from machina.core.config import config
from machina.core.exceptions import ProviderError

# VS Code's public GitHub App client id (device flow; no secret).
COPILOT_CLIENT_ID = "Iv1.b507a08c87ecfe98"
DEVICE_CODE_URL = "https://github.com/login/device/code"
ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
USER_URL = "https://api.github.com/user"
DEVICE_SCOPE = "read:user"
DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
DEVICE_POLL_INTERVAL = 5.0
DEVICE_POLL_SAFETY_MARGIN = 3.0
DEFAULT_DEVICE_TIMEOUT = 300.0

# Token families the Copilot API exchanges; classic PATs (ghp_*) are rejected.
SUPPORTED_PREFIXES = ("gho_", "ghu_", "github_pat_")

AUTH_FILE_ENV = "MACHINA_COPILOT_AUTH_FILE"
LOGIN_HINT = "run `gh auth login` (then `machina auth copilot login --import`) or `machina auth copilot login`"
_STORE_FIELDS = ("token", "account", "token_family", "last_refresh")
_DEFAULT_TIMEOUT = 20.0


class CopilotAuthError(ProviderError):
    """Typed Copilot auth failure. Carries a stable ``code``, never tokens."""

    def __init__(self, message: str, *, code: str = "copilot_auth_error") -> None:
        super().__init__(message)
        self.code = code


def auth_path() -> Path:
    """Resolve the Machina Copilot token file without touching the disk."""
    override = os.environ.get(AUTH_FILE_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    home = ""
    with suppress(Exception):
        home = str(config.get("machina_home") or "")
    root = Path(home).expanduser() if home else Path.home() / ".machina"
    return root / "auth" / "copilot.json"


def _utc_now_z() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def token_family(token: Any) -> str:
    """Classify a GitHub token: ``gho``, ``ghu``, ``pat``, ``classic`` or ``unknown``."""
    value = str(token or "").strip()
    if value.startswith("gho_"):
        return "gho"
    if value.startswith("ghu_"):
        return "ghu"
    if value.startswith("github_pat_"):
        return "pat"
    if value.startswith("ghp_"):
        return "classic"
    return "unknown"


def validate_token(token: str) -> tuple[bool, str]:
    """Usability of a GitHub token with the Copilot API (hermes parity)."""
    value = str(token or "").strip()
    if not value:
        return False, "empty token"
    family = token_family(value)
    if family == "classic":
        return False, (
            "classic personal access tokens (ghp_*) are not supported by the Copilot API; "
            "use `gh auth login` (gho_*), the device flow, or a fine-grained github_pat_*"
        )
    if family == "unknown":
        return False, (
            "unsupported GitHub token format for Copilot; expected gho_*, ghu_* or github_pat_*"
        )
    return True, "ok"


def _normalize_store(raw: Any) -> dict[str, Any] | None:
    """Shape a stored payload into the Machina token dict."""
    if not isinstance(raw, dict):
        return None
    token = raw.get("token") or raw.get("access_token")
    if not isinstance(token, str) or not token.strip():
        return None
    value = token.strip()
    account = raw.get("account")
    last_refresh = raw.get("last_refresh")
    return {
        "token": value,
        "account": account.strip() if isinstance(account, str) and account.strip() else None,
        "token_family": token_family(value),
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


# ---------------------------------------------------------------------------
# gh CLI import (read-only: `gh auth token`)
# ---------------------------------------------------------------------------


def _probe_gh_token() -> str | None:
    """Token from ``gh auth token``; the gh store itself is never modified.

    The probe runs with ``GITHUB_TOKEN``/``GH_TOKEN`` stripped (so gh cannot
    short-circuit on an env token) and with prompts disabled, bounded by a
    5 second timeout.
    """
    env = {
        key: value for key, value in os.environ.items() if key not in {"GITHUB_TOKEN", "GH_TOKEN"}
    }
    env.setdefault("GH_PROMPT_DISABLED", "1")
    env.setdefault("GH_NO_UPDATE_NOTIFIER", "1")
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            env=env,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout.strip()


def fetch_account(
    token: str, *, client: httpx.Client | None = None, timeout: float = _DEFAULT_TIMEOUT
) -> str | None:
    """Best-effort GitHub login for *token* (display metadata only)."""
    http, owns_client = _client_or_new(client, timeout)
    try:
        response = http.get(
            USER_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "machina",
            },
        )
        if response.status_code != 200:
            return None
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    finally:
        if owns_client:
            http.close()
    login = payload.get("login") if isinstance(payload, dict) else None
    return login.strip() if isinstance(login, str) and login.strip() else None


def import_from_gh_cli(
    *,
    path: str | Path | None = None,
    force: bool = False,
    fetch_login: bool = False,
    client: httpx.Client | None = None,
) -> dict[str, Any] | None:
    """Copy the token ``gh auth token`` holds into the Machina store.

    The gh credential store is only read. Existing Machina tokens win unless
    *force* is set. A token gh holds that the Copilot API cannot use (a
    classic PAT) raises the typed error instead of storing something broken.
    """
    target = Path(path) if path is not None else auth_path()
    if not force and load_tokens(target) is not None:
        return None
    token = _probe_gh_token()
    if not token:
        return None
    valid, message = validate_token(token)
    if not valid:
        raise CopilotAuthError(
            f"the token from `gh auth token` is not usable with Copilot: {message}",
            code="copilot_token_unsupported",
        )
    account = fetch_account(token, client=client) if fetch_login else None
    save_tokens({"token": token, "account": account}, target)
    return load_tokens(target)


def status(*, path: str | Path | None = None, probe_gh: bool = True) -> dict[str, Any]:
    """Read-only status: signed in?, token family, account, source. Never tokens.

    ``probe_gh`` runs ``gh auth token`` when no Machina store exists, so the
    status is honest before the first call; it never imports.
    """
    result: dict[str, Any] = {
        "signed_in": False,
        "source": None,
        "token_family": None,
        "account": None,
        "last_refresh": None,
    }
    tokens = load_tokens(path)
    source = "machina"
    if tokens is None and probe_gh:
        token = _probe_gh_token()
        if token:
            tokens = _normalize_store({"token": token})
            source = "gh"
    if tokens is None:
        return result
    result.update(
        signed_in=True,
        source=source,
        token_family=tokens.get("token_family"),
        account=tokens.get("account"),
        last_refresh=tokens.get("last_refresh"),
    )
    return result


# ---------------------------------------------------------------------------
# Device flow (GitHub OAuth 2.0 device authorization grant)
# ---------------------------------------------------------------------------


def _client_or_new(client: httpx.Client | None, timeout: float) -> tuple[httpx.Client, bool]:
    if client is not None:
        return client, False
    return httpx.Client(
        timeout=httpx.Timeout(float(timeout), connect=min(10.0, float(timeout)))
    ), True


def _form_headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "machina",
    }


def device_flow_start(
    *,
    client: httpx.Client | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Request a device + user code; returns the fields the CLI displays."""
    http, owns_client = _client_or_new(client, timeout)
    try:
        response = http.post(
            DEVICE_CODE_URL,
            headers=_form_headers(),
            content=urlencode({"client_id": COPILOT_CLIENT_ID, "scope": DEVICE_SCOPE}),
        )
    except httpx.HTTPError as exc:
        raise CopilotAuthError(
            f"GitHub device authorization could not reach {DEVICE_CODE_URL} "
            f"({type(exc).__name__}); retry when the network is available",
            code="copilot_device_unreachable",
        ) from exc
    finally:
        if owns_client:
            http.close()
    if response.status_code != 200:
        raise CopilotAuthError(
            f"GitHub device authorization failed (HTTP {response.status_code}).",
            code="copilot_device_failed",
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise CopilotAuthError(
            "GitHub device authorization returned invalid JSON.",
            code="copilot_device_invalid_json",
        ) from exc
    if (
        not isinstance(payload, dict)
        or not payload.get("device_code")
        or not payload.get("user_code")
    ):
        raise CopilotAuthError(
            "GitHub device authorization response was missing device_code/user_code.",
            code="copilot_device_missing_fields",
        )
    return payload


def device_flow_poll(
    device_code: str,
    *,
    interval: float = DEVICE_POLL_INTERVAL,
    timeout: float = DEFAULT_DEVICE_TIMEOUT,
    client: httpx.Client | None = None,
    sleep: Any = time.sleep,
) -> str:
    """Poll the access-token endpoint until the user authorizes or it expires."""
    http, owns_client = _client_or_new(client, _DEFAULT_TIMEOUT)
    deadline = time.monotonic() + max(1.0, float(timeout))
    wait = max(1.0, float(interval)) + DEVICE_POLL_SAFETY_MARGIN
    try:
        while time.monotonic() < deadline:
            sleep(wait)
            try:
                response = http.post(
                    ACCESS_TOKEN_URL,
                    headers=_form_headers(),
                    content=urlencode(
                        {
                            "client_id": COPILOT_CLIENT_ID,
                            "device_code": device_code,
                            "grant_type": DEVICE_GRANT_TYPE,
                        }
                    ),
                )
            except httpx.HTTPError:
                continue
            try:
                payload = response.json()
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            token = payload.get("access_token")
            if isinstance(token, str) and token.strip():
                valid, message = validate_token(token)
                if not valid:
                    raise CopilotAuthError(
                        f"GitHub returned a token the Copilot API cannot use: {message}",
                        code="copilot_token_unsupported",
                    )
                return token.strip()
            error = str(payload.get("error") or "")
            if error == "slow_down":
                server_interval = payload.get("interval")
                if isinstance(server_interval, (int, float)) and server_interval > 0:
                    wait = float(server_interval)
                else:
                    wait += 5.0
            elif error in ("expired_token", "access_denied"):
                raise CopilotAuthError(
                    f"GitHub device authorization {error.replace('_', ' ')}; run the login again",
                    code=f"copilot_device_{error}",
                )
            elif error and error != "authorization_pending":
                raise CopilotAuthError(
                    f"GitHub device authorization failed: {error}",
                    code="copilot_device_failed",
                )
    finally:
        if owns_client:
            http.close()
    raise CopilotAuthError(
        "GitHub device authorization timed out; run the login again",
        code="copilot_device_timeout",
    )


def resolve_tokens(
    *,
    path: str | Path | None = None,
    import_when_missing: bool = True,
) -> dict[str, Any]:
    """Provider-facing credential resolution: store -> gh CLI import.

    Raises :class:`CopilotAuthError` with the login hint when nothing usable
    is available. There is no refresh: the GitHub token is long-lived, and a
    revoked one surfaces as a typed 401 from the API call.
    """
    target = Path(path) if path is not None else auth_path()
    tokens = load_tokens(target)
    if tokens is None and import_when_missing:
        tokens = import_from_gh_cli(path=target)
    if tokens is None:
        raise CopilotAuthError(
            "not signed in to Copilot: no Machina token store and no gh CLI "
            f"credential found; {LOGIN_HINT}",
            code="copilot_auth_missing",
        )
    valid, message = validate_token(tokens["token"])
    if not valid:
        raise CopilotAuthError(
            f"the stored Copilot credential is unusable: {message}; {LOGIN_HINT}",
            code="copilot_token_unsupported",
        )
    return tokens
