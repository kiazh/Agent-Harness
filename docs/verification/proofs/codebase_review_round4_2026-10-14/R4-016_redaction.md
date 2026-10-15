# R4-016 · Redaction under-match/tail-leak + over-match guards

- **Task:** `docs/verification/FIX_PLAN_ROUND4_TASKS.md` §R4-016 (P1).
- **Status:** implemented in the working tree; **NO commit** (parent orchestrator owns the commit).
- **Files changed:** `machina/memory/redaction.py` (pattern engine + streaming partials);
  `tests/test_redaction_adversarial.py` (xfail markers removed only — no expectation changed).
  No other source/test file touched; no new dependencies (stdlib `re` only).
- **Evidence logs:** this directory (`R4-016_*`).

## 1. RED evidence (before)

Contemporaneous capture, corpus with its original strict-xfail markers:

```
pytest tests/test_redaction_adversarial.py -q -p no:cacheprovider --runxfail
37 failed, 82 passed
```

Failing classes: 22 batch secret leaks (11 cases × plain/prose) + 11 streaming
leaks + 4 benign over-matches = **37**.

Reconstructed reproduction against the pre-fix engine (`git show <HEAD>:machina/memory/redaction.py`,
loaded via a conftest import shim) — failing set identical:

```
37 failed, 82 passed        # R4-016_red_reconstructed_pytest.txt
```

## 2. GREEN evidence (after)

| Check | Result | Log |
| --- | --- | --- |
| `pytest tests/test_redaction_adversarial.py -q -p no:cacheprovider` | **119 passed, 0 xfailed** | `R4-016_green_corpus.txt` |
| same with `--runxfail` (no hidden failures) | 119 passed | `R4-016_green_corpus.txt` |
| `pytest tests/test_fm5_redaction.py tests/test_review_secrets.py tests/test_memory.py tests/test_handoff_regressions.py -q` | 129 passed | `R4-016_green_required_suites.txt` |
| `pytest tests/test_architecture_layers.py -q` (ratchet) | 25 passed | `R4-016_green_architecture.txt` |
| redaction-adjacent sweep (every test file referencing redaction, 29 files) | 609 passed in the clean pre-merge run; later runs pick up **other** tasks' in-flight edits — see classification below | `R4-016_green_redaction_adjacent_sweep.txt` |
| full backend suite (clean run after all behavior-affecting changes, tree consistent) | 4444 passed, 1 skipped | `R4-016_green_full_suite.txt` |
| `ruff format --check` + `ruff check` | clean | `R4-016_ruff_format.txt`, `R4-016_ruff_check.txt` |

**Flip count: 37/37 strict xfails now hard-pass; 0 deliberately remaining.**
No assertion was weakened; the only corpus edits are removal of `broken=…`
arguments / xfail markers for cases that now pass, plus the module docstring.

> Note on suite churn: this repo is being edited by parallel R4 tasks. Later
> full-suite/adjacent runs picked up *other* tasks' in-flight edits only —
> e.g. `Config has no attribute 'memory_approval_enabled'` from the concurrent
> R4-089 gate work, UI typecheck errors from R4-094's TUI test, and transient
> collection ImportErrors while `rag/search.py` / `skills/repo_install.py`
> were saved mid-run. `git stash push -- machina/memory/redaction.py`
> reproduces the approval failure identically without this change, and none
> of the other failures touch redaction. All scoped suites above are green at
> HEAD `e45cd6b`.

Reproducibility artifacts in this directory: `R4-016_redaction_before.py`
(byte-identical to the pre-fix module; sha1 `1d6bdb94…`, equals
`git show e45cd6b:machina/memory/redaction.py`), `R4-016_probe.py`
(before/after + fuzz + idempotence probe) and `R4-016_bench_probe.py`
(micro-benchmark).

## 3. Pattern-engine changes (mapping to the fix steps)

1. **Ordering** — `generic_quoted_key` and the new `generic_quoted_secret`
   now run **before** `generic_secret`. Quoted values are captured whole,
   any charset, min length 8 (same guard as the existing quoted-key rule).
