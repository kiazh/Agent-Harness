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
    "redact_value",
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

    # Tests only; kept as the redaction-result probe (review 2026-10-14).
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
    # OpenAI project/service-account/admin keys (dash-separated segments are
    # not covered by the classic sk- pattern).
    RedactionPattern(
        name="openai_project_key",
        pattern=re.compile(r"sk-(?:proj|svcacct|admin)-[A-Za-z0-9_-]{20,}"),
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
    # Google API keys
    RedactionPattern(
        name="google_api_key",
        pattern=re.compile(r"AIza[0-9A-Za-z_-]{20,}"),
        replacement="[REDACTED_GOOGLE_KEY]",
    ),
    # Stripe live secret keys
    RedactionPattern(
        name="stripe_live_key",
        pattern=re.compile(r"sk_live_[A-Za-z0-9]{20,}"),
        replacement="[REDACTED_STRIPE_KEY]",
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
        # Slash is part of the token: do not mistake a 40-character suffix
        # inside a longer filesystem path/base64 run for a standalone key.
        pattern=re.compile(r"(?<![a-zA-Z0-9/+=])[a-zA-Z0-9/+=]{40}(?![a-zA-Z0-9/+=])"),
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
    # Quoted key: value assignments whose identifier ends in "key"
    # (api_key, openai_key, access-key, "service_key": ...). The unquoted
    # pattern above misses quoted values containing non-alphanumerics.
    RedactionPattern(
        name="generic_quoted_key",
        pattern=re.compile(
            r"\b((?:[A-Za-z0-9]+[_.-])?key)['\"]?\s*[=:]\s*(['\"])([^'\"\n]{8,})\2",
            re.IGNORECASE,
        ),
        replacement=r"\1=[REDACTED_KEY]",
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

        from machina.security.secrets import known_secret_values

        for value in known_secret_values():
            if value in redacted:
                redacted = redacted.replace(value, "[REDACTED_SECRET]")
                redactions.append("configured_secret")

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
            if rp.name == "aws_secret_key_bare":
                # Tightened heuristic (RF7-F1): a bare 40-char token must
                # look like a generated AWS-style secret — mixed lower,
                # upper, and digits. Plain 40-char hex digests (commit
                # hashes) and single-case runs are left alone.
                def _aws_repl(m: re.Match[str]) -> str:
                    token = m.group(0)
                    classes = (
                        any(c.islower() for c in token),
                        any(c.isupper() for c in token),
                        any(c.isdigit() for c in token),
                    )
                    return rp.replacement if all(classes) else token

                new_text = rp.pattern.sub(_aws_repl, redacted)
                if new_text != redacted:
                    redactions.append(rp.name)
                    redacted = new_text
                continue
            if rp.pattern.search(redacted):
                redacted = rp.pattern.sub(rp.replacement, redacted)
                redactions.append(rp.name)

        return RedactionResult(text=redacted, redactions=redactions)

    # Tests only; kept as a structured-redaction test seam (review 2026-10-14).
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

    # Tests only; kept as the quick-detection probe (review 2026-10-14).
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
# length except PEM blocks. An open PEM block is withheld whole; if it grows
# past MAX_BUFFERED_PEM the stream redactor replaces its body with the
# standard marker and discards it — a frontier *inside* an unterminated block
# would leak raw body lines, since the whole-string PEM pattern needs the END
# marker to match. Formats and bounds are documented below, not "perfect".
STREAM_TAIL_CHARS = 512  # stream-tail hold; per-format bounds documented below
MAX_BUFFERED_PEM = 32 * 1024
#: Marker replacing an oversized or unterminated open PEM body during
#: streaming; same text as the ``private_key_pem`` replacement pattern.
_PEM_REDACTED_MARKER = "[REDACTED_PRIVATE_KEY]"
#: Tail kept while discarding an oversized open PEM body — wide enough to
#: still recognize a split END marker across feed boundaries.
_PEM_RESUME_WINDOW = STREAM_TAIL_CHARS

# End-anchored partial patterns: each matches a PREFIX of a valid secret that
# reaches the buffer end. Leftmost start across all patterns = frontier.
# LP-09: recognition here must align with COMPLETE patterns (which have no
# preceding-character assumptions), so partials carry no lookbehinds either:
# a secret glued to preceding text ("Xsk-...") withholds and redacts exactly
# like whole-string recognition does.
_PARTIAL_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[A-Za-z0-9]{0,200}$"),
    re.compile(r"sk-(?:proj|svcacct|admin)-[A-Za-z0-9_-]{0,200}$"),
    re.compile(r"sk-ant-[A-Za-z0-9\-_]{0,200}$"),
    re.compile(r"sk-or-[A-Za-z0-9\-_]{0,200}$"),
    re.compile(r"sk_live_[A-Za-z0-9]{0,200}$"),
    re.compile(r"AIza[0-9A-Za-z_-]{0,200}$"),
    re.compile(r"ghp_[A-Za-z0-9]{0,36}$"),
    re.compile(r"gho_[A-Za-z0-9]{0,36}$"),
    re.compile(r"ghs_[A-Za-z0-9]{0,36}$"),
    re.compile(r"ghu_[A-Za-z0-9]{0,36}$"),
    re.compile(r"github_pat_[A-Za-z0-9_]{0,255}$"),
    re.compile(r"AKIA[0-9A-Z]{0,16}$"),
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]{0,200}$"),
    re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]{0,500}$", re.IGNORECASE),
    re.compile(r"(?:password|passwd|pwd)\s*[=:]\s*['\"]?[^\s'\"]{0,200}$", re.IGNORECASE),
    re.compile(
        r"(?:secret|token|api_key|apikey|access_key|cohere_api_key|aws_secret_access_key|aws_secret_key)"
        r"\s*[=:]\s*['\"]?[A-Za-z0-9\-_/+=]{0,300}$",
        re.IGNORECASE,
    ),
    # Quoted key: value assignments (RF7-F1): hold from the identifier so a
    # value split across deltas is never emitted partially. Two stages: the
    # assignment head (key / key" / key: / key: ) and the quoted value tail.
    re.compile(
        r"\b(?:[A-Za-z0-9]+[_.-])?key['\"]?\s*[=:]?\s*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:[A-Za-z0-9]+[_.-])?key['\"]?\s*[=:]\s*['\"][^'\"\n]{0,300}$",
        re.IGNORECASE,
    ),
    re.compile(r"(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://[^@\s]{0,300}$"),
    re.compile(r"(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis):?/{0,2}$"),
    re.compile(r"//[^@\s]{1,300}$"),
    # Assignment name with trailing spaces but no "=" yet: the "=" may arrive
    # in the next delta, and complete patterns normalize spacing on match, so
    # emitting the spaced name now would desynchronize streamed output.
    re.compile(
        r"(?:password|passwd|pwd|secret|token|api_key|apikey|access_key|cohere_api_key"
        r"|aws_secret_access_key|aws_secret_key)\s{1,10}$",
        re.IGNORECASE,
    ),
    re.compile(r"eyJ[A-Za-z0-9_.\-/+=]{0,1200}$"),
)

