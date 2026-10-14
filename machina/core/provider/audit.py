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
        sanitized = re.sub(
            r"(postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://([^:]+):([^@]+)@",
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
        return sanitized
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
                if key.lower() not in protected
                and not key.lower().endswith(("_content", "_preview", "_payload"))
            }
        if isinstance(value, (list, tuple)):
            return [metadata(item) for item in value]
        return value

    sanitized_kwargs = _sanitize_value(metadata(kwargs))
    entry = {
        "timestamp": time.time(),
        "event": event_type,
        **sanitized_kwargs,
    }
    _audit_logger.info(json.dumps(entry, default=str))
    from machina.observability.audit import audit_persistence

    audit_persistence.submit(entry)
