"""Adversarial redaction corpus for R4-016 (test-only, synthetic values).

This corpus is the acceptance spec for the R4-016 redaction precision work:
every secret case must be fully removed (assert side: the value itself and
every 8-char fragment of it) both through ``redact_secrets`` and through
every split of ``StreamingSecretRedactor``; every benign case must come back
byte-identical.

Covered classes (all fixed by the R4-016 pattern-engine changes):

* Secret values under token/secret-style names: dotted / URL-safe base64
  payloads, short leading runs, quoted and unquoted forms.
* Over-match guards: English prose around "bearer", ``task-…`` identifiers,
  stand-alone 9-digit numbers.
* ``credit_card`` covers 13-19 digits behind the Luhn gate.
* A JWT whose segments carry base64 ``=`` padding.

Everything here is synthetic; no database, network, or real credentials.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from machina.memory.redaction import StreamingSecretRedactor, redact_secrets

#: Length of a payload window that must never survive redaction. Eight
#: characters is far longer than any legitimate leftover (replacement
#: markers share no text with the payloads below).
FRAGMENT = 8

# --- synthetic credential values -------------------------------------------

_SK_CLASSIC = "sk-AbCd0123456789EfGhIjKlMnOpQrStUv"
_SK_PROJECT = "sk-proj-Ab1Cd2Ef3Gh4Ij5Kl6Mn7Op8Qr9St"
_SK_ANTHROPIC = "sk-ant-api03-Ab1Cd2Ef3Gh4Ij5Kl6Mn7Op8"
_SK_OPENROUTER = "sk-or-v1-Ab1Cd2Ef3Gh4Ij5Kl6Mn7Op8Qr9"
_SK_STRIPE = "sk_" + "live_Ab1Cd2Ef3Gh4Ij5Kl6Mn7Op8"
_GOOGLE = "AIzaSyD-Ab1Cd2Ef3Gh4Ij5Kl6Mn7Op8Qr9StUv"
_GITHUB = "ghp_" + "Ab1" * 12  # exactly the 36-char body github_token requires
_SLACK = "xoxb-123456789012-AbCdEfGhIjKl"
_AWS_ACCESS = "AKIAIOSFODNN7EXAMPLE"
_AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"

_JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ"
    ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
)
_PADDED_JWT = (
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0=.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
)
_PEM = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIEowIBAAKCAQEAyQnSyntheticKeyMaterial4xY8zA1bC2dE3fG4hJ5kL6mN7oP8qR9sT0uVyZ\n"
    "wXyZaBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789abcdefGHIJKLMNOPQRSTUVWXYZab\n"
    "-----END RSA PRIVATE KEY-----"
)
_PASSWORD = "CorrectHorseBattery9!"
_PASSWORD_DOTTED = "synthetic.pass.word.123456"
_PWD = "hunter2synthetic"
_DB_PASSWORD = "syntheticDBpass99"
_COHERE = "Ab1Cd2Ef3Gh4Ij5Kl6Mn7Op8"

# R4-016 leak class: a >= 16-char run followed by a non-class character.
_DOTTED_SHORT = "abc.defghijklmnop.qrstuvwxyz0123"
_DOTTED_LONG = "ABCDEFGHIJKLMNOP.qrstuvwxyz012345"
# 33 chars: "token=" + value stays at 39, so the value cannot be swallowed
# by the unrelated bare 40-char AWS-secret pattern, which would mask the
# generic_secret bug this case must expose.
_URLSAFE_SHORT = "Zm9vYmFyQmF6UXV4+KzEy/MzQ1Njc4OT="
_URLSAFE_LONG = "Zm9vYmFyQmF6UXV4+KzEy/MzQ1Njc4OTA="

# Luhn-valid 17- and 19-digit numbers (Stripe-style prefixes, synthetic).
_CARD_17 = "40000000000000006"
_CARD_19 = "4000000000000000006"


@dataclass(frozen=True)
class SecretCase:
    """A secret payload embedded in text; it must never survive redaction."""

    case_id: str
    secret: str
    text: str
    #: R4-016 reason when redaction is currently broken; None when it works.
    broken: str | None = None


@dataclass(frozen=True)
class BenignCase:
    """Ordinary text that redaction must return unchanged."""

    case_id: str
    text: str
    broken: str | None = None


SECRET_CASES: tuple[SecretCase, ...] = (
    # Recognized formats — these must keep passing.
    SecretCase("sk_classic", _SK_CLASSIC, f"openai_api_key={_SK_CLASSIC}"),
    SecretCase("sk_project", _SK_PROJECT, f"The deployment uses {_SK_PROJECT} internally."),
    SecretCase("sk_anthropic", _SK_ANTHROPIC, f"anthropic_key={_SK_ANTHROPIC}"),
    SecretCase("sk_openrouter", _SK_OPENROUTER, f"openrouter_api_key={_SK_OPENROUTER}"),
    SecretCase("sk_stripe", _SK_STRIPE, f"stripe key {_SK_STRIPE} rotated"),
    SecretCase("google_api_key", _GOOGLE, f"maps key {_GOOGLE} active"),
    SecretCase("github_token", _GITHUB, f"credential {_GITHUB} stored"),
    SecretCase("slack_token", _SLACK, f"slack token {_SLACK} posted"),
    SecretCase("aws_access_key", _AWS_ACCESS, f"aws key {_AWS_ACCESS} found"),
    SecretCase("aws_secret_named", _AWS_SECRET, f"aws_secret_access_key={_AWS_SECRET}"),
    SecretCase("aws_secret_bare", _AWS_SECRET, f"Stored credential: {_AWS_SECRET} (rotated)"),
    SecretCase("jwt_standard", _JWT, f"session {_JWT} issued"),
    SecretCase("pem_block", _PEM, f"PEM export:\n{_PEM}\nEnd of export."),
    SecretCase("password_plain", _PASSWORD, f"password={_PASSWORD}"),
    SecretCase("password_quoted_dotted", _PASSWORD_DOTTED, f'password="{_PASSWORD_DOTTED}"'),
    SecretCase("password_colon", _PWD, f"pwd: {_PWD}"),
    SecretCase(
        "db_connection",
        _DB_PASSWORD,
        f"postgres://app_user:{_DB_PASSWORD}@db.internal:5432/app",
    ),
    SecretCase("ssn_dashed", "123-45-6789", "SSN 123-45-6789 on file"),
    SecretCase("card_16_spaced", "4111 1111 1111 1111", "card on file: 4111 1111 1111 1111"),
    SecretCase("card_16_plain", "4111111111111111", "card: 4111111111111111"),
    SecretCase("cohere_api_key", _COHERE, f"cohere_api_key={_COHERE}"),
    SecretCase("api_key_quoted_short", "abc.def.ghi.123", 'api_key="abc.def.ghi.123"'),
    # R4-016 leak class: dotted / padded values under token/secret-style names.
    SecretCase("dotted_token_short_lead", _DOTTED_SHORT, f"token={_DOTTED_SHORT}"),
    SecretCase("dotted_secret_tail", _DOTTED_LONG, f"secret={_DOTTED_LONG}"),
    SecretCase("quoted_dotted_token", _DOTTED_SHORT, f'token="{_DOTTED_SHORT}"'),
    SecretCase("quoted_dotted_secret", _DOTTED_LONG, f'secret="{_DOTTED_LONG}"'),
    SecretCase("dotted_access_token", _DOTTED_SHORT, f"access_token={_DOTTED_SHORT}"),
    SecretCase("dotted_api_key_short_lead", _DOTTED_SHORT, f"api_key={_DOTTED_SHORT}"),
    SecretCase("urlsafe_token_tail", _URLSAFE_SHORT, f"token={_URLSAFE_SHORT}"),
    SecretCase(
        "urlsafe_quoted_secret_tail",
        _URLSAFE_LONG,
        f'secret="{_URLSAFE_LONG}"',
    ),
    SecretCase("padded_jwt", _PADDED_JWT, f"token={_PADDED_JWT}"),
    SecretCase("card_17_luhn_valid", _CARD_17, f"card {_CARD_17} charged"),
    SecretCase("card_19_luhn_valid", _CARD_19, f"card {_CARD_19} charged"),
)


BENIGN_CASES: tuple[BenignCase, ...] = (
    BenignCase("bearer_of_bad_news", "the report was a bearer of bad news"),
    BenignCase("bearer_token_phrase", "the bearer token is not configured yet"),
    BenignCase("task_hex_identifier", "task-0123456789abcdef0123456789"),
    BenignCase("math_9_pow_9", "387420489"),
    BenignCase("order_id_13_digits", "order 1234567890123 confirmed"),
    BenignCase("order_id_16_digits", "order 1234567890123456 confirmed"),
    BenignCase("uuid", "session 550e8400-e29b-41d4-a716-446655440000 active"),
    BenignCase("semver", "release v1.2.3-rc.4+build.5678 shipped"),
    BenignCase("filename_with_date", "quarterly-report-2026-10-14.pdf"),
    BenignCase("filename_with_version", "step-4-final-v2.txt"),
    BenignCase("commit_sha1_hex", "commit a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2 merged"),
    BenignCase("secret_sauce", "the secret sauce stays secret"),
    BenignCase("token_bucket", "a token bucket rate limiter"),
    BenignCase("access_key_words", "access key rotation policy"),
    BenignCase("api_key_words", "api key management guide"),
    BenignCase("pwd_prose", "my pwd is missing from the vault"),
    BenignCase("password_prose", "I reset my password yesterday"),
    BenignCase("card_declined", "the card was declined at checkout"),
    BenignCase("phone_number", "call 555-123-4567 for support"),
    BenignCase("private_ip", "shard at 192.168.1.1 responded"),
)


def _marks(case: SecretCase | BenignCase) -> tuple[pytest.MarkDecorator, ...]:
    if case.broken:
        return (pytest.mark.xfail(strict=True, reason=f"R4-016: {case.broken}"),)
    return ()


def _secret_params() -> list:
    params = []
    for case in SECRET_CASES:
        for context in ("plain", "prose"):
            params.append(
                pytest.param(case, context, marks=_marks(case), id=f"{case.case_id}-{context}")
            )
    return params


def _streaming_params() -> list:
    return [pytest.param(case, marks=_marks(case), id=case.case_id) for case in SECRET_CASES]


def _benign_params() -> list:
    return [pytest.param(case, marks=_marks(case), id=case.case_id) for case in BENIGN_CASES]


def _assert_secret_gone(secret: str, output: str, where: str) -> None:
    assert secret not in output, f"{where}: raw secret survived redaction: {output!r}"
    survivors = [
        secret[index : index + FRAGMENT]
        for index in range(len(secret) - FRAGMENT + 1)
        if secret[index : index + FRAGMENT] in output
    ]
    assert not survivors, f"{where}: {survivors[:5]} survived in {output!r}"


def _stream(text: str, chunks: list[str]) -> str:
    redactor = StreamingSecretRedactor()
    emitted = "".join(redactor.feed(chunk) for chunk in chunks)
    return emitted + redactor.flush()


@pytest.mark.parametrize("case, context", _secret_params())
def test_secret_does_not_survive_redaction(case: SecretCase, context: str) -> None:
    text = case.text if context == "plain" else f"Deployment note: {case.text} End of note."
    _assert_secret_gone(case.secret, redact_secrets(text).text, context)


@pytest.mark.parametrize("case", _streaming_params())
def test_streaming_never_leaks_across_boundaries(case: SecretCase) -> None:
    text = case.text
    # Every single split point, fed as two deltas.
    for split in range(1, len(text)):
        output = _stream(text, [text[:split], text[split:]])
        _assert_secret_gone(case.secret, output, f"2-way split at {split}")
    # Multi-way chunkings, including byte-at-a-time.
    for size in (1, 2, 3, 5, 8, 13):
        chunks = [text[index : index + size] for index in range(0, len(text), size)]
        _assert_secret_gone(case.secret, _stream(text, chunks), f"chunks of {size}")


@pytest.mark.parametrize("case", _benign_params())
def test_benign_text_is_left_untouched(case: BenignCase) -> None:
    assert redact_secrets(case.text).text == case.text
