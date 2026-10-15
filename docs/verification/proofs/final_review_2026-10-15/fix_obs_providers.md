# fix_obs_providers — final-review providers/config + shutdown-ordering lane (2026-10-15)

Lane: FIX (final review). Sole-writer set:
`machina/core/provider/audit.py`, `machina/core/provider/sse.py`,
`machina/core/provider/rate_limit.py`, `machina/core/runtime.py`,
`machina/core/config.py` (rate-limit bound only), affected tests, and one new
evidence test file. No commits. No production file outside the lane was edited.

## Verdict

All seven findings fixed, RED-first where feasible (six primary findings
reproduced RED as a suite; the shutdown-ordering addendum separately reproduced
RED). 20/20 evidence tests GREEN; focused regression suites GREEN; ruff clean.

| # | Finding | Source | Sev | Status |
|---|---|---|---|---|
| F1 | Embedded-`@` DB-URL password tail leaked past metadata() | `adversarial_review.md` F1 | P2 | FIXED |
| SEC-2 | Presigned-URL signature params (`sig`, `X-Amz-Signature`, `X-Goog-Signature`) not redacted | relaunched security review | P2 | FIXED |
| SEC-3 | Password-only userinfo (`redis://:pw@host`) not redacted | relaunched security review | P3 | FIXED |
| F2 | `metadata()` `AttributeError` on non-string nested keys | `line_providers.md` P3-3 | P3 | FIXED |
| F3 | `**sanitized_kwargs` could override `event`/`timestamp` | `security_review.md` S6 | P3 | FIXED |
| F4 | Shared SSE parser did not strip a leading BOM | `line_providers.md` P3-4 | P3 | FIXED |
| F5 | `rate_limit_calls_per_minute = 0` unreachable (bound `(1, None)`) | `line_providers.md` P3-2 | P3 | FIXED |
| F6 | `_detect_providers` reported `anthropic` as unknown | `line_providers.md` P2-1 | P2 | FIXED |
| F7 | `runtime.shutdown` stopped audit writer before draining background | `line_gateway_obs_cli.md` F5 (addendum) | P3 | FIXED |

## Files changed

| File | Change |
|---|---|
| `machina/core/provider/audit.py` | F1: DB-scheme password group consumes to the last `@`; F2: `str(key).lower()` in `metadata()`; F3: reserved `event`/`timestamp` filtered from caller kwargs; SEC-2: `signature` suffix + exact `sig` (+ explicit `amzsignature`/`amzcredential`/`googsignature`/`googcredential`) credential params; SEC-3: optional username in the DB-scheme and userinfo regexes |
| `machina/core/provider/sse.py` | F4: strip a single leading UTF-8 BOM in `sse_field` |
| `machina/core/config.py` | F5: `rate_limit_calls_per_minute` bound `(1, None)` → `(0, None)` |
| `machina/core/runtime.py` | F6: explicit `anthropic` readiness arm (`ANTHROPIC_API_KEY`); F7: drain background tasks before audit stop |
| `tests/test_final_review_2026_10_15_providers.py` | **NEW** — RED-first evidence suite (28 tests) |
| `docs/verification/proofs/final_review_2026-10-15/fix_obs_providers.md` + `logs/fix_obs_providers_*.log` | evidence |

`machina/core/provider/rate_limit.py` needed no change: `_build_rate_limiter`
already accepts `0` and `AsyncTokenBucket.acquire` already short-circuits on
`rate == 0`; F5 was purely the config bound.

Marker names are unchanged (`[REDACTED_DB_PASSWORD]`, `[REDACTED_URL_PASSWORD]`,
`[REDACTED_CREDENTIAL]`, `[REDACTED]`, …).

## F1 — embedded-`@` DB-URL password (P2) — FIXED

RED (`test_embedded_at_db_password_tail_does_not_leak`,
`test_multi_at_db_password_tail_does_not_leak`):

```
_sanitize_value("postgres://alice:p@ss@db.example.com:5432/mydb")
  -> "postgres://alice:[REDACTED_DB_PASSWORD]@ss@db.example.com:5432/mydb"   # "ss" leaked
_sanitize_value("postgresql://u:se@cr@et@host/db")
  -> "postgresql://u:[REDACTED_DB_PASSWORD]@cr@et@host/db"                   # "cr@et" leaked
```

