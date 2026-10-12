"""Validated connector configuration, status, and result contracts.

No raw tokens ever live in these objects: stdio secret *values* are
resolved from a caller-supplied mapping at launch time, and HTTP credentials
are referenced by profile id only. `public_view()` is what listings,
logs, and audit payloads may carry.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import urlparse

# Catalog/context bounds (see docs/plans/MCP_CONNECTORS_PLAN.md).
MAX_CATALOG_TOOLS = 200
MAX_SCHEMA_DEPTH = 8
MAX_DESCRIPTION_CHARS = 2000
MAX_RESULT_BYTES = 256_000
MAX_STDERR_BYTES = 64_000
MAX_TOOL_NAME_CHARS = 128
MAX_ARGV_ENTRIES = 64
MAX_ARGV_ENTRY_CHARS = 1024

_CONNECTOR_ID_RE = re.compile(r"[a-z0-9][a-z0-9\-_]{0,63}")
_ENV_NAME_RE = re.compile(r"[A-Z_][A-Z0-9_]*")
_TOOL_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\-]{0,127}")
_SHELL_METACHARS = re.compile(r"[;&|$`\n]")


class Transport(enum.StrEnum):
    STDIO = "stdio"
    STREAMABLE_HTTP = "streamable_http"


class Trust(enum.StrEnum):
    UNTRUSTED = "untrusted"
    TRUSTED_READ = "trusted_read"


class ConnectionState(enum.StrEnum):
    DISABLED = "disabled"
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DEGRADED = "degraded"
    AUTH_REQUIRED = "auth_required"
    ERROR = "error"


class ConnectorError(Exception):
    """Base error for connector failures (sanitized before display)."""


class ConfigError(ConnectorError):
    """Connector configuration rejected before persistence."""


class ConnectionFailed(ConnectorError):
    """Connect/initialization/discovery failed or timed out."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class ConnectorToolError(ConnectorError):
    """A remote tool call failed, timed out, or returned an unknown outcome."""


class AuthRequired(ConnectorError):
    """Credentials missing, expired, or revoked; needs human reauthorization."""


