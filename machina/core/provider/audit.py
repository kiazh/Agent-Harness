"""Audit logging — sanitized security events with async persistence."""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

# ---------------------------------------------------------------------------
# Audit logging
# ---------------------------------------------------------------------------
_audit_logger = logging.getLogger("machina.audit")
_audit_logger.setLevel(logging.INFO)
if not _audit_logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s [AUDIT] %(message)s"))
    _audit_logger.addHandler(_handler)


def _sanitize_value(value: Any) -> Any:
    """Sanitize a value for audit logging — redact potential secrets."""
    if isinstance(value, bytes):
        try:
            return _sanitize_value(value.decode("utf-8")).encode("utf-8")
        except UnicodeDecodeError:
            return value
    if isinstance(value, str):
        sanitized = value
        sanitized = re.sub(r"sk-[a-zA-Z0-9]{20,}", "[REDACTED_KEY]", sanitized)
        sanitized = re.sub(r"sk-ant-[a-zA-Z0-9\-_]{20,}", "[REDACTED_KEY]", sanitized)
        sanitized = re.sub(r"sk-or-[a-zA-Z0-9\-_]{20,}", "[REDACTED_KEY]", sanitized)
        sanitized = re.sub(
            r"\b(ghp_[a-zA-Z0-9]{20,}|gho_[a-zA-Z0-9]{20,}|ghu_[a-zA-Z0-9]{20,}"
            r"|ghs_[a-zA-Z0-9]{20,}|github_pat_[a-zA-Z0-9_]{20,})",
            "[REDACTED_KEY]",
            sanitized,
        )
        sanitized = re.sub(r"\bxox[baprs]-[a-zA-Z0-9\-]{10,}", "[REDACTED_KEY]", sanitized)
        sanitized = re.sub(
            r"\beyJ[a-zA-Z0-9\-_]{10,}\.eyJ[a-zA-Z0-9\-_]{10,}\.[a-zA-Z0-9\-_]{10,}",
            "[REDACTED_KEY]",
            sanitized,
        )
        sanitized = re.sub(
            r"(Bearer\s+)[a-zA-Z0-9\-._~+/]+=*",
            r"\1[REDACTED_TOKEN]",
            sanitized,
            flags=re.IGNORECASE,
        )
        sanitized = re.sub(
            r"(password|passwd|pwd)\s*[=:]\s*['\"]?([^\s'\"]{4,})['\"]?",
            r"\1=[REDACTED_PASSWORD]",
            sanitized,
            flags=re.IGNORECASE,
        )
        # Consume the password to the last ``@`` before a path/query/fragment
        # boundary (mirrors ``_URL_USERINFO_RE``) so an embedded ``@`` is fully
        # redacted: the old ``([^@]+)`` stopped at the first ``@`` and the
        # later userinfo pass preserved the ``[REDACTED_DB_PASSWORD]`` marker,
        # leaking the password tail (final-review F1). The username is
        # optional (SEC-3) so a password-only DSN (``redis://:pw@host``) is
        # redacted too.
        sanitized = re.sub(
            r"(postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://([^:]*):([^/\s?#]*)@",
            r"\1://\2:[REDACTED_DB_PASSWORD]@",
            sanitized,
        )
        sanitized = re.sub(r"AKIA[0-9A-Z]{16}", "[REDACTED_AWS_KEY]", sanitized)
        sanitized = re.sub(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            "[REDACTED_PRIVATE_KEY]",
            sanitized,
            flags=re.DOTALL,
        )
        # URL credentials last: earlier patterns already redacted token-shaped
        # and DB-scheme secrets (their markers are preserved, never rewritten).
        # This closes the remainder — URI userinfo passwords and credential
        # query/fragment values of any length or format (R5-N / finding 18).
        return redact_url_credentials(sanitized)
    elif isinstance(value, dict):
        return {
            k: ("[REDACTED]" if _is_sensitive_audit_key(k) else _sanitize_value(v))
            for k, v in value.items()
        }
    elif isinstance(value, list):
        return [_sanitize_value(v) for v in value]
    elif isinstance(value, tuple):
        return tuple(_sanitize_value(v) for v in value)
    elif isinstance(value, set):
        return {_sanitize_value(v) for v in value}
    else:
        return value


# R5-N (finding 18): URI credential redaction for audit output. Token-shaped
# secrets and DB-scheme passwords are handled by the patterns above; these
# cover the remainder — URI userinfo passwords and credential-bearing query /
# fragment parameters of any length or format.

#: ``scheme://user:password@`` — password allows embedded ``@`` (greedy to the
#: last ``@`` before a path/query/fragment boundary); ``/``, whitespace, ``?``
#: and ``#`` still terminate so ports (``host:8080/…``) and IPv6 literals
#: never match. The username is optional (SEC-3): a password-only userinfo
#: (``https://:pw@host``, ``redis://:pw@host``) is still a credential.
_URL_USERINFO_RE = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://[^/\s?#@]*):([^/\s?#]*)@")