Root cause: the DB-scheme pattern's `([^@]+)@` class excludes `@`, so it can
only ever match up to the first `@` (greedy vs non-greedy is irrelevant for an
excluded delimiter). The later generic userinfo pass matched the remainder but
`_redact_userinfo_match` preserves anything starting with `[REDACTED`, so the
password tail survived.

Fix (`machina/core/provider/audit.py`): the password group consumes to the last
`@` before a path/query/fragment boundary — `([^/\s?#]*)@`, exactly like the
already-correct `_URL_USERINFO_RE`. Controls green: single-`@` DSN keeps
`[REDACTED_DB_PASSWORD]` (not rewritten to the URL marker), bare `user@host`
DSN untouched, generic `https://` embedded-`@` already handled.

GREEN:

```
postgres://alice:[REDACTED_DB_PASSWORD]@db.example.com:5432/mydb
postgresql://u:[REDACTED_DB_PASSWORD]@host/db
```

## SEC-2 — presigned-URL signature params (P2) — FIXED

Source: relaunched security review. Azure SAS `sig=` and AWS SigV4
`X-Amz-Signature` / `X-Goog-Signature` query params passed through
`_sanitize_value` and persisted to `audit_events` as a live replayable
credential. (`X-Amz-Credential` was already caught by the existing
`credential` suffix.)

RED (`test_azure_sas_sig_param_redacted_neighbors_intact`,
`test_aws_sigv4_signature_and_credential_redacted`,
`test_gcs_x_goog_signature_redacted`, `test_is_credential_param_precise_sig_rules`)
— the whole `sig`/`X-Amz-Signature`/`X-Goog-Signature` value survived.

Fix (`_is_credential_param`, precise rules):
* exact normalized `sig` (Azure SAS). Deliberately **not** a suffix — a
  `sig` suffix would wrongly catch benign `resig`/`insig`.
* suffix `signature` — covers `X-Amz-Signature` → `xamzsignature`,
  `X-Goog-Signature` → `xgoogsignature`, and a plain `signature`.
* explicit normalized `amzsignature` / `amzcredential` / `googsignature` /
  `googcredential` (subsumed by the suffix rules today, pinned so the intent
  survives future suffix-list edits).

Benign controls stay intact (`test_signature_like_benign_params_stay_intact`):
`signal=`, `design=`, `resig=`, `insig=`, `assigned=`, `X-Amz-Date=`,
`X-Amz-Expires=`, `X-Amz-SignedHeaders=`, `X-Amz-Algorithm=`.

GREEN:
```
…/doc?sig=[REDACTED_CREDENTIAL]&se=…&sp=r
…/k?X-Amz-Signature=[REDACTED_CREDENTIAL]&X-Amz-Expires=3600
…/b/o?X-Goog-Signature=[REDACTED_CREDENTIAL]&X-Goog-Expires=3600
```

## SEC-3 — password-only userinfo (P3) — FIXED

Source: relaunched security review. `redis://:pw@host` /
`postgresql://:pw@host` / `https://:pw@host` were not redacted: both the
DB-scheme rule (`([^:]+)`) and `_URL_USERINFO_RE` (`[^/\s?#@]+`) required a
non-empty username.

RED (`test_password_only_db_userinfo_redacted`,
`test_password_only_generic_userinfo_redacted`): the passwords survived.

Fix: the username group is optional in both patterns —
`([^:]*)` (DB-scheme) and `[^/\s?#@]*` (userinfo). Ports (`host:8080/path`
with no `@`) and bare `user@host` remain untouched (controls green).

GREEN:
```
redis://:[REDACTED_DB_PASSWORD]@host:6379/0
postgresql://:[REDACTED_DB_PASSWORD]@db.internal:5432/app
https://:[REDACTED_URL_PASSWORD]@example.invalid/doc
```

## F2 — `metadata()` non-string nested keys (P3) — FIXED

