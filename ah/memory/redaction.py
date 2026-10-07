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
    "StreamingSecretRedactor",
    "STREAM_TAIL_CHARS",
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


def _luhn_valid(digits: str) -> bool:
    """Return True if *digits* passes the Luhn checksum (CC validation)."""
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


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
        pattern=re.compile(
            r"cohere_api_key\s*[=:]\s*['\"]?([a-zA-Z0-9\-_]{20,})['\"]?", re.IGNORECASE
        ),
        replacement="cohere_api_key=[REDACTED_COHERE_KEY]",
    ),
    RedactionPattern(
        name="openrouter_api_key",
        pattern=re.compile(r"sk-or-[a-zA-Z0-9_-]{20,}"),
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
    # Bare 40-char base64 AWS-style secret (no key name prefix)
    RedactionPattern(
        name="aws_secret_key_bare",
        pattern=re.compile(r"\b[a-zA-Z0-9/+=]{40}\b"),
        replacement="[REDACTED_AWS_SECRET]",
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
    RedactionPattern(
        name="github_pat",
        pattern=re.compile(r"github_pat_[a-zA-Z0-9_]{22,255}"),
        replacement="[REDACTED_GITHUB_TOKEN]",
    ),
    RedactionPattern(
        name="github_app_token",
        pattern=re.compile(r"ghs_[a-zA-Z0-9]{36}"),
        replacement="[REDACTED_GITHUB_TOKEN]",
    ),
    RedactionPattern(
        name="github_user_token",
        pattern=re.compile(r"ghu_[a-zA-Z0-9]{36}"),
        replacement="[REDACTED_GITHUB_TOKEN]",
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
    # Contiguous 9-digit SSN (word boundaries to avoid matching longer numbers)
    RedactionPattern(
        name="ssn_contiguous",
        pattern=re.compile(r"\b\d{9}\b"),
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
            if rp.name == "credit_card":
                # Only redact digit runs that pass the Luhn checksum to reduce FPs.
                def _cc_repl(m: re.Match[str]) -> str:
                    digits = re.sub(r"\D", "", m.group(0))
                    if 13 <= len(digits) <= 19 and _luhn_valid(digits):
                        return rp.replacement
                    return m.group(0)

                new_text = rp.pattern.sub(_cc_repl, redacted)
                if new_text != redacted:
                    redactions.append(rp.name)
                    redacted = new_text
                continue
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
        """Return ``(redacted_dict, redaction_names)`` for a mapping.

        Nested dicts stay dicts; lists/tuples/sets/bytes are redacted
        recursively with container types preserved (tuples/sets rebuilt).
        """
        redacted_dict: dict = {}
        all_redactions: list[str] = []

        for key, value in data.items():
            redacted_value, names = self._redact_value(value)
            redacted_dict[key] = redacted_value
            all_redactions.extend(names)

        return redacted_dict, all_redactions

    def _redact_value(self, value: object) -> tuple[object, list[str]]:
        """Redact a single value, preserving container types."""
        if isinstance(value, str):
            result = self.redact(value)
            return result.text, result.redactions
        if isinstance(value, dict):
            return self._redact_mapping(value)
        if isinstance(value, list):
            out: list[object] = []
            names: list[str] = []
            for item in value:
                redacted_item, item_names = self._redact_value(item)
                out.append(redacted_item)
                names.extend(item_names)
            return out, names
        if isinstance(value, tuple):
            items: list[object] = []
            names = []
            for item in value:
                redacted_item, item_names = self._redact_value(item)
                items.append(redacted_item)
                names.extend(item_names)
            return tuple(items), names
        if isinstance(value, set):
            items_set: set[object] = set()
            names = []
            for item in value:
                redacted_item, item_names = self._redact_value(item)
                try:
                    items_set.add(redacted_item)  # type: ignore[arg-type]
                except TypeError:
                    # Unhashable after redaction (e.g. dict) — fall back to frozenset repr
                    items_set.add(str(redacted_item))
                names.extend(item_names)
            return items_set, names
        if isinstance(value, bytes):
            try:
                text = value.decode("utf-8")
            except UnicodeDecodeError:
                return value, []
            result = self.redact(text)
            return result.text.encode("utf-8"), result.redactions
        return value, []

    def has_secrets(self, text: str) -> bool:
        """Quick check if text contains any known secret patterns."""
        return any(rp.pattern.search(text) for rp in self._patterns)


# Module-level convenience function
_default_redactor = SecretRedactor()


def redact_secrets(text: str) -> RedactionResult:
    """Convenience function to redact secrets using the default redactor."""
    return _default_redactor.redact(text)


# Streaming redaction policy (5.1 corrective rewrite).
#
# Invariant: no bytes belonging to a recognized secret are emitted. The old
# implementation split RAW text at len-TAIL before matching, so a secret
# crossing the cut was redacted as two non-matching fragments and leaked in
# full (reproduced: "sk-" + 24 chars + 486 padding emitted the whole key).
#
# New design: never split before matching. Each feed computes the safe
# frontier — the earliest buffer index where a *partial* secret could begin
# and extend past the current end — using end-anchored prefix patterns for
# every supported format. Only text before the frontier is emitted (fully
# redacted; complete matches inside it are removed). Everything from the
# frontier on is withheld until more deltas arrive or flush() redacts it
# whole. Bounded by construction: every partial pattern has an explicit max
# length except PEM blocks, which are capped separately (line-frontier
# fallback). Formats and bounds are documented below, not "perfect".
STREAM_TAIL_CHARS = 512  # kept for backwards compat; see MAX_PARTIAL_HOLD
MAX_PARTIAL_HOLD = 1024
MAX_BUFFERED_PEM = 32 * 1024

# End-anchored partial patterns: each matches a PREFIX of a valid secret that
# reaches the buffer end. Leftmost start across all patterns = frontier.
_PARTIAL_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9]{0,200}$"),
    re.compile(r"(?<![A-Za-z0-9])sk-ant-[A-Za-z0-9\-_]{0,200}$"),
    re.compile(r"(?<![A-Za-z0-9])sk-or-[A-Za-z0-9\-_]{0,200}$"),
    re.compile(r"(?<![A-Za-z0-9])ghp_[A-Za-z0-9]{0,36}$"),
    re.compile(r"(?<![A-Za-z0-9])gho_[A-Za-z0-9]{0,36}$"),
    re.compile(r"(?<![A-Za-z0-9])ghs_[A-Za-z0-9]{0,36}$"),
    re.compile(r"(?<![A-Za-z0-9])ghu_[A-Za-z0-9]{0,36}$"),
    re.compile(r"github_pat_[A-Za-z0-9_]{0,255}$"),
    re.compile(r"(?<![A-Z0-9])AKIA[0-9A-Z]{0,16}$"),
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]{0,200}$"),
    re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]{0,500}$", re.IGNORECASE),
    re.compile(r"(?:password|passwd|pwd)\s*[=:]\s*['\"]?[^\s'\"]{0,200}$", re.IGNORECASE),
    re.compile(
        r"(?:secret|token|api_key|apikey|access_key|cohere_api_key|aws_secret_access_key|aws_secret_key)"
        r"\s*[=:]\s*['\"]?[A-Za-z0-9\-_/+=]{0,300}$",
        re.IGNORECASE,
    ),
    re.compile(r"(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://[^@\s]{0,300}$"),
    re.compile(r"eyJ[A-Za-z0-9_.\-/+=]{0,1200}$"),
    # Bare base64 / digit runs have no lead: the frontier scan handles them
    # below with content checks (digit/symbol presence) so ordinary trailing
    # words flush immediately while secret-like runs are withheld.
    re.compile(r"(?:(?<=[^A-Za-z0-9/+=])|^)[A-Za-z0-9/+=]{8,39}$"),
)