# PEM marker-fragment partials, applied ONLY while a block is open (see
# _stream_frontier): a split can land inside the dashes or the BEGIN/END
# words themselves. On complete input the whole-pattern redaction applies.
_PEM_DASH_RE = re.compile(r"-{1,5}$")
_PEM_WORD_RE = re.compile(r"\b(?:BEGIN|END)[ A-Z-]{0,40}$")

_PEM_BEGIN_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_PEM_END_RE = re.compile(r"-----END [A-Z ]*PRIVATE KEY-----")
# Leadless formats (bare base64, card/SSN digit runs): withhold a trailing
# run that could grow into one. One trailing word/number of lag mid-stream;
# flush always completes. This is the honest cost of the invariant for
# formats with no distinctive lead.
_BARE_RUN_RE = re.compile(r"(?:(?<=[^A-Za-z0-9/+=])|^)[A-Za-z0-9/+=]{1,39}$")
_DIGIT_RUN_RE = re.compile(r"\d[\d -]{0,39}$")
# Unanchored twins of the lead partials: a hold placed by a run rule can land
# inside a longer secret (e.g. digit hold on the last char of "sk-ant-...0").
# Any such match overlapping the cut pulls the frontier back to its start.
_UNANCHORED: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p.pattern[:-1], p.flags) for p in _PARTIAL_RES
)


# Short-tail rule: a tiny trailing fragment ("s", "sk", "ghu") is not yet
# recognizable as a partial secret but could combine with the next delta to
# rebuild one. Withhold a trailing run (bounded 64 chars) when it is an exact
# prefix of a known secret lead or contains a digit/symbol that could grow
# into a bare secret. Ordinary words flush immediately.
_LEADS = (
    "sk-",
    "sk-proj-",
    "sk-svcacct-",
    "sk-admin-",
    "sk_live_",
    "sk-ant-",
    "sk-or-",
    "aiza",
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
    "-----BEGIN",
    "BEGIN",
    "END",
)
_LEAD_PREFIXES = frozenset(p[:i] for p in _LEADS for i in range(1, len(p) + 1)) | frozenset(
    p[:i].lower() for p in _LEADS for i in range(1, len(p) + 1)
)
_SHORT_TAIL_RE = re.compile(r"[A-Za-z0-9/+=._-]{1,64}$")


