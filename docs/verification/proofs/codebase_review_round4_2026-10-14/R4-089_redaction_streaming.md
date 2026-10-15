# R4-089 (redaction streaming lane): long digit-run hold + calibration note

Scope: this lane owns ONLY the streaming redactor in
`machina/memory/redaction.py` (frontier/hold logic, `StreamingSecretRedactor`)
and additive cases in `tests/test_redaction_adversarial.py`. The other R4-089
bullets (dedup embeddings, approval gate, `update_access`, `shared_bus`) are
other writers' lanes and are untouched here.

## 1. Streaming digit-run hold fix

Root cause: `_DIGIT_RUN_RE` capped the trailing hold at 40 chars
(`\d[\d -]{0,39}$`). For a trailing digit/space/dash run longer than 40
chars the frontier cut mid-run, and the word-boundary pullback in
`_stream_frontier` stops at spaces, so a raw head fragment of a CC window
the batch pass redacts was emitted mid-stream.

RED (unfixed code, H-CORPUS probe, no mocks — pure redaction path):
`R4-089_probe.py` / `R4-089_red.log` / `R4-089_red_pytest.txt`.
Text `ref 4111 1111 1111 1111 4111 1111 1111 1111 4111 done` (synthetic
Luhn-valid windows): batch redacts both windows, but streaming at chunk
sizes 1/2/3/8 emits `ref 4111 …` — final output diverges from batch.
New pytest cases fail 3× on unfixed code for exactly this reason
(`R4-089_red_pytest.txt`, exit 1); calibration/benign cases pass on both
(by design — they pin existing correct behavior).

Fix (smallest safe change, streaming part only): uncap the run —
`_DIGIT_RUN_RE = re.compile(r"\d[\d -]*$")` — plus comment/docstring updates
stating the tradeoff. The bare-base64 cap (`_BARE_RUN_RE{1,39}`) was
considered and deliberately left alone: no recognized bare secret spans such
a cut (the exact-40 AWS token either matches whole on emission or its
<40 prefix is still held), so stream==batch there; uncapping it would only
add lag on benign runs.

GREEN: `R4-089_green_probe.log` (probe exit 0, all chunkings agree),
`tests/test_redaction_adversarial.py` 141 passed (119 pre-existing + 22 new),
adjacent `test_memory.py` + `test_security.py` + `test_memory_bug_fixes.py`
green, ruff check/format clean.

Explicit behavior change: a trailing digit run longer than 40 chars is now
withheld until the run terminates (previously its head streamed
immediately). Final flushed output is unchanged (== batch); benign long runs
only experience mid-stream emission lag. No batch-pattern behavior changed.

## 2. Calibration follow-ups + policy note (SSN/CC/sk-)

Probed through batch AND stream (chunk sizes 1, 7; stream==batch asserted):
labeled contiguous SSNs redact (`SSN 123456789`, `ssn=…`,
`social security number: …`); bare `123456789` survives; CC needs 13–19
digits AND Luhn (`4222222222222` 13-digit redacts, `4111-…-1111` dashed
redacts, while Luhn-valid `400000000002` 12-digit, Luhn-valid
`40000000000000000002` 20-digit, and Luhn-invalid 16-digit survive);
`disk-cache` survives the sk- left-boundary rule while a literal `sk-` lead
after a dash (`task-sk-AbCd…`) redacts — fail-closed, since `sk-` with a
20+-char body is distinctive and the cost of a miss exceeds a mangle.
Encoded as 14 additive calibration cases. They pass on unfixed code too —
they pin the fail-closed policy (must-not-change), not a behavior delta.

## 3. Notes

- Pre-existing suite was 119 green before the change; still 119 green after
  (141 total with the 22 additive cases) — no R4-016 regression.
- One adjacent run showed 3 failures in `test_memory*` (33 s wall, incl.
  `update_access_skips_quarantined` — another lane's bullet); three reruns
  of the identical tree pass 113/113 and 254/254 combined. Recorded as an
  environment flake (concurrent-lane DB contention), not caused by this
  diff, which cannot affect those modules.
- No commit (per lane instructions).

Verification commands:
`uv run python3 docs/verification/proofs/codebase_review_round4_2026-10-14/R4-089_probe.py`
`uv run pytest tests/test_redaction_adversarial.py -q`
`uv run pytest tests/test_memory.py tests/test_security.py tests/test_memory_bug_fixes.py -q`
`uv run ruff check machina/memory/redaction.py tests/test_redaction_adversarial.py && uv run ruff format --check machina/memory/redaction.py tests/test_redaction_adversarial.py`