2. **Unquoted value class** — `generic_secret` widened from
   `[a-zA-Z0-9\-_]{16,}` to `[a-zA-Z0-9_\-+/=.~]{16,}` (URL-safe base64,
   dotted and `~` payloads).
3. **Post-pass re-scan** — after a pass that replaced text, the engine
   re-scans the output once with the patterns that fired (`only=`), guarded
   so it never rewrites text already containing a `[REDACTED_…]` marker.
   Without the marker guard the re-scan could swallow a brace after a marker
   (`password=[REDACTED_PASSWORD]}` → `…]`); with it, the re-scan can only
   add precision. Restricting to fired patterns keeps the hot path bounded —
   only a pattern that replaced text can have left a tail.
4. **Over-match guards**
   - `bearer_token`: value must be token-shaped and ≥ 16 chars
     (`(Bearer\s+)[A-Za-z0-9\-._~+/]{16,}=*`).
   - `sk-` family: left boundary "not preceded by an alphanumeric character".
     Implemented as a substitution callback (`_LEFT_BOUNDED`) instead of a
     regex lookbehind: a lookbehind defeats CPython's literal-prefix scan
     (~12 ms/MB per pattern), the callback pays per match only. `task-…`
     therefore survives; keys after `=`, `"`, `:`, space, `_`, start still
     redact.
   - `ssn_contiguous`: no longer a bare `\b\d{9}\b`; requires an explicit
     label + separator (`ssn|social security` `(number)?` `[=:]?` then
     `\d{9}\b`). Bare 9-digit numbers (`387420489`) survive; `SSN: 123456789`
     and `social security number 123456789` still redact. (`ssn` dashed form
     unchanged.)
   - `credit_card`: quantifier `{13,16}` → `{13,19}`, Luhn gate retained.
5. **JWT** — all three segments accept base64 `=` padding
   (`eyJ[A-Za-z0-9_=-]*\.eyJ[A-Za-z0-9_=-]*\.[A-Za-z0-9_=-]*`).

**Streaming (`StreamingSecretRedactor` partials).** The same rules feed the
frontier: sk-family partials carry the equivalent lookbehind (so `task-…`
streams through untouched and equals whole-string output); the generic
assignment partial accepts quoted any-charset values and the widened unquoted
class; a new labeled-SSN partial holds from the label (prefix-enumerated so
mid-word deltas keep the hold). The pre-existing >40-char digit-run hold was
**not touched** (R4-089 owns it).

## 4. Before/after table per corpus class

Full probe output: `R4-016_before_after_probe.txt`. Abridged (values synthetic):

| Corpus class | Before | After |
| --- | --- | --- |
| `token=abc.defghijklmnop.qrstuvwxyz0123` | unchanged (full leak) | `token=[REDACTED_SECRET]` |
| `secret=ABCDEFGHIJKLMNOP.qrstuvwxyz012345` | `secret=[REDACTED_SECRET].qrstuvwxyz012345` (tail) | `secret=[REDACTED_SECRET]` |
| `token="abc.defghijklmnop.qrstuvwxyz0123"` | unchanged (full leak) | `token=[REDACTED_SECRET]` |
| `secret="ABCDEFGHIJKLMNOP.qrstuvwxyz012345"` | `secret=[REDACTED_SECRET].qrstuvwxyz012345"` (tail) | `secret=[REDACTED_SECRET]` |
| `access_token=abc.…` | unchanged (full leak) | `access_token=[REDACTED_SECRET]` |
| `api_key=abc.…` | unchanged (full leak) | `api_key=[REDACTED_SECRET]` |
| `token=Zm9v…+KzEy/MzQ1Njc4OT=` | `token=[REDACTED_SECRET]+KzEy/MzQ1Njc4OT=` (tail) | `token=[REDACTED_SECRET]` |
| `secret="Zm9v…+KzEy/MzQ1Njc4OTA="` | `secret=[REDACTED_SECRET]+KzEy/MzQ1Njc4OTA="` (tail) | `secret=[REDACTED_SECRET]` |
| `token=<padded JWT>` | `token=[REDACTED_SECRET].<payload>.SflKx…` (payload+sig leak) | `token=[REDACTED_JWT]` |
| `card 40000000000000006 charged` (17-digit, Luhn-valid) | unchanged (full leak) | `card [REDACTED_CC] charged` |
| `card 4000000000000000006 charged` (19-digit, Luhn-valid) | unchanged (full leak) | `card [REDACTED_CC] charged` |
| `the report was a bearer of bad news` | `…a bearer [REDACTED_TOKEN] bad news` | unchanged |
| `the bearer token is not configured yet` | `the bearer [REDACTED_TOKEN] is not…` | unchanged |
| `task-0123456789abcdef0123456789` | `ta[REDACTED_OPENAI_KEY]` | unchanged |
| `387420489` | `[REDACTED_SSN]` | unchanged |

