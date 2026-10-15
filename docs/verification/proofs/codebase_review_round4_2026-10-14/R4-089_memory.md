# R4-089 memory (dedup / approval gate / update_access / shared_bus) — proof

Lane: consolidator.py, approval.py, store.py, shared_bus.py (+ tests).
NOT this lane: `machina/memory/redaction.py` (streaming digit-run hold,
SSN/CC/sk- calibration + corpus) — owned by the REDACT lane, already
committed upstream (970bc57, 948f61e); their evidence is
`R4-089_redaction_streaming.md` + `R4-089_probe.py` / `R4-089_red.log` /
`R4-089_red_pytest.txt` / `R4-089_green_probe.log`. Reported as deferred
below; this file covers only the four non-redaction bullets.

Mock boundary: fakes only at the provider/embedder boundary (LLM completion,
embedding service). Chunks, sessions, dedup, writes, receipts all hit the
real `machina_test` DB. `update_access` uses real SQL against the real DB.
The shared-bus test deletes/re-inserts a real `memory_provenance` row.

## (a) Cosine dedup runs on the production path

Root cause: `_extract_memories` builds candidates with `embedding=None` and
the embed-before-store stage ran AFTER the dedup comparison, so on the real
path only exact normalized-text dedup ever fired.

Fix (`machina/memory/consolidator.py:296-324`): the redaction-first
embedding stage moved to step 3.5, BEFORE the step-4 comparison; threshold
semantics unchanged (`dedup_threshold`, default 0.85). Candidates that still
lack an embedding (no provider, failure, empty content) keep
normalized-content dedup. Tests:
`tests/test_memory_bug_fixes.py:813` (`TestConsolidatorDedupEmbedding`,
stored-paraphrase + intra-batch-paraphrase cases, threshold 0.85,
fake embedder maps same-cluster paraphrases to cosine 1.0).

- RED (`R4-089_memory_red.log`, detached worktree at pre-fix HEAD + new
  tests): `a paraphrase above the 0.85 cosine threshold must not be
  stored` (2 memories written, audit shows `new_memories_written: 1`);
  `intra-batch paraphrases must collapse to one memory` (`assert 2 == 1`).
- GREEN (`R4-089_green_head.log`): both pass; production consolidation
  writes exactly one memory and `update_access` fires on the dedup match.

## (b) Memory approval gate wired into MemoryStore.add (decision: wire)

Fix: `MemoryStore.add` (`machina/memory/store.py:83-117`) consults
`gate.is_enabled()` and routes to `pending_memories` via `submit()` when
on. Switch: `memory_approval_enabled` config (default OFF —
`machina/core/config.py:242,484`), OR the gate's explicit constructor flag
(`machina/memory/approval.py:119-132` `is_enabled()`); both the write
router and `submit`'s auto-approve use this single resolution, so an
enabled request can never be auto-approved by a default singleton.
`approve`/`approve_all` (`approval.py:260,419`) and the shared-bus delivery
copy pass `bypass_approval=True` so completions never re-enter the gate.
The gate↔store import cycle is resolved via a sys.modules/importlib seam
(`store.py:31-45`); architecture guards stay green. E2E tests:
`tests/test_memory_approval.py:536` (`TestApprovalGateWiring`).

- RED (`R4-089_memory_red.log`): `the write must land in
  pending_memories` (write landed durably instead); reject-path test shows
  `assert 1 == 0` on the memories count (memory durable before review).
- GREEN (`R4-089_green_head.log`): enabled → write lands in
  `pending_memories` with zero durable rows, approve creates exactly one
  durable memory linked by `memory_id`, reject creates none and marks the
  row `rejected`; disabled (default) → direct durable write, zero pending
  rows (behavior unchanged).

## (c) update_access quarantined filter

Fix (`machina/memory/store.py:400-412`): `update_access` now carries the
same `AND quarantined = FALSE` filter as `batch_update_access`
(`store.py:421`) — quarantined rows are unretrievable and must not accrue
access evidence. Test: `tests/test_memory_bug_fixes.py:895`
(`TestUpdateAccessQuarantined`, real DB).

