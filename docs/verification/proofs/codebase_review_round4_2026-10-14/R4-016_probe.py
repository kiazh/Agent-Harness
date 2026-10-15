"""R4-016 evidence: before/after per corpus class + fuzz + idempotence.

Run from the repo root:  .venv/bin/python docs/verification/proofs/.../R4-016_probe.py
"""
import importlib.util
import pathlib
import random
import time

BEFORE_PATH = str(pathlib.Path(__file__).with_name("R4-016_redaction_before.py"))

spec = importlib.util.spec_from_file_location("redaction_before", BEFORE_PATH)
import sys
before = importlib.util.module_from_spec(spec)
sys.modules["redaction_before"] = before
spec.loader.exec_module(before)

import machina.memory.redaction as after  # noqa: E402

CASES = [
    # (label, text, secret)
    ("dotted_token_short_lead", "token=abc.defghijklmnop.qrstuvwxyz0123", "abc.defghijklmnop.qrstuvwxyz0123"),
    ("dotted_secret_tail", "secret=ABCDEFGHIJKLMNOP.qrstuvwxyz012345", "ABCDEFGHIJKLMNOP.qrstuvwxyz012345"),
    ("quoted_dotted_token", 'token="abc.defghijklmnop.qrstuvwxyz0123"', "abc.defghijklmnop.qrstuvwxyz0123"),
    ("quoted_dotted_secret", 'secret="ABCDEFGHIJKLMNOP.qrstuvwxyz012345"', "ABCDEFGHIJKLMNOP.qrstuvwxyz012345"),
    ("dotted_access_token", "access_token=abc.defghijklmnop.qrstuvwxyz0123", "abc.defghijklmnop.qrstuvwxyz0123"),
    ("dotted_api_key_short_lead", "api_key=abc.defghijklmnop.qrstuvwxyz0123", "abc.defghijklmnop.qrstuvwxyz0123"),
    ("urlsafe_token_tail", "token=Zm9vYmFyQmF6UXV4+KzEy/MzQ1Njc4OT=", "Zm9vYmFyQmF6UXV4+KzEy/MzQ1Njc4OT="),
    ("urlsafe_quoted_secret_tail", 'secret="Zm9vYmFyQmF6UXV4+KzEy/MzQ1Njc4OTA="', "Zm9vYmFyQmF6UXV4+KzEy/MzQ1Njc4OTA="),
    ("padded_jwt", "token=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0=.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
     "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0=.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"),
    ("card_17_luhn_valid", "card 40000000000000006 charged", "40000000000000006"),
    ("card_19_luhn_valid", "card 4000000000000000006 charged", "4000000000000000006"),
    ("bearer_of_bad_news", "the report was a bearer of bad news", None),
    ("bearer_token_phrase", "the bearer token is not configured yet", None),
    ("task_hex_identifier", "task-0123456789abcdef0123456789", None),
    ("math_9_pow_9", "387420489", None),
]

print("=== before/after per corpus class ===")
for label, text, secret in CASES:
    b = before.redact_secrets(text).text
    a = after.redact_secrets(text).text
    leaked_before = secret is not None and secret in b
    tail_before = secret is not None and any(
        secret[i:i+8] in b for i in range(len(secret) - 7)
    )
    ok_after = secret is not None and not any(
        secret[i:i+8] in a for i in range(len(secret) - 7)
    ) or (secret is None and a == text)
    print(f"{label}\n  text : {text!r}\n  before: {b!r}  [leak={leaked_before} tail={tail_before}]\n  after : {a!r}  [ok={ok_after}]")

print()
print("=== idempotence (all corpus texts) ===")
bad = []
for label, text, secret in CASES:
    once = after.redact_secrets(text).text
    twice = after.redact_secrets(once).text
    if once != twice:
        bad.append((label, once, twice))
print("non-idempotent cases:", bad if bad else "none")

print()
print("=== streamed benign task- identifier (no mangle check) ===")
r = after.StreamingSecretRedactor()
benign = "task-0123456789abcdef0123456789"
out = "".join(r.feed(c) for c in benign) + r.flush()
print("char-at-a-time:", repr(out), "equal:", out == benign)
for split in range(len(benign) + 1):
    r = after.StreamingSecretRedactor()
    out = r.feed(benign[:split]) + r.feed(benign[split:]) + r.flush()
    if out != benign:
        print("MISMATCH at split", split, repr(out)); break
else:
    print("all 2-way splits equal:", True)

print()
print("=== fuzz: no 8-char fragment of embedded secret survives (batch) ===")
rng = random.Random(41616)
alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.+-_=/~"
from machina.memory.redaction import StreamingSecretRedactor, redact_secrets
fails = 0
for i in range(300):
    secret = "".join(rng.choice(alphabet) for _ in range(rng.randint(16, 48)))
    # avoid embedding that the bare 40-char AWS heuristic itself might catch -> that is fine too
    ctx = rng.choice(["token={s}", 'secret="{s}"', "api_key={s}", 'token="{s}" tail', "pre token={s} post"])
    text = ctx.replace("{s}", secret)
    out = redact_secrets(text).text
    frags = [secret[j:j+8] for j in range(len(secret)-7) if secret[j:j+8] in out]
    if frags:
        fails += 1
        if fails <= 3:
            print("FUZZ LEAK:", ctx, repr(out), frags[:3])
print("fuzz batch leaks:", fails, "/ 300")

print()
print("=== micro-benchmark (hot path, 200 x ~24KB text) ===")
blob = ("The quick brown fox jumps over the lazy dog. " * 200 +
        "token=Zm9vYmFyQmF6UXV4+KzEy/MzQ1Njc4OT= " +
        "Bearer abcdef1234567890XYZ " +
        "api_key=abc.defghijklmnop.qrstuvwxyz0123 " +
        "user@example.com 4111 1111 1111 1111 123-45-6789 card 40000000000000006\n") * 200
def bench(fn, n=20):
    t0 = time.perf_counter()
    for _ in range(n):
        fn(blob)
    return (time.perf_counter() - t0) / n * 1000  # ms per call
# warmup
before.redact_secrets(blob); after.redact_secrets(blob)
b_ms = bench(before.redact_secrets)
a_ms = bench(after.redact_secrets)
print(f"text length: {len(blob)} chars")
print(f"before: {b_ms:.2f} ms/call; after: {a_ms:.2f} ms/call; delta: {a_ms - b_ms:+.2f} ms ({(a_ms/b_ms - 1)*100:+.1f}%)")