_PEM_BEGIN_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_PEM_END_RE = re.compile(r"-----END [A-Z ]*PRIVATE KEY-----")
_CC_RUN_RE = re.compile(r"[\d -]{6,40}$")


# Short-tail rule: a tiny trailing fragment ("s", "sk", "ghu") is not yet
# recognizable as a partial secret but could combine with the next delta to
# rebuild one. Withhold a trailing run (bounded 64 chars) when it is an exact
# prefix of a known secret lead or contains a digit/symbol that could grow
# into a bare secret. Ordinary words flush immediately.
_LEADS = (
    "sk-",
    "sk-ant-",
    "sk-or-",
    "ghp_",
    "gho_",
    "ghs_",
    "ghu_",
    "github_pat_",
    "akia",
    "xoxb-",
    "xoxa-",
    "xoxp-",
    "xoxr-",
    "xoxs-",
    "bearer",
    "eyj",
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "access_key",
    "cohere_api_key",
    "aws_secret_access_key",
    "aws_secret_key",
    "postgres",
    "postgresql",
    "mysql",
    "mongodb",
    "redis",
)
_LEAD_PREFIXES = frozenset(p[:i] for p in _LEADS for i in range(1, len(p) + 1))
_SHORT_TAIL_RE = re.compile(r"[A-Za-z0-9/+=]{1,64}$")


