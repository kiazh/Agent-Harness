"""Secret redaction — detect and redact sensitive data before storing in memory.

Patterns covered:
- API keys (OpenAI, Anthropic, Cohere, etc.)
- Bearer tokens
- Password assignments
- Private keys (PEM blocks)
- Database connection strings with credentials
- AWS access keys
- GitHub tokens
- Slack tokens
- Credit card numbers
- SSN patterns
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import NamedTuple

__all__ = [
    "RedactionPattern",
    "RedactionResult",
    "SecretRedactor",
    "redact_secrets",
]


class RedactionPattern(NamedTuple):
    """A named regex pattern for secret detection."""
    name: str
    pattern: re.Pattern[str]
    replacement: str


@dataclass
class RedactionResult:
    """Result of a redaction operation."""
    text: str
    redactions: list[str] = field(default_factory=list)

    @property
    def was_redacted(self) -> bool:
        return len(self.redactions) > 0


# ─── Regex patterns for common secret formats ───────────────────────────────

_PATTERNS: list[RedactionPattern] = [
    # API keys — various providers
    RedactionPattern(
        name="openai_api_key",
        pattern=re.compile(r"sk-[a-zA-Z0-9]{20,}"),
        replacement="[REDACTED_OPENAI_KEY]",
    ),
    RedactionPattern(
        name="anthropic_api_key",
        pattern=re.compile(r"sk-ant-[a-zA-Z0-9\-_]{20,}"),
        replacement="[REDACTED_ANTHROPIC_KEY]",
    ),
    RedactionPattern(
        name="cohere_api_key",
        pattern=re.compile(r"cohere_api_key\s*[=:]\s*['\"]?([a-zA-Z0-9\-_]{20,})['\"]?", re.IGNORECASE),
        replacement="cohere_api_key=[REDACTED_COHERE_KEY]",
    ),
    RedactionPattern(
        name="openrouter_api_key",
        pattern=re.compile(r"sk-or-[a-zA-Z0-9]{20,}"),
        replacement="[REDACTED_OPENROUTER_KEY]",
    ),
    # Bearer tokens
    RedactionPattern(
        name="bearer_token",
        pattern=re.compile(r"(Bearer\s+)[a-zA-Z0-9\-._~+/]+=*", re.IGNORECASE),
        replacement=r"\1[REDACTED_TOKEN]",
    ),
    # Password assignments
    RedactionPattern(
        name="password_assignment",
        pattern=re.compile(
            r"(password|passwd|pwd)\s*[=:]\s*['\"]?([^\s'\"]{4,})['\"]?",
            re.IGNORECASE,
        ),
        replacement=r"\1=[REDACTED_PASSWORD]",
    ),
    # Database connection strings with credentials
    RedactionPattern(
        name="db_connection_string",
        pattern=re.compile(
            r"(postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://([^:]+):([^@]+)@",
        ),
        replacement=r"\1://\2:[REDACTED_DB_PASSWORD]@",
    ),
    # AWS access key IDs
    RedactionPattern(
        name="aws_access_key",
        pattern=re.compile(r"AKIA[0-9A-Z]{16}"),
        replacement="[REDACTED_AWS_ACCESS_KEY]",
    ),
    # AWS secret access keys (40-char base64)
    RedactionPattern(
        name="aws_secret_key",
        pattern=re.compile(
            r"(aws_secret_access_key|aws_secret_key)\s*[=:]\s*['\"]?([a-zA-Z0-9/+=]{40})['\"]?",
            re.IGNORECASE,
        ),
        replacement=r"\1=[REDACTED_AWS_SECRET]",
    ),
    # GitHub tokens
    RedactionPattern(
        name="github_token",
        pattern=re.compile(r"ghp_[a-zA-Z0-9]{36}"),
        replacement="[REDACTED_GITHUB_TOKEN]",
    ),
    RedactionPattern(
        name="github_oauth_token",
        pattern=re.compile(r"gho_[a-zA-Z0-9]{36}"),
        replacement="[REDACTED_GITHUB_OAUTH_TOKEN]",
    ),
    # Slack tokens
    RedactionPattern(
        name="slack_token",
        pattern=re.compile(r"xox[baprs]-[a-zA-Z0-9\-]{10,}"),
        replacement="[REDACTED_SLACK_TOKEN]",
    ),
    # Private key PEM blocks
    RedactionPattern(
        name="private_key_pem",
        pattern=re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
        replacement="[REDACTED_PRIVATE_KEY]",
    ),
    # Credit card numbers (basic Luhn-checkable patterns)
    RedactionPattern(
        name="credit_card",
        pattern=re.compile(r"\b(?:\d[ -]*?){13,16}\b"),
        replacement="[REDACTED_CC]",
    ),
    # SSN patterns
    RedactionPattern(
        name="ssn",
        pattern=re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
        replacement="[REDACTED_SSN]",
    ),
    # JWT tokens
    RedactionPattern(
        name="jwt_token",
        pattern=re.compile(r"eyJ[a-zA-Z0-9_-]*\.eyJ[a-zA-Z0-9_-]*\.[a-zA-Z0-9_-]*"),
        replacement="[REDACTED_JWT]",
    ),
    # Generic secret assignments
    RedactionPattern(
        name="generic_secret",
        pattern=re.compile(
            r"(secret|token|api_key|apikey|access_key)\s*[=:]\s*['\"]?([a-zA-Z0-9\-_]{16,})['\"]?",
            re.IGNORECASE,
        ),
        replacement=r"\1=[REDACTED_SECRET]",
    ),
]


class SecretRedactor:
    """Detects and redacts secrets from text before memory storage.

    Usage:
        redactor = SecretRedactor()
        result = redactor.redact("My API key is sk-abc123...")
        assert result.was_redacted
        assert "sk-abc123" not in result.text
    """

    def __init__(self, patterns: list[RedactionPattern] | None = None) -> None:
        self._patterns = patterns or _PATTERNS

    def redact(self, text: str) -> RedactionResult:
        """Redact all detected secrets from text.

        Returns a RedactionResult with the redacted text and a list of
        redaction descriptions (pattern names that matched).
        """
        redacted = text
        redactions: list[str] = []

        for rp in self._patterns:
            if rp.pattern.search(redacted):
                redacted = rp.pattern.sub(rp.replacement, redacted)
                redactions.append(rp.name)

        return RedactionResult(text=redacted, redactions=redactions)

    def redact_dict(self, data: dict) -> RedactionResult:
        """Redact secrets from all string values in a dict, recursively.

        Nested mappings are redacted in place and remain dicts; only the
        top-level result is exposed as the ``text`` representation.
        """
        redacted_dict, all_redactions = self._redact_mapping(data)
        return RedactionResult(text=str(redacted_dict), redactions=all_redactions)

    def _redact_mapping(self, data: dict) -> tuple[dict, list[str]]:
        """Return ``(redacted_dict, redaction_names)`` for a mapping."""
        redacted_dict: dict = {}
        all_redactions: list[str] = []

        for key, value in data.items():
            if isinstance(value, str):
                result = self.redact(value)
                redacted_dict[key] = result.text
                all_redactions.extend(result.redactions)
            elif isinstance(value, dict):
                nested, nested_redactions = self._redact_mapping(value)
                redacted_dict[key] = nested
                all_redactions.extend(nested_redactions)
            else:
                redacted_dict[key] = value

        return redacted_dict, all_redactions

    def has_secrets(self, text: str) -> bool:
        """Quick check if text contains any known secret patterns."""
        return any(rp.pattern.search(text) for rp in self._patterns)


# Module-level convenience function
_default_redactor = SecretRedactor()


def redact_secrets(text: str) -> RedactionResult:
    """Convenience function to redact secrets using the default redactor."""
    return _default_redactor.redact(text)