RED: `audit_log("x", data={1: "y"})` raised
`AttributeError: 'int' object has no attribute 'lower'` from `metadata()`.
Fix: `str(key).lower()` (mirrors the sibling `_is_sensitive_audit_key` /
`_is_credential_param`). Sensitive str-key redaction is unaffected
(`{"api_key": ...} -> {"api_key": "[REDACTED]"}` still pinned).

## F3 — reserved `event`/`timestamp` spoofing (P3) — FIXED

RED: `audit_log("real_event", event="spoofed", timestamp=0.0)` produced an entry
whose `event`/`timestamp` were the caller's values (both the persistence entry
and the `[AUDIT]` wire line). Fix: filter reserved keys from `sanitized_kwargs`
before building the entry, so `event`/`timestamp` are authoritative. Non-colliding
kwargs pass through unchanged.

## F4 — leading UTF-8 BOM in the shared SSE parser (P3) — FIXED

RED: `sse_event("\ufeffevent: message_start") is None` and
`sse_data("\ufeffdata: {}") is None`; a BOM-prefixed first `data:` frame was
dropped from a real `DirectProvider` stream. Fix: `sse_field` strips one leading
`U+FEFF` before matching (only one; a BOM mid-line is payload). Covered by a
parser test and a behavioral direct-stream test.

## F5 — `rate_limit_calls_per_minute = 0` (P3) — FIXED

RED: `validate_value("rate_limit_calls_per_minute", 0)` raised
`ValueError ... must be in range 1+, got 0`, so the documented "0 disables
throttling" hatch was unreachable. Fix: bound `(1, None)` → `(0, None)`.
`-1` still rejected. Pinned end-to-end: `config.get` → `reload_rate_limiters`
yields a zero-rate/zero-capacity bucket, and a zero bucket's `acquire()` returns
without waiting.

## F6 — anthropic readiness honesty (P2) — FIXED

RED: with `provider=anthropic` + a key, `_detect_providers()` reported
`chat_provider="unavailable"`, `reason="unknown provider 'anthropic'"`. Fix:
explicit `elif selected == "anthropic"` arm reading `anthropic_api_key`, reason
`"ANTHROPIC_API_KEY not set"` when absent. Removed providers (`claude`/`gemini`)
still report `unknown provider` (control green).

## F7 (addendum) — shutdown ordering: drain before audit stop (P3) — FIXED

Source: `docs/verification/proofs/final_review_2026-10-15/line_gateway_obs_cli.md`
F5. `RuntimeServices.shutdown` stopped the audit writer **before**
`drain_background_tasks`, while the gateway (`gateway/server.py:387-405`) does
the reverse. Consolidation/learning tasks emit `audit_log(...)` events; a submit
after the writer stops is dropped (`observability/audit.py:_drop`).

Fix (`machina/core/runtime.py`): move the background drain ahead of the audit
stop, keeping the same bounded timeout (`min(0.1 if cancellation else 5,
timeout)`) and the identical `CancelledError` capture / `Exception` logging. The
later DB close and embedder teardown are unchanged.

RED (ordering temporarily reverted, capture only):
`logs/fix_obs_providers_f7_red.log` shows the behavioral test failing with
`Audit event dropped during/after writer shutdown; event was not persisted` and
the ordering assertion failing (`["audit_stop", "drain"]`).

Two tests:
* `test_runtime_shutdown_drains_background_before_audit_stop` — structural pin:
  recorded call order is exactly `["drain", "audit_stop"]` (no DB needed).
* `test_runtime_shutdown_persists_audit_from_drained_task` — behavioral pin on a
  real test DB: a task in `pending_learning_reviews` that calls
  `audit_log("shutdown_drain_probe", ...)` still reaches `audit_events` after a
  full `runtime_services.shutdown()`.

## Verification

All commands run with the repo venv and `MACHINA_TEST_DATABASE_URL → machina_test`.

