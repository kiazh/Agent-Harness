"""Lane L2R sanity probes: large benign run + huge token flush + emission timing."""
from machina.memory.redaction import StreamingSecretRedactor, redact_secrets

# 1) Large benign run streams through (no unbounded benign hold) and round-trips.
text = "prefix " + "a" * 500_000 + " suffix"
red = StreamingSecretRedactor()
out = ""
for i in range(0, len(text), 8192):
    out += red.feed(text[i : i + 8192])
out += red.flush()
assert out == text, "benign large run changed"

# 2) A 100k-char still-growing token is withheld end-to-end, then fully redacted.
token = "sk-" + "a" * 100_000
text2 = token + " done"
red = StreamingSecretRedactor()
out2 = ""
for i in range(0, len(token), 4096):
    out2 += red.feed(token[i : i + 4096])
assert out2 == "", f"token bytes emitted early: {out2[:40]!r}"
out2 += red.feed(" done") + red.flush()
assert out2 == "[REDACTED_OPENAI_KEY] done", out2[:80]

# 3) Benign text ending in a lead-like word still flushes intact.
text3 = "the deployment sk"
red = StreamingSecretRedactor()
out3 = red.feed(text3) + red.flush()
assert out3 == text3, out3

# 4) Left-boundary rule still holds mid-stream: task-hex identifier survives.
text4 = "task-" + "0123456789abcdef" * 4
red = StreamingSecretRedactor()
out4 = "".join(red.feed(ch) for ch in text4) + red.flush()
assert out4 == redact_secrets(text4).text == text4, out4

print("probes OK: benign-large, huge-token-flush, lead-tail-flush, task-hex boundary")
