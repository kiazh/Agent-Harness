# R5-Nb · RAG source redaction at the storage boundary (finding 18 remainder) — evidence

## Verdict: FIXED (reproduced RED, then GREEN)

## Scope

R5-N closed finding 18 for the **audit** path (`redact_url_credentials` wired
into audit emission and audit persistence). The residual was the RAG
**storage** path: `RAGPipeline.index_document` persisted `doc.source` through
`_redact_or_failed` alone (shared `redact_secrets`), which is blind to URI
userinfo passwords (`https://alice:pass@host/...`) and to credential
query/fragment values of any length (`?token=abc123`). The raw source reached
`context_chunks.document_source` and the stored msgpack payload.

## RED (pre-fix, real index + `context_chunks` row read)

- Repro script: `logs/R5-Nb_repro_2026-10-15.py` (keyword-only `RAGPipeline`,
  private `isolated_schema` against `machina_test`, real
  `audit_persistence`, real `context_chunks` + `audit_events` read). Raw log:
  `logs/R5-Nb_red_2026-10-15.log` (exit 1 = RED reproduced).
- Source under test (both vectors at once):
  `https://alice:example-passphrase@example.invalid/doc?api_key=short1&x=1`.
- `redact_secrets(SRC)` returned the input verbatim; `_redact_or_failed(SRC)`
  likewise — the storage redactor saw no secret.
- All three stored source fields leaked the raw password and query value:

  | stored field | raw leak |
  |---|---|
  | `context_chunks.document_source` | yes |
  | `payload.metadata.source` | yes |
  | `payload.source` | yes |

- Audit rows (`rag_index_start` / `rag_index_complete`) were already clean
  (the R5-N lane), so the RED is specifically the storage path.
- New suite RED: `6 failed, 1 passed` — `logs/R5-Nb_test_red_2026-10-15.log`.

## Fix (one owned source file, smallest safe change)

`machina/rag/pipeline.py`:

- New `_redact_source(source)`: runs `_redact_or_failed` then the shared
  `redact_url_credentials` from `machina/core/provider/audit.py`. **One
  authoritative implementation — the regexes are reused, never copied.**
  Fail closed: any exception from either stage yields `REDACTION_FAILED`,
  never the raw source (mirrors `_redact_or_failed`).
- Every source path now routes through it:
  - `rag_index_start` / `rag_index_complete` audit emission
    (`source=_redact_source(doc.source)`) — previously called
    `redact_url_credentials` directly;
  - `doc_source = _redact_source(doc.source)`, reused verbatim for the
    `document_source` column, `payload.metadata.source`, and `payload.source`.
    Reusing the one computed value also makes the delete/lock key
    (`document_source`) provably identical to what is stored.

No behavior change for credential-free sources: safe paths, ports, IPv6,
benign query params, and bare `user@host` pass through byte-identical.

## GREEN (post-fix)

- Same repro script: `GREEN: no raw credential in storage or audit rows`,
  exit 0 — `logs/R5-Nb_green_2026-10-15.log`. Stored value:
  `https://alice:[REDACTED_URL_PASSWORD]@example.invalid/doc?api_key=[REDACTED_CREDENTIAL]&x=1`
  (host, username, path, and `x=1` retained).
- New `tests/test_r5_nb_source_redaction.py`: **7 passed**
  (`logs/R5-Nb_test_green_2026-10-15.log`) — 5 unit (both vectors redacted
  with context retained; benign source untouched; bare userinfo preserved;
  fail-closed when the URL redactor raises; fail-closed when the secret
  detector raises) + 2 DB E2E (real index of the combined source → every
  stored row field credential-free with redacted identifiers retained; audit
  rows credential-free with host/user retained).

## Regressions (all green, `MACHINA_TEST_DATABASE_URL` → local `machina_test`)

| Check | Command | Result |
|---|---|---|
| Required suites | `pytest tests/test_rag.py tests/test_audit_regressions.py` | 75 passed (`logs/R5-Nb_required_suites_green_2026-10-15.log`) |
| Related suites | `pytest tests/test_core_audit_regressions.py tests/test_r5_018_audit_redaction.py tests/test_fm5_consistency.py tests/test_r4_026_rag_workspace_resolver.py tests/test_deep_rag_backfill.py` | 37 passed (`logs/R5-Nb_related_suites_green_2026-10-15.log`) |
| Lint | `ruff check` + `ruff format --check` on both files | clean |

Note: an earlier bundled run that also included `test_rag_quality.py` hit a
transient cross-suite PostgreSQL `DeadlockDetectedError` in
`test_document_benchmark_real_postgres`; it passes in isolation and is
unrelated to this change (DB test-infra lock contention, not redaction).
Raw output kept as `logs/R5-Nb_rag_adjacent_transient_deadlock_2026-10-15.log`.

## Files changed (no commit, per lane rules)

- `machina/rag/pipeline.py` (`_redact_source` + source-redaction call sites only)
- `tests/test_r5_nb_source_redaction.py` (new)
- This doc + `logs/R5-Nb_{red,green}_2026-10-15.log`,
  `logs/R5-Nb_repro_2026-10-15.py`,
  `logs/R5-Nb_test_{red,green}_2026-10-15.log`,
  `logs/R5-Nb_{required,related,adjacent_suites}_green_2026-10-15.log`