def _short_tail_hold(buf: str) -> int:
    m = _SHORT_TAIL_RE.search(buf)
    if not m:
        return len(buf)
    run = m.group(0)
    # Only a run that starts at a boundary; mid-run cuts are already covered
    # by the partial table above.
    if m.start() > 0 and buf[m.start() - 1].isalnum():
        return len(buf)
    low = run.lower()
    if low in _LEAD_PREFIXES:
        return m.start()
    if any(c.isdigit() or c in "+/=" for c in run):
        return m.start()
    return len(buf)


def _stream_frontier(buf: str) -> int:
    """Return the earliest index that must be withheld (len(buf) if none)."""
    frontier = len(buf)
    for rx in _PARTIAL_RES[:-1]:
        m = rx.search(buf)
        if m and m.start() < frontier:
            frontier = m.start()
            if frontier == 0:
                return 0
    # Bare base64 runs: withhold only secret-like trailing runs (contain a
    # digit or +/=, or all-caps); ordinary lowercase words flush at once.
    m = _PARTIAL_RES[-1].search(buf)
    if m:
        run = m.group(0)
        if (
            any(c.isdigit() or c in "+/=" for c in run) or (run.isupper() and len(run) >= 8)
        ) and m.start() < frontier:
            frontier = m.start()
            if frontier == 0:
                return 0
    # Credit-card / SSN digit runs: only when the trailing run holds digits
    # that could reach a valid length.
    m = _CC_RUN_RE.search(buf)
    if m:
        digits = sum(c.isdigit() for c in m.group(0))
        if digits >= 4 and m.start() < frontier:
            frontier = m.start()
    # PEM: withhold an open block until its END (capped; line fallback).
    for b in _PEM_BEGIN_RE.finditer(buf):
        tail = buf[b.start() :]
        if not _PEM_END_RE.search(tail):
            if b.start() < frontier:
                frontier = b.start()
            break
    # Short ambiguous tail (single-char fragments that later deltas could
    # complete into a secret).
    short = _short_tail_hold(buf)
    if short < frontier:
        frontier = short
    if frontier < len(buf) - MAX_BUFFERED_PEM:
        # Pathological open block: emit redacted complete lines only. A PEM
        # body line is base64; full lines are removed by the bare-secret
        # pattern, so a line frontier cannot leak a complete line.
        cut = buf.rfind("\n", 0, len(buf) - MAX_BUFFERED_PEM)
        if cut > frontier:
            frontier = cut + 1
    return frontier


class StreamingSecretRedactor:
    """Boundary-safe redactor for streamed text deltas (rewritten 5.1).

    Feed deltas; only the safe frontier (no partial secret crosses it) is
    emitted, fully redacted. The withheld suffix is retained until further
    deltas resolve it or :meth:`flush` redacts it whole. Byte-bounded
    buffering; see policy table above for per-format bounds.
    """

    def __init__(self, tail: int = STREAM_TAIL_CHARS) -> None:
        self._max_hold = max(64, tail)
        self._buf = ""

    def feed(self, delta: str) -> str:
        """Add *delta*, return newly-safe redacted output (may be empty)."""
        if not delta:
            return ""
        self._buf += delta
        frontier = _stream_frontier(self._buf)
        if frontier <= 0:
            return ""
        safe, self._buf = self._buf[:frontier], self._buf[frontier:]
        return redact_secrets(safe).text

    def flush(self) -> str:
        """Redact and return any buffered remainder."""
        out = redact_secrets(self._buf).text if self._buf else ""
        self._buf = ""
        return out
