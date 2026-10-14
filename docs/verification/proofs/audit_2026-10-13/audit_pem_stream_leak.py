#!/usr/bin/env python
"""Audit finding: streaming redactor leaks PEM body past MAX_BUFFERED_PEM.

A synthetic private key block (600 x 64-char body lines) is fully redacted
as one string; streamed in chunks it emits raw body lines.
"""
import sys
from pathlib import Path

REPO = Path("/Users/shadow/machina")
sys.path.insert(0, str(REPO))

from machina.memory.redaction import (
    MAX_BUFFERED_PEM,
    StreamingSecretRedactor,
    redact_secrets,
)

MARKERS = [f"BODYLINE{i:04d}" + "A" * (64 - len(f"BODYLINE{i:04d}")) for i in range(600)]
body = "\n".join(MARKERS)
complete = f"-----BEGIN PRIVATE KEY-----\n{body}\n-----END PRIVATE KEY-----\n"
print(f"block size: {len(complete)} chars (MAX_BUFFERED_PEM={MAX_BUFFERED_PEM})")

whole = redact_secrets(complete).text
print("whole-string: body leaked:", "BODYLINE0000" in whole)

stream = StreamingSecretRedactor()
emitted_parts = []
for i in range(0, len(complete), 1024):
    emitted_parts.append(stream.feed(complete[i : i + 1024]))
emitted_parts.append(stream.flush())
emitted = "".join(emitted_parts)
leaked = "BODYLINE0000" in emitted or "BODYLINE0500" in emitted
print("streaming: body leaked:", leaked)
if leaked:
    idx = emitted.find("BODYLINE")
    print("first leaked body context:", repr(emitted[idx : idx + 90]))

# Also the genuinely unfinished variant (>32KiB, no END), per the report.
unfinished = f"-----BEGIN PRIVATE KEY-----\n{body}\n"
stream2 = StreamingSecretRedactor()
out2 = "".join(stream2.feed(unfinished[i : i + 1024]) for i in range(0, len(unfinished), 1024))
out2 += stream2.flush()
print("unfinished stream: body leaked:", "BODYLINE0000" in out2)
print("whole unfinished redacted:", "BODYLINE0000" not in redact_secrets(unfinished).text)

ok = ("BODYLINE0000" not in whole) and leaked
print("CLAIM CONFIRMED (whole=redacted, stream=leaked)" if ok else "CLAIM NOT REPRODUCED (as stated)")
sys.exit(0 if ok else 1)
