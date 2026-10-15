"""H02 repro: reviewer's synthetic diagnostic, verbatim conditions."""
from machina.memory.redaction import StreamingSecretRedactor, redact_secrets

chunks = ["sk-" + "a" * 210, "b" * 80 + " done."]
full = "".join(chunks)

batch = redact_secrets(full).text
r = StreamingSecretRedactor()
streamed = "".join(r.feed(c) for c in chunks) + r.flush()

print("batch output:", repr(batch))
print("stream output:", repr(streamed))
tail = "b" * 80
print("unredacted synthetic tail length:", len(tail) if tail in streamed else 0)
assert batch == "[REDACTED_OPENAI_KEY] done.", batch
leaked = streamed != batch and tail in streamed
print("LEAK REPRODUCED:", leaked)
