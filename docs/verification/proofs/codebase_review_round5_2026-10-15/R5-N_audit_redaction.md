# R5-N · Audit-path credential redaction (finding 18) — evidence

## Verdict: FIXED (reproduced RED, then GREEN)

## RED (pre-fix, real index + audit-row read)
- Repro script: `logs/R5-N_repro_2026-10-15.py` (keyword-only `RAGPipeline`,
  isolated-schema private schema against `machina_test`, real `audit_persistence`
  writer, `audit_events` row read). Raw log: `logs/R5-N_red_2026-10-15.log`.
- `_sanitize_value("https://alice:example-passphrase@example.invalid/document")`
  returned the input verbatim; `redact_secrets` likewise — userinfo invisible
  to both redactors.
- A short credential query (`?api_key=short1`) also passed through: the
  generic-secret pattern needs ≥16-char values.
- All 4 persisted `rag_index_start`/`rag_index_complete` rows contained the
  raw password / raw query secret. The rendered `[AUDIT]` log line leaked too.

## Fix (3 owned files, smallest safe change)
1. `machina/core/provider/audit.py` — new shared helper
   `redact_url_credentials()`: redacts URI userinfo passwords
   (`scheme://user:password@` → `scheme://user:[REDACTED_URL_PASSWORD]@`,
   greedy to the last `@` so embedded-`@` passwords are fully covered) and
   credential-named query/fragment values (`[?&#;]name=value` →
   `[REDACTED_CREDENTIAL]`, any length/format; names normalized like
   `_is_sensitive_audit_key` + `auth`). Bare `user@host`, ports, IPv6, and
   non-credential params are preserved for debuggability. Wired as the LAST
   step of `_sanitize_value`'s string branch so existing markers
   (`[REDACTED_DB_PASSWORD]`, `[REDACTED_TOKEN]`, …) are preserved, never
   rewritten (guarded by `[REDACTED` prefix checks).
2. `machina/rag/pipeline.py` (audit-emission sites only) — both `audit_log`
   calls in `index_document` (`rag_index_start`, `rag_index_complete`) now
   pass `source=redact_url_credentials(doc.source)`. Storage paths
   (`document_source`, payloads) untouched — out of scope for this finding.
3. `machina/observability/audit.py` — `_run` re-sanitizes each entry with
   `_sanitize_value` (lazy import, no cycle) immediately before the
   `audit_events` INSERT, so a raw direct `submit()` bypassing `audit_log`
   still cannot persist a credential. Sanitizer failure fails closed to a
   marker row instead of dropping or persisting raw.

No API/behavior change for credential-free sources: safe strings, ports,
benign query params, and bare `user@host` pass through byte-identical.

## GREEN (post-fix)
- Same repro script: `leaked=False` on both vectors, `audit_events` rows
  clean — `logs/R5-N_green_2026-10-15.log` (script exit 1 = "NOT REPRODUCED"
  = fixed).
- New `tests/test_r5_018_audit_redaction.py`: 10 passed — 8 unit (userinfo,
  bare-user, short/long query, fragment, safe-string passthrough, DB-marker
  preservation, end-to-end sanitizer) + 2 DB E2E (real index of 3
  credentialed sources → 6 audit rows, zero secrets, host/user retained;
  raw direct `submit()` scrubbed at the persistence boundary).

## Regressions (all green, `MACHINA_TEST_DATABASE_URL` → local `machina_test`)
- `test_security.py` + `test_phase6.py` + `test_audit_regressions.py` +
  `test_r4_063_064_observability.py`: 75 passed
- `test_audit_remediation.py` + `test_fm5_redaction.py` +
  `test_fm5_consistency.py`: 78 passed
- `test_rag.py` + `test_memory_approval.py` + `test_rag_quality.py`: 135 passed

## Files changed (no commit, per lane rules)
- `machina/core/provider/audit.py` (shared URL-credential redactor + wiring)
- `machina/observability/audit.py` (persistence-boundary re-sanitize)
- `machina/rag/pipeline.py` (2 audit-emission call sites only)
- `tests/test_r5_018_audit_redaction.py` (new)
- This doc + `logs/R5-N_{red,green}_2026-10-15.log`,
  `logs/R5-N_repro_2026-10-15.py`
