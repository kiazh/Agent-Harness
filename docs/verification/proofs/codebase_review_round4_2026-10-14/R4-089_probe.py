"""R4-089 RED probe (H-CORPUS): long digit-run streaming hold.

Fails on unfixed code: StreamingSecretRedactor cuts a trailing digit run at
40 chars (_DIGIT_RUN_RE ``{0,39}`` cap), the word-boundary pullback stops at
spaces, and the head fragment of a batch-redactable CC window is emitted raw.
Final streamed output then diverges from redact_secrets (batch).
No mocks: pure redaction path, real call sites (feed/flush + redact_secrets).
"""

from machina.memory.redaction import StreamingSecretRedactor, redact_secrets

UNIT = "4111 1111 1111 1111"  # Luhn-valid 16-digit CC window
TEXT = "ref " + UNIT + " " + UNIT + " 4111 " + "done"


def stream(text: str, size: int) -> str:
    r = StreamingSecretRedactor()
    chunks = [text[i : i + size] for i in range(0, len(text), size)]
    return "".join(r.feed(c) for c in chunks) + r.flush()


failures = 0
batch = redact_secrets(TEXT).text
print("batch:", batch)
assert "[REDACTED_CC]" in batch, "probe setup: batch must redact the CC windows"
for size in (1, 2, 3, 5, 8, 13, 999):
    out = stream(TEXT, size)
    status = "AGREE" if out == batch else "DIVERGE"
    if out != batch:
        failures += 1
    print(f"chunk={size:>3} {status}: {out!r}")

r = StreamingSecretRedactor()
head = r.feed("ref " + UNIT + " " + UNIT + " 4111 ")
print("first-feed output:", repr(head))
if head != "ref ":
    failures += 1
    print("LEAK: over-40 run head streamed before the run terminated")

print("RED" if failures else "GREEN", f"({failures} failures)")
raise SystemExit(1 if failures else 0)
