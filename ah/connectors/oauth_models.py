"""OAuth contracts: validated settings, immutable review/binding types, reason codes.

Non-secret by construction. No tokens, codes, verifiers, or client secrets
live in these objects. Secret material lives in oauth_store records only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

# ── reason codes (section 12.1) ──────────────────────────────────────────
REASON_CODES = frozenset(
    {
        "oauth_not_configured",
        "oauth_discovery_failed",
        "oauth_unsafe_destination",
        "oauth_issuer_mismatch",
        "oauth_resource_mismatch",
        "oauth_pkce_unsupported",
        "oauth_client_configuration_required",
        "oauth_registration_failed",
        "oauth_scope_not_allowed",
        "oauth_callback_invalid",
        "oauth_cancelled",
        "oauth_expired",
        "oauth_exchange_failed",
        "oauth_refresh_failed",
        "oauth_reauthorization_required",
        "oauth_storage_unavailable",
        "oauth_config_changed",
        "oauth_operation_not_found",
        "oauth_local_browser_unavailable",
        "oauth_redirect_unsupported",
    }
)

# ── named bounds (section 12.2) ──────────────────────────────────────────
OAUTH_PREVIEW_DISCOVERY_SECONDS = 20.0
OAUTH_METADATA_FETCH_SECONDS = 3.0
OAUTH_METADATA_MAX_BYTES = 128 * 1024
OAUTH_TOKEN_MAX_BYTES = 64 * 1024
OAUTH_PREVIEW_TTL_SECONDS = 120.0
OAUTH_OPERATION_TTL_SECONDS = 300.0
OAUTH_EXCHANGE_SECONDS = 10.0
OAUTH_CALLBACK_MAX_BYTES = 8 * 1024
OAUTH_CALLBACK_MAX_PARAMS = 16
OAUTH_CALLBACK_PARAM_MAX = 4 * 1024
OAUTH_CALLBACK_READ_SECONDS = 3.0
OAUTH_CALLBACK_MAX_SOCKETS = 8
OAUTH_MAX_ISSUERS = 8
OAUTH_MAX_SCOPES = 64
OAUTH_SCOPE_MAX_CHARS = 256
OAUTH_TOKEN_MAX_CHARS = 16 * 1024
OAUTH_GRANT_FILE_MAX_BYTES = 256 * 1024
OAUTH_INDEX_MAX_BYTES = 1 * 1024 * 1024
OAUTH_LOCK_SECONDS = 5.0
OAUTH_MAX_OPERATIONS = 8
OAUTH_TERMINAL_RETENTION_SECONDS = 60.0
OAUTH_REFRESH_SKEW_SECONDS = 60.0

_CLIENT_MODES = ("auto", "preregistered", "metadata_document")
_AUTH_METHODS = ("none", "client_secret_basic")
_SCOPE_RE = re.compile(r"[!-~]+")
_ENV_NAME_RE = re.compile(r"[A-Z_][A-Z0-9_]*")
_SECRET_KEYS = frozenset(
    {
        "access_token",
        "refresh_token",
        "client_secret",
        "code_verifier",
        "code",
        "token",
        "authorization",
        "secret",
    }
)


class OAuthConfigError(ValueError):
    """Raised for invalid OAuth settings before persistence."""


def _reject_secret_sentinel(mapping: dict[str, Any]) -> None:
    for key in mapping:
        if str(key).lower() in _SECRET_KEYS:
            raise OAuthConfigError(f"oauth field {key!r} must not carry secret material")


@dataclass(frozen=True)
class OAuthSettings:
    client_mode: str = "auto"
    client_id: str = ""
    client_metadata_url: str = ""
    client_secret_ref: str = ""
    token_endpoint_auth_method: str = "none"
    issuer: str = ""
    scope_allowlist: tuple[str, ...] = ()
    callback_port: int = 0

    @classmethod
    def from_dict(cls, fields: dict[str, Any] | None) -> OAuthSettings | None:
        if fields is None:
            return None
        if not isinstance(fields, dict):
            raise OAuthConfigError("oauth must be an object")
        unknown = set(fields) - {
            "client_mode",
            "client_id",
            "client_metadata_url",
            "client_secret_ref",
            "token_endpoint_auth_method",
            "issuer",
            "scope_allowlist",
            "callback_port",
        }
        if unknown:
            raise OAuthConfigError(f"unsupported oauth fields: {sorted(unknown)}")
        _reject_secret_sentinel(fields)
        mode = fields.get("client_mode", "auto")
        auth_method = fields.get("token_endpoint_auth_method", "none")
        if mode not in _CLIENT_MODES:
            raise OAuthConfigError("client_mode must be auto|preregistered|metadata_document")
        if auth_method not in _AUTH_METHODS:
            raise OAuthConfigError("token_endpoint_auth_method must be none|client_secret_basic")
        client_id = fields.get("client_id", "")
        metadata_url = fields.get("client_metadata_url", "")
        secret_ref = fields.get("client_secret_ref", "")
        issuer = fields.get("issuer", "")
        scopes = fields.get("scope_allowlist", [])
        port = fields.get("callback_port", 0)
        if not isinstance(client_id, str) or not isinstance(metadata_url, str):
            raise OAuthConfigError("client_id/client_metadata_url must be strings")
        if not isinstance(secret_ref, str) or not isinstance(issuer, str):
            raise OAuthConfigError("client_secret_ref/issuer must be strings")
        if len(client_id) > 2048:
            raise OAuthConfigError("client_id must be at most 2048 characters")
        if any(ord(c) < 32 or ord(c) == 127 for c in client_id):
            raise OAuthConfigError("client_id must not contain control characters")
        if mode == "preregistered" and not client_id:
            raise OAuthConfigError("preregistered mode requires client_id")
        if mode != "preregistered" and secret_ref:
            raise OAuthConfigError("client_secret_ref requires preregistered client mode")
        if auth_method == "client_secret_basic" and (mode != "preregistered" or not secret_ref):
            raise OAuthConfigError("client_secret_basic requires preregistered + secret ref")
        if secret_ref and not _ENV_NAME_RE.fullmatch(secret_ref):
            raise OAuthConfigError(f"invalid secret name: {secret_ref!r}")
        if mode == "metadata_document":
            if not metadata_url:
                raise OAuthConfigError("metadata_document mode requires client_metadata_url")
        if metadata_url:
            parts = urlparse(metadata_url)
            if (
                parts.scheme != "https"
                or not parts.hostname
                or parts.username
                or parts.password
                or parts.query
                or parts.fragment
                or (parts.path or "/") == "/"
            ):
                raise OAuthConfigError(
                    "client_metadata_url must be https with a non-root path, no userinfo/query/fragment"
                )
        elif mode == "metadata_document":
            raise OAuthConfigError("metadata_document mode requires client_metadata_url")
        if mode != "metadata_document" and metadata_url and mode == "auto":
            # Allowed: operator may pre-supply a document URL in auto mode? No:
            # auto must not carry a document URL to keep selection explicit.
            raise OAuthConfigError("client_metadata_url requires metadata_document mode")
        if issuer:
            iparts = urlparse(issuer)
            if iparts.scheme != "https" or not iparts.hostname:
                # Explicit literal-loopback fixture exception handled by network
                # layer; config-level issuer must be https.
                if not (
                    iparts.scheme == "http"
                    and iparts.hostname in ("127.0.0.1", "::1", "::ffff:127.0.0.1")
                ):
                    raise OAuthConfigError("issuer must be an https URL")
            if iparts.username or iparts.password or iparts.fragment:
                raise OAuthConfigError("issuer must not embed userinfo or fragments")
        if not isinstance(scopes, list) or any(not isinstance(s, str) for s in scopes):
            raise OAuthConfigError("scope_allowlist must be a list of strings")
        if len(scopes) > OAUTH_MAX_SCOPES:
            raise OAuthConfigError("scope_allowlist exceeds 64 scopes")
        for scope in scopes:
            if not scope or len(scope) > OAUTH_SCOPE_MAX_CHARS or not _SCOPE_RE.fullmatch(scope):
                raise OAuthConfigError(f"invalid scope token: {scope!r}")
        if type(port) is not int:
            raise OAuthConfigError("callback_port must be an integer")
        if port != 0 and not 1024 <= port <= 65535:
            raise OAuthConfigError("callback_port must be 0 or within 1024-65535")
        return cls(
            client_mode=mode,
            client_id=client_id,
            client_metadata_url=metadata_url,
            client_secret_ref=secret_ref,
            token_endpoint_auth_method=auth_method,
            issuer=issuer,
            scope_allowlist=tuple(scopes),
            callback_port=port,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "client_mode": self.client_mode,
            "client_id": self.client_id,
            "client_metadata_url": self.client_metadata_url,
            "client_secret_ref": self.client_secret_ref,
            "token_endpoint_auth_method": self.token_endpoint_auth_method,
            "issuer": self.issuer,
            "scope_allowlist": list(self.scope_allowlist),
            "callback_port": self.callback_port,
        }


@dataclass(frozen=True)
class OAuthOwner:
    session_id: str
    agent_id: str


@dataclass(frozen=True)
class OAuthBinding:
    connector_id: str
    credential_key: str
    resource: str
    issuer: str
    client_id: str


@dataclass(frozen=True)
class DiscoveredOAuth:
    resource: str
    issuers: tuple[str, ...]
    selected_issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str
    revocation_endpoint: str
    scopes: tuple[str, ...]
    require_iss: bool
    supports_s256: bool
    fingerprint: str


@dataclass(frozen=True)
class OAuthReviewSnapshot:
    connector_id: str
    config_revision: int
    resource: str
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str
    revocation_endpoint: str
    redirect_uri: str
    requested_scopes: tuple[str, ...]
    registration_mode: str
    client_id: str
    fingerprint: str


@dataclass(frozen=True)
class RegisteredOAuthClient:
    client_id: str
    token_endpoint_auth_method: str
    redirect_uri: str


@dataclass
class OAuthCredentialRecord:
    version: int = 1
    credential_key: str = ""
    connector_id: str = ""
    config_revision: int = 0
    resource: str = ""
    issuer: str = ""
    client_id: str = ""
    client_secret_ref: str = ""
    authorization_endpoint: str = ""
    token_endpoint: str = ""
    revocation_endpoint: str = ""
    redirect_uri_used: str = ""
    token_endpoint_auth_method: str = "none"
    grant_id: str = ""
    generation: int = 0
    approved_scopes: tuple[str, ...] = ()
    granted_scopes: tuple[str, ...] = ()
    access_token: str = field(default="", repr=False)
    refresh_token: str = field(default="", repr=False)
    acquired_at_utc: str = ""
    expires_at_utc: str = ""
    refresh_state: str = "idle"

    def __repr__(self) -> str:  # safe repr: no token bytes
        return (
            f"OAuthCredentialRecord(connector_id={self.connector_id!r}, "
            f"issuer={self.issuer!r}, grant_id={self.grant_id!r}, "
            f"generation={self.generation!r}, refresh_state={self.refresh_state!r})"
        )


@dataclass(frozen=True)
class OAuthCallbackResult:
    ok: bool
    code: str = field(default="", repr=False)
    state: str = field(default="", repr=False)
    issuer: str = ""
    error: str = ""
    reason: str = ""