def _short_tail_hold(buf: str) -> int:
    m = _SHORT_TAIL_RE.search(buf)
    if not m:
        return len(buf)
    run = m.group(0)
    # Only a run that starts at a boundary; mid-run cuts are already covered
    # by the partial table above.
    if m.start() > 0 and buf[m.start() - 1].isalnum():
        return len(buf)
    core = run.strip("_-.").lower()
    if run.lower() in _LEAD_PREFIXES or core in _LEAD_PREFIXES:
        return m.start()
    # A marker may start mid-token ("X-----BE", "github_"): any suffix of the
    # token that is a lead prefix withholds from the suffix start — but a
    # bare dash run alone ("KEY-----") is not a marker start, only a marker
    # prefix containing lead text is.
    token = run.lower()
    for i in range(1, min(len(token), 13)):
        suffix = token[i:]
        if suffix in _LEAD_PREFIXES and any(c.isalpha() for c in suffix):
            return m.start() + i
    if any(c.isdigit() or c in "+/=" for c in run):
        return m.start()
    return len(buf)


def _stream_frontier(buf: str) -> int:
    """Return the earliest index that must be withheld (len(buf) if none)."""
    frontier = len(buf)
    for rx in _PARTIAL_RES:
        m = rx.search(buf)
        if m and m.start() < frontier:
            frontier = m.start()
            if frontier == 0:
                return 0
    # Leadless trailing runs (LP-09): a bare-base64 fragment or digit run at
    # the buffer end could complete into a recognized secret with the next
    # delta, so withhold from the run start. Ordinary interior text is
    # unaffected; only the trailing run lags mid-stream.
    m = _BARE_RUN_RE.search(buf)
    if m and m.start() < frontier:
        frontier = m.start()
        if frontier == 0:
            return 0
    m = _DIGIT_RUN_RE.search(buf)
    if m and m.start() < frontier:
        frontier = m.start()
        if frontier == 0:
            return 0
    # Short ambiguous tail (single-char fragments that later deltas could
    # complete into a secret lead).
    short = _short_tail_hold(buf)
    if short < frontier:
        frontier = short
    # Never cut between two word characters: whole-string \b recognition
    # would differ on either side alone (e.g. "X4111..." is not a CC number,
    # but cutting after X makes the withheld run look like one). Pull back
    # over preceding word characters; the overlap loop below then aligns to
    # any enclosing signature.
    while (
        0 < frontier < len(buf)
        and (buf[frontier - 1].isalnum() or buf[frontier - 1] == "_")
        and (buf[frontier].isalnum() or buf[frontier] == "_")
    ):
        frontier -= 1
    # Overlap expansion: a run-rule hold must not cut inside a longer secret
    # that started earlier. Pull back over any overlapping lead match.
    for _ in range(4):
        moved = False
        for rx in _UNANCHORED:
            for m in rx.finditer(buf):
                if m.start() < frontier <= m.end() and m.end() > m.start():
                    frontier = m.start()
                    moved = True
                    break
            if moved:
                break
        if not moved:
            break
    # PEM: withhold an open block until its END. The frontier never cuts
    # inside the block: a partial (open) block is not matchable by the
    # whole-string PEM pattern, so emitting any prefix of it would leak raw
    # body lines. Oversized blocks are handled by StreamingSecretRedactor
    # (marker + discard), not by moving this frontier.
    # Marker detection is substring-based ("BEGIN" present with no END after
    # the last one), so splits inside the dashes or the marker words still
    # withhold. The hold starts at the marker including preceding dashes.
    # Trailing dash runs / bare BEGIN/END words are always withheld for the
    # same reason; a cut inside an already-COMPLETE block is repaired below.
    for b in _PEM_BEGIN_RE.finditer(buf):
        if not _PEM_END_RE.search(buf, b.end()):
            if b.start() < frontier:
                frontier = b.start()
            break
    else:
        frag = buf.rfind("BEGIN")
        if frag != -1 and not _PEM_END_RE.search(buf, frag):
            s = frag
            while s > 0 and buf[s - 1] == "-":
                s -= 1
            if s < frontier:
                frontier = s
    for rx in (_PEM_DASH_RE, _PEM_WORD_RE):
        m = rx.search(buf)
        if m and m.start() < frontier:
            frontier = m.start()
            if frontier == 0:
                return 0
    # Complete-block repair: a hold that lands inside an already-COMPLETE
    # PEM block would break its END marker and desynchronize output. Extend
    # to the block end so it emits whole (and redacted).
    for b in _PEM_BEGIN_RE.finditer(buf):
        e = _PEM_END_RE.search(buf, b.end())
        if e and b.start() < frontier <= e.end():
            frontier = e.end()
            break
    return frontier