```
# RED (unfixed tree): 10 failed, 8 passed (controls) — F1..F6
.venv/bin/python -m pytest tests/test_final_review_2026_10_15_providers.py -q
# -> logs/fix_obs_providers_red.log

# RED (rules temporarily reverted): 6 failed, 2 passed (controls) — SEC-2/SEC-3
.venv/bin/python -m pytest tests/test_final_review_2026_10_15_providers.py -q \
  -k "sig or signature or password_only or sec"
# -> logs/fix_obs_providers_sec23_red.log

# GREEN: 28 passed (evidence suite alone)
.venv/bin/python -m pytest tests/test_final_review_2026_10_15_providers.py -q
# + runtime shutdown suites: 40 passed
.venv/bin/python -m pytest tests/test_final_review_2026_10_15_providers.py \
  tests/test_deep_runtime_shutdown.py tests/test_audit_shutdown.py \
  tests/test_audit_lifecycle.py -q
# -> logs/fix_obs_providers_green.log

# Focused regression sweep A: 349 passed
.venv/bin/python -m pytest \
  tests/test_r5_018_audit_redaction.py tests/test_r5_nb_source_redaction.py \
  tests/test_redaction_adversarial.py tests/test_security.py \
  tests/test_sse_stream_adversarial.py tests/test_r5_011_013_provider_transport.py \
  tests/test_fm2_provider.py tests/test_provider_rate_limit.py \
  tests/test_configuration_precedence.py tests/test_subscription_removals.py \
  tests/test_provider_store.py tests/test_provider_custom.py \
  tests/test_kimi_provider.py -q
# -> logs/fix_obs_providers_regression.log

# Redaction/audit regression sweep: 350 passed (SEC-2/SEC-3 re-run)
.venv/bin/python -m pytest tests/test_r5_018_audit_redaction.py \
  tests/test_r5_nb_source_redaction.py tests/test_redaction_adversarial.py \
  tests/test_fm5_redaction.py tests/test_security.py tests/test_memory_approval.py \
  tests/test_audit_regressions.py tests/test_fm6_audit.py \
  tests/test_fm3_audit_verdicts.py tests/test_audit_remediation.py \
  tests/test_provider_context_audit.py tests/test_core_audit_regressions.py -q
# -> logs/fix_obs_providers_regression.log (latest run)

# Focused regression sweep B: 168 passed, 1 skipped
.venv/bin/python -m pytest tests/test_runtime_migrations.py \
  tests/test_production_readiness_p2.py tests/test_integration_permissions.py \
  tests/test_deep_runtime_cleanup.py tests/test_soulspec_runtime.py \
  tests/test_provider_context_audit.py tests/test_audit_regressions.py \
  tests/test_core_audit_regressions.py tests/test_fm6_audit.py \
  tests/test_fm3_audit_verdicts.py tests/test_audit_remediation.py -q
# -> logs/fix_obs_providers_regression2.log

# Lint
.venv/bin/ruff check <changed files>        # All checks passed!
.venv/bin/ruff format --check <changed files>  # formatted
# -> logs/fix_obs_providers_ruff.log
```

## Logs

- `logs/fix_obs_providers_red.log` — evidence suite vs unfixed tree (10 failed / 8 passed), F1..F6.
- `logs/fix_obs_providers_sec23_red.log` — SEC-2/SEC-3 pre-fix (rules reverted): 6 failed / 2 passed.
- `logs/fix_obs_providers_f7_red.log` — F7 pre-fix (ordering reverted): audit event dropped.
- `logs/fix_obs_providers_green.log` — evidence + runtime shutdown suites (40 passed).
- `logs/fix_obs_providers_regression.log` — redaction/audit regression sweep (350 passed).
- `logs/fix_obs_providers_regression2.log` — focused sweep B (168 passed, 1 skipped).
- `logs/fix_obs_providers_ruff.log` — ruff check + format on changed files.

## Out of scope / residual

- F1 is fixed on the **audit** path (`_sanitize_value` → `redact_url_credentials`).
  `machina/memory/redaction.py` carries a separate copy of the same non-greedy
  DB pattern; it is outside this lane's sole-writer set and was not edited.
- F2's `metadata()` fix handles non-string keys, but `json.dumps` still cannot
  serialize a *tuple* dict key (a generic JSON limitation, not the `.lower()`
  finding); the evidence test scopes to int/float/None keys.