@dataclass
class ConnectorConfig:
    """Validated, non-secret connector configuration."""

    id: str
    display_name: str = ""
    kind: str = "mcp"
    enabled: bool = True
    owner: str = ""
    transport: str = Transport.STDIO.value
    # Stdio: executable + argv as separate fields (never a shell string).
    command: str = ""
    argv: list[str] = field(default_factory=list)
    cwd: str = ""
    env_allowlist: list[str] = field(default_factory=list)
    secret_refs: list[str] = field(default_factory=list)
    # HTTP transport.
    endpoint: str = ""
    credential_profile: str = ""
    local_access: bool = False
    oauth: dict[str, Any] | None = None
    # Bounds and policy.
    connect_timeout: float = 15.0
    call_timeout: float = 60.0
    discovery_timeout: float = 20.0
    shutdown_timeout: float = 10.0
    result_limit: int = MAX_RESULT_BYTES
    concurrency: int = 4
    max_retries: int = 3
    backoff_base: float = 0.5
    tool_allowlist: list[str] = field(default_factory=list)
    trust: str = Trust.UNTRUSTED.value
    revision: int = 0

    def validate(self) -> ConnectorConfig:
        """Reject unknown fields is the caller's job (strict constructors);
        this rejects invalid values before persistence."""
        for key in ("enabled", "local_access"):
            if type(getattr(self, key)) is not bool:
                raise ConfigError(f"{key} must be a boolean")
        for key in ("result_limit", "concurrency", "max_retries", "revision"):
            if type(getattr(self, key)) is not int:
                raise ConfigError(f"{key} must be an integer")
        for key in (
            "id",
            "display_name",
            "kind",
            "owner",
            "transport",
            "command",
            "cwd",
            "endpoint",
            "credential_profile",
            "trust",
        ):
            if not isinstance(getattr(self, key), str):
                raise ConfigError(f"{key} must be a string")
        for key in ("argv", "env_allowlist", "secret_refs", "tool_allowlist"):
            value = getattr(self, key)
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                raise ConfigError(f"{key} must be a list of strings")
        if not _CONNECTOR_ID_RE.fullmatch(self.id or ""):
            raise ConfigError("connector id must match [a-z0-9][a-z0-9-_]{0,63}")
        if len(self.display_name) > 80:
            raise ConfigError("display_name must be at most 80 characters")
        if len(self.owner) > 80:
            raise ConfigError("owner must be at most 80 characters")
        if self.kind != "mcp":
            raise ConfigError("connector kind must be 'mcp'")
        if self.transport == Transport.STDIO.value:
            absolute_command = (
                PurePosixPath(self.command).is_absolute()
                or PureWindowsPath(self.command).is_absolute()
            )
            if (
                not self.command
                or _SHELL_METACHARS.search(self.command)
                or (re.search(r"\s", self.command) and not absolute_command)
            ):
                raise ConfigError("stdio command must be a plain executable (no shell syntax)")
            if len(self.argv) > MAX_ARGV_ENTRIES or any(
                len(a) > MAX_ARGV_ENTRY_CHARS for a in self.argv
            ):
                raise ConfigError("stdio argv exceeds bounds")
            if self.cwd and not (
                PurePosixPath(self.cwd).is_absolute() or PureWindowsPath(self.cwd).is_absolute()
            ):
                raise ConfigError("stdio cwd must be an absolute path")
            if self.endpoint:
                raise ConfigError("endpoint is HTTP-only; stdio connectors must not set it")
        elif self.transport == Transport.STREAMABLE_HTTP.value:
            parts = urlparse(self.endpoint)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                raise ConfigError("endpoint must be an http(s) URL with a host")
            if (
                parts.username is not None
                or parts.password is not None
                or parts.query
                or parts.fragment
            ):
                raise ConfigError(
                    "endpoint must not embed user credentials, query parameters, or fragments"
                )
            if self.command or self.argv:
                raise ConfigError("command/argv are stdio-only; HTTP connectors must not set them")
            if len(self.credential_profile) > 80:
                raise ConfigError("credential_profile must be at most 80 characters")
        else:
            raise ConfigError("transport must be 'stdio' or 'streamable_http'")
        for name in (*self.env_allowlist, *self.secret_refs):
            if not _ENV_NAME_RE.fullmatch(name or ""):
                raise ConfigError(f"invalid environment/secret name: {name!r}")
        for tool in self.tool_allowlist:
            if not _TOOL_NAME_RE.fullmatch(tool or ""):
                raise ConfigError(f"invalid allowlisted tool name: {tool!r}")
        for key in (
            "connect_timeout",
            "call_timeout",
            "discovery_timeout",
            "shutdown_timeout",
            "backoff_base",
        ):
            value = getattr(self, key)
            if type(value) not in (int, float) or not 0 < value <= 600:
                raise ConfigError(f"{key} must be within (0, 600] seconds")
        if not 1 <= self.result_limit <= 1_048_576:
            raise ConfigError("result_limit must be within [1, 1048576] bytes")
        if not 1 <= self.concurrency <= 16:
            raise ConfigError("concurrency must be within [1, 16]")
        if not 0 <= self.max_retries <= 5:
            raise ConfigError("max_retries must be within [0, 5]")
        if self.trust not in (Trust.UNTRUSTED.value, Trust.TRUSTED_READ.value):
            raise ConfigError("trust must be 'untrusted' or 'trusted_read'")
        if not isinstance(self.revision, int) or self.revision < 0:
            raise ConfigError("revision must be a non-negative integer")
        self._validate_oauth()
        return self

    def _validate_oauth(self) -> None:
        from ah.connectors.oauth_models import OAuthConfigError, OAuthSettings

        if self.oauth is None:
            return
        if not isinstance(self.oauth, dict):
            raise ConfigError("oauth must be an object")
        if self.transport == Transport.STDIO.value:
            raise ConfigError("oauth is HTTP-only; stdio connectors must not set it")
        if self.credential_profile:
            raise ConfigError("oauth and credential_profile are mutually exclusive")
        try:
            OAuthSettings.from_dict(self.oauth)
        except OAuthConfigError as exc:
            raise ConfigError(str(exc)) from None

    def public_view(self) -> dict[str, Any]:
        """Sanitized summary safe for listings, logs, and audit payloads."""
        oauth_view: dict[str, Any] | None = None
        if isinstance(self.oauth, dict):
            from ah.connectors.oauth_models import OAuthSettings

            try:
                settings = OAuthSettings.from_dict(self.oauth)
                oauth_view = settings.to_dict() if settings is not None else None
            except Exception:
                oauth_view = None
        return {
            "id": self.id,
            "display_name": self.display_name,
            "kind": self.kind,
            "enabled": self.enabled,
            "owner": self.owner,
            "transport": self.transport,
            "command": self.command if self.transport == Transport.STDIO.value else "",
            "endpoint": self.endpoint if self.transport == Transport.STREAMABLE_HTTP.value else "",
            "credential_profile": self.credential_profile,
            "local_access": self.local_access,
            "trust": self.trust,
            "revision": self.revision,
            "tool_allowlist": list(self.tool_allowlist),
            "oauth": oauth_view,
        }


@dataclass
class DiscoveredTool:
    """One namespaced tool from a connector catalog."""

    connector_id: str
    name: str  # stable model-facing name: <connector>__<tool>
    remote_name: str  # original server tool name
    description: str = ""
    schema: dict[str, Any] = field(default_factory=dict)
    schema_revision: int = 0


@dataclass
class CallOutcome:
    """Typed result of one remote tool call (never raw server text alone)."""

    ok: bool
    text: str = ""
    structured: Any = None
    truncated: bool = False
    outcome_unknown: bool = False
    error: str = ""