#: ``?name=value`` / ``&name=value`` / ``#name=value`` — value stops at the
#: next delimiter or whitespace so surrounding parameters survive verbatim.
_URL_CREDENTIAL_PARAM_RE = re.compile(r"([?&#;])([^?&#;\s=]+)=([^?&#;\s]*)")

#: Query/fragment parameter names whose values are credentials. Normalized
#: (lowercase, non-alphanumeric stripped) before comparison, mirroring
#: :func:`_is_sensitive_audit_key`.
#:
#: ``signature`` covers the presigned-URL forms (SEC-2): AWS
#: ``X-Amz-Signature`` -> ``xamzsignature``, GCS ``X-Goog-Signature`` ->
#: ``xgoogsignature``, and a plain ``signature``; ``X-Amz-Credential`` /
#: ``X-Goog-Credential`` -> ``…credential`` land on the ``credential``
#: suffix below. The bare ``sig`` (Azure SAS) is an *exact* name, not a
#: suffix: a suffix match would wrongly catch benign ``resig``/``insig``.
_CREDENTIAL_PARAM_SUFFIXES = (
    "apikey",
    "token",
    "secret",
    "password",
    "passwd",
    "pwd",
    "credential",
    "privatekey",
    "accesskey",
    "signature",
)
_CREDENTIAL_PARAM_EXACT = frozenset(
    {
        "authorization",
        "auth",
        # Azure SAS signature (exact — a ``sig`` *suffix* would wrongly catch
        # benign ``resig``/``insig``).
        "sig",
    }
)


def _is_credential_param(name: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", name.lower())
    return normalized in _CREDENTIAL_PARAM_EXACT or normalized.endswith(_CREDENTIAL_PARAM_SUFFIXES)


def _redact_userinfo_match(match: re.Match[str]) -> str:
    password = match.group(2)
    if not password or password.startswith("[REDACTED"):
        # Empty or already-redacted (e.g. ``[REDACTED_DB_PASSWORD]`` from the
        # DB-scheme pattern above) — preserve verbatim.
        return match.group(0)
    return f"{match.group(1)}:[REDACTED_URL_PASSWORD]@"


def _redact_credential_param_match(match: re.Match[str]) -> str:
    value = match.group(3)
    if not value or value.startswith("[REDACTED"):
        return match.group(0)
    if not _is_credential_param(match.group(2)):
        return match.group(0)
    return f"{match.group(1)}{match.group(2)}=[REDACTED_CREDENTIAL]"


def redact_url_credentials(text: str) -> str:
    """Redact URI userinfo passwords and credential query/fragment values.

    Shared by audit emission (:func:`_sanitize_value`), the RAG pipeline's
    audit call sites, and audit persistence. The username, host, path, and
    non-credential parameters are preserved for debuggability; a bare
    ``user@host`` (no password) is an identifier, not a credential, and is
    left intact.
    """
    redacted = _URL_USERINFO_RE.sub(_redact_userinfo_match, text)
    return _URL_CREDENTIAL_PARAM_RE.sub(_redact_credential_param_match, redacted)


def _is_sensitive_audit_key(key: Any) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
    return normalized == "authorization" or normalized.endswith(
        (
            "apikey",
            "token",
            "secret",
            "password",
            "passwd",
            "pwd",
            "credential",
            "privatekey",
            "accesskey",
        )
    )


def audit_log(event_type: str, **kwargs) -> None:
    """Log a security-relevant event as JSON. All kwargs are sanitized to redact secrets."""
    protected = {
        "prompt",
        "completion",
        "content",
        "query",
        "query_text",
        "search_text",
        "response",
        "result",
        "tool_args",
        "args",
        "payload",
        "messages",
        "task",
        "preview",
        "result_preview",
        "validation_reason",
        "error",
        "message",
    }

    def metadata(value):
        if isinstance(value, dict):
            return {
                key: metadata(item)
                for key, item in value.items()
                if str(key).lower() not in protected
                and not str(key).lower().endswith(("_content", "_preview", "_payload"))
            }
        if isinstance(value, (list, tuple)):
            return [metadata(item) for item in value]
        return value

    sanitized_kwargs = _sanitize_value(metadata(kwargs))
    # ``event`` / ``timestamp`` are reserved: a caller-supplied kwarg of the
    # same name must never rewrite the audit record's identity (final-review
    # S6/F3). Filter the reserved keys, then set the authoritative values.
    reserved = {k: v for k, v in sanitized_kwargs.items() if k not in ("event", "timestamp")}
    entry = {
        "timestamp": time.time(),
        "event": event_type,
        **reserved,
    }
    _audit_logger.info(json.dumps(entry, default=str))
    from machina.observability.audit import audit_persistence

    audit_persistence.submit(entry)