- RED (`R4-089_memory_red.log`): `assert 1 == 0` on the quarantined row's
  `access_count` (it was bumped to 1).
- GREEN (`R4-089_green_head.log`): quarantined row keeps
  `access_count == 0` / `last_accessed IS NULL`; active row bumps to 1.

## (d) shared_bus retryable receipts

Root cause: `deliver` cached EVERY outcome one-per-(source, recipient),
including a rejection from a transiently missing `memory_provenance` row —
permanently blocking the retry once the row became visible.

Fix (`machina/memory/shared_bus.py:93-150`): a rejection caused by
`provenance is None` returns a receipt WITHOUT a durable
`shared_memory_deliveries` insert (retryable). All conclusive outcomes
(accepted / quarantined / source-quarantined / mismatched-or-tampered
provenance / recipient-gate rejection) keep the one-per-pair durable
receipt incl. the ON CONFLICT winner path. Test:
`tests/test_shared_memory_bus.py:91`
(`test_missing_provenance_rejection_is_retryable`, real DB: delete
provenance row → deliver → re-insert row → deliver).

- RED (`R4-089_memory_red.log`): `a transient provenance miss must not
  become a durable rejection` (`assert 1 == 0` on the receipts count).
- GREEN (`R4-089_green_head.log`): first delivery `rejected` with zero
  receipt rows; retry after the row reappears `accepted` with a target copy.

## Verification

- Required suites (real `machina_test` DB, `-p no:cacheprovider`):
  `R4-089_green_head.log` → **190 passed** (test_memory,
  test_memory_bug_fixes, test_persona_memory, test_memory_approval,
  test_shared_memory_bus, test_architecture_layers). Earlier attempt
  history incl. shared-DB deadlock retries:
  `R4-089_contention_attempts.log`, `R4-089_contention_attempts_head.log`
  (lock-error attempts retried per rule; every rotating failure also passed
  solo; clean window → 190/190). A prior 190-pass run:
  `R4-089_required_suites_green.log`.
- Adjacent suites (same DB): 402 passed (approval_batch,
  embedding_storage, fm5_consistency, embedding_failopen_adversarial,
  fm5_redaction, gateway_features, research_integration,
  leaf_independence, memory_rl, audit_remediation, agent_factory_memory,
  embedder_local, gateway_memory_usage, redaction_adversarial,
  fm3_approval_store) — re-verified at current HEAD 2026-10-15.
- `ruff check` + `ruff format --check`: clean on all touched files.
- Pre-existing suites that pin the touched seams still pass unmodified
  (e.g. `TestConsolidatorBatchDedup`, approval approve/reject with mocks,
  `test_concurrent_delivery_yields_one_copy_and_one_receipt`).

## Deferred (not this lane)

- Streaming redactor digit-run hold (`redaction.py:491-618`) + long-digit
  stream test — REDACT lane (committed 948f61e; evidence
  `R4-089_redaction_streaming.md`).
- R4-016 calibration follow-ups (SSN/CC/sk- + policy note) — REDACT lane
  (committed 970bc57).
- `user_profile.py` (sole-writer file, no change): reviewed at HEAD —
  write/read paths already fail-closed on redaction failure (W8) and
  `get_or_create` is race-safe (N8); no defect from this task's bullets
  applies, so no edit per smallest-change rule.

## Residuals

- Shared-`machina_test`-DB lock contention with concurrent lanes caused
  rotating `DeadlockDetectedError` flakes during verification; all resolved
  on retry and none reproduce solo. No code residual.
- `config.py` carries other lanes' uncommitted changes in the same working
  tree; this lane's addition is the 4-line `memory_approval_enabled`
  default/field only.
- `tests/test_memory.py` has another lane's uncommitted test-truth
  tightening; untouched by this lane.