Streaming: all 11 streamed leak classes (same 11 secret cases) now pass at
every 2-way split and at chunk sizes 1/2/3/5/8/13 — the corpus streaming
tests and the LP-09 equality tests (`test_handoff_regressions.py`, FM5) are
green.

## 5. Hot-path micro-benchmark

`R4-016_bench.txt` (stdlib `time`, pre-fix module vs current, same interpreter):

| Input | Before | After | Ratio |
| --- | --- | --- | --- |
| 107 B log line with 1 secret | 0.040 ms | 0.046 ms | 1.14x |
| ~5 KB | 1.70 ms | 1.94 ms | 1.14x |
| ~64 KB | 20.1 ms | 24.1 ms | 1.20x |
| pathological 1.8 MB all-secret blob | 281 ms | 449 ms | 1.60x |

The pathological delta is the extra quoted-secret scan + fired-pattern
re-scan; realistic per-call costs are tens of microseconds. The first
lookbehind-based draft was 3x on the large blob; the callback boundary check
and fired-only re-scan brought it to the numbers above.

## 6. Residual risks / deliberately out of scope

- **>40-char digit-run streaming hold** — untouched; belongs to R4-089
  (flagged in the task text). No corpus case depends on it.
- **Labeled-SSN strictness** — `SSN: 123456789`, `ssn 123456789`,
  `social security number 123456789` redact; prose-bridged forms such as
  `ssn is 123456789` do not (deliberate: avoids bridging over arbitrary
  text). Bare 9-digit numbers always survive.
- **Bearer recall trade-off** — token values shorter than 16 chars are no
  longer redacted (`Bearer abc`). Spec-mandated over-match guard.
- **Unquoted generic values with exotic characters** — a value containing a
  char outside `[A-Za-z0-9_\-+/=.~]` (e.g. `!`) still matches only up to the
  break char for the unquoted form; the quoted form covers any charset.
  Pre-existing shape, not exercised by the corpus.
- **Marker-bearing input re-redaction** — `password=[REDACTED_PASSWORD]}`
  loses the brace when explicitly redacted *again* (pre-existing; the new
  internal re-scan is marker-guarded, so `redact()` is stable on its own
  output for corpus/fuzz inputs).
- **`has_secrets`** — still a coarse probe; `task-…` returns True (it was
  True before this change as well; the redaction output is correct).
- **Named 40-char AWS secrets** — `aws_secret_key` still matches exactly 40
  base64 chars, so a longer value keeps a tail (pre-existing, not covered by
  R4-016).

## 7. Repo hygiene

- `memory/redaction.py` is **not** listed in `tests/test_architecture_layers.py`
  `GOD_FILE_CEILINGS` (grep count 0); the ratchet suite is green. File grew
  787 → 899 lines, all in the reviewed delta.
- `redact_value` / `redact_dict` / `redact_secrets`/`StreamingSecretRedactor`
  share the same `_PATTERNS`/`_PARTIAL_RES`, so every call path receives the
  fix.
- Adversarial probe: 300 random 16–48-char URL-safe/dotted values inside
  `token=`/`secret=`/`api_key=` (plain and quoted contexts) → 0 fragment
  leaks; redaction is idempotent on every corpus case.