def _open_pem_block_start(buf: str) -> int | None:
    """Start index of an unterminated PRIVATE KEY block in *buf*, else None.

    Only complete BEGIN markers count: the whole-string PEM pattern (and the
    body it protects) starts at a complete marker, so a bare fragment has no
    body to guard.
    """
    for match in _PEM_BEGIN_RE.finditer(buf):
        if not _PEM_END_RE.search(buf, match.end()):
            return match.start()
    return None


def redact_value(value: object) -> object:
    """Redact nested transport payloads while preserving their structure."""
    return SecretRedactor()._redact_value(value)[0]


class StreamingSecretRedactor:
    """Boundary-safe redactor for streamed text deltas (rewritten 5.1).

    Feed deltas; only the safe frontier (no partial secret crosses it) is
    emitted, fully redacted. The withheld suffix is retained until further
    deltas resolve it or :meth:`flush` redacts it whole. An unterminated PEM
    block is withheld whole; once it exceeds ``MAX_BUFFERED_PEM`` its body is
    replaced by ``[REDACTED_PRIVATE_KEY]`` and discarded — raw body prefixes
    are never emitted, and :meth:`flush` fails closed for any block whose END
    never arrives. Byte-bounded buffering; see policy table above for
    per-format bounds.
    """

    def __init__(self, tail: int = STREAM_TAIL_CHARS) -> None:
        self._buf = ""
        #: Explicit open-PEM state: True from the moment an unterminated block
        #: exceeds MAX_BUFFERED_PEM until its END marker is consumed.
        self._pem_open = False
        #: Withheld text before the open block (restored after its END).
        self._pem_prefix = ""

    def feed(self, delta: str) -> str:
        """Add *delta*, return newly-safe redacted output (may be empty)."""
        if not delta:
            return ""
        self._buf += delta

        if self._pem_open and not self._consume_open_pem_end():
            # Still inside the oversized block: its body was already replaced
            # by the marker, so nothing else may be emitted; only the bounded
            # END-detection tail is kept.
            return ""

        frontier = _stream_frontier(self._buf)
        from machina.security.secrets import known_secret_values

        for value in known_secret_values():
            # Keep any partial configured credential across the emission boundary.
            for length in range(min(len(value) - 1, len(self._buf)), 0, -1):
                if self._buf.endswith(value[:length]):
                    frontier = min(frontier, len(self._buf) - length)
                    break
            # Never split a complete credential before exact-value redaction.
            start = self._buf.rfind(value)
            if start >= 0 and start < frontier < start + len(value):
                frontier = start
        emitted = ""
        if frontier > 0:
            safe, self._buf = self._buf[:frontier], self._buf[frontier:]
            emitted = redact_secrets(safe).text

        start = _open_pem_block_start(self._buf)
        if start is not None and len(self._buf) - start > MAX_BUFFERED_PEM:
            # Pathological open block: never buffer (or emit) its body. The
            # marker stands in for the whole block and is emitted exactly
            # once; keep only a marker-sized tail so a split END can still be
            # recognized and consumed across feeds.
            self._pem_open = True
            self._pem_prefix = self._buf[:start]
            self._buf = self._buf[max(start, len(self._buf) - _PEM_RESUME_WINDOW) :]
            emitted += _PEM_REDACTED_MARKER
        return emitted

    def _consume_open_pem_end(self) -> bool:
        """Consume the END of an oversized open block; True when found.

        The body was already replaced by the redaction marker, so everything
        buffered up to END is discarded, never emitted.
        """
        end = _PEM_END_RE.search(self._buf)
        if end is None:
            if len(self._buf) > _PEM_RESUME_WINDOW:
                self._buf = self._buf[-_PEM_RESUME_WINDOW:]
            return False
        self._buf = self._pem_prefix + self._buf[end.end() :]
        self._pem_prefix = ""
        self._pem_open = False
        return True

    def flush(self) -> str:
        """Redact and return any buffered remainder."""
        if self._pem_open:
            # Oversized unterminated block: the marker was already emitted;
            # drop the remaining body tail rather than emitting any of it.
            self._buf = ""
            self._pem_prefix = ""
            self._pem_open = False
            return ""
        if _open_pem_block_start(self._buf) is not None:
            # Unterminated block under the cap: the whole-string PEM pattern
            # needs the END marker to match, so fail closed instead of
            # leaking the buffered body.
            self._buf = ""
            return _PEM_REDACTED_MARKER
        out = redact_secrets(self._buf).text if self._buf else ""
        self._buf = ""
        return out
