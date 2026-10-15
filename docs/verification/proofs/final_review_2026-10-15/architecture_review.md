# Final-phase architecture review — round diff `cdc5232..HEAD`

Date: 2026-10-15 · Reviewer: senior-architect (read-only) · HEAD: `1e1edbf`
Scope: `git diff cdc5232..HEAD` + the tree it produces. No file in the tree was
modified; only this evidence file was written.

Method: read the round diff, the repo's own architecture guards
(`tests/test_architecture_layers.py`, `tests/test_architecture_pairs.py`), the
persistence DDL and its migration path, and the new test files. Assertions were
verified by reading code + tests (no runtime execution). Number/census claims
were re-measured locally with `wc -l` and an AST walk identical to the guard's.

## Verdict summary

- **Ratchet honesty: HONEST.** Every moved ceiling in `a424c26` equals the file's
  measured line count exactly; the delta table sums exactly; the deferred-import
  census (687) is at or under the cap (689). Details in the dedicated section.
- **P1 findings: none.** No correctness/security regression that survives the
  documented boundaries.
- Module boundaries, SSE consolidation, and the persistence migrations are sound.
- Three duplication/layering residuals (redaction ownership, credential-store
  fencing, tracing module names) and two lifecycle/ordering residuals.

---

## Findings

### P2-1 — A cross-cutting redaction primitive is owned by the provider package, then imported back by `rag` and `observability` (layer inversion); a second token/secret redactor already exists in `memory`
Cites: `machina/core/provider/audit.py:22-74` (`_sanitize_value` reimplements
`sk-*`/`gh*`/Bearer/password/DB-URL patterns that
`machina/memory/redaction.py:118-200` already owns), `machina/core/provider/audit.py:145`
(`redact_url_credentials`, the new "shared" URL redactor),
`machina/rag/pipeline.py:18,63-71` (imports `redact_url_credentials` from the
provider package), `machina/observability/audit.py:94` (lazy private import of
`_sanitize_value` from the provider package).
Why it matters: a security primitive that is used by the RAG leaf and the
observability leaf now lives in `machina.core.provider.*`; those are leaf→core
edges the layer guard does not even cover (`observability` is not pinned in
`test_architecture_layers.py`, and `rag`'s core-import ceiling was consumed, not
shrunk). The bare regex set in `_sanitize_value` and the pattern table in
`memory/redaction.py` are two implementations of "redact a secret from text" that
drift independently.
Recommendation: hoist URI/credential redaction (and ideally the shared pattern
set) into a leaf module (`machina.memory.redaction` or a new
`machina.security.redaction`) and have `audit.py`, `observability/audit.py`, and
`rag/pipeline.py` call it; keep `_sanitize_value` as a thin caller.

### P2-2 — `store_fence.py` bills itself as the single credential-store fencing implementation, but its own package keeps a parallel bounded-flock, and two more live elsewhere
Cites: `machina/security/store_fence.py:1-27,138-186` (claim + `store_lock`),
`machina/security/providers.py:93-145` (`_store_lock` + `_acquire_lock`, an
independent 5 s bounded flock in the *same* package),
`machina/connectors/oauth_store.py:134-165` (`_file_lock_acquire`),
`machina/security/oauth_keys.py:154-200` (`_acquire_creation_lock`),
`machina/interop.py:664-696` (`_state_lock`, cited by `store_fence` as the origin
idiom). Five flock loops exist; only `chatgpt_auth.py` and `copilot_auth.py`
adopt `store_fence.store_lock`.
Why it matters: the docstring "One authoritative implementation of the mechanics
idiom" is not true of the tree, so the next writer has no single place to fix a
lock/timeout regression. `providers.py` already imports `store_fence` for
`chmod_temp_file`, so the dependency seam exists.
Recommendation: make `providers._store_lock` delegate to
`store_fence.store_lock(target)` with a parameterized timeout, and fold the OAuth
store/key helpers onto the same implementation (or reword the docstring to name
which store owns which loop).

### P3-1 — Audit-writer singleton: start/stop guards are asymmetric, and the test harness recovers by poking privates
Cites: start at `machina/gateway/server.py:503-524` (nested under
`if not self._db_ready:` → unconditional on ownership); stop at
`machina/gateway/server.py:382-397` (nested under
`if self._db_ready and self._owns_db:`). The HTTP path is covered by the
symmetric pair in `machina/core/runtime.py:122,231`; the asymmetry only bites an
embedded `Gateway(owns_db=False)` that is *not* fronted by `runtime_services`
(no stop) or one whose `_db_ready` was pre-seeded true (no start → audit events
drop as "before-start"). `tests/conftest.py:161-177` normalizes a leaked owner by
assigning `audit_writer._owners/_task/_queue` directly and leaves `_ever_started`
set.
Recommendation: add an explicit `AuditPersistence.reset()`/generation method (and
reset `_ever_started`), or make `stop()` tolerate a task bound to a foreign loop,
so the singleton's lifecycle is recoverable without tests reaching into privates.

### P3-2 — Event-ordinal ordering is incomplete in the compaction snapshot
Cites: `machina/core/context.py:578,595` (`get_chunks_before` keyset ORDER BY
`created_at DESC, id DESC`, left unchanged), consumed by
`get_all_chunks` (`context.py:683`) → compression.
Why it matters: HELD-55 fixed replay order (prompts, transcript, consolidation)
but the compression input set is still UUID-ordered *within a batch*, so the
summarizer can see a batch's events scrambled. Documented as intentional
("compaction consumes a set, not a replay"), which is defensible; flagged so the
asymmetry is known rather than accidental.
Recommendation: accept as-is (set semantics) but state the compression-ordering
boundary in the HELD-55 residual note; or add `event_seq` to that keyset in a
follow-up.

### P3-3 — Consolidation cursor vs sort key: the "cannot skip or duplicate" claim is narrower than stated
Cites: `machina/core/context.py:605-632` (`get_chunks_since` WHERE cursor stays
`(created_at, id)` while ORDER BY gains `event_seq` ASC NULLS LAST),
`machina/memory/consolidator.py:310-321` (`_checkpoint_for` = max processed
`(created_at, id)`), `MAX_CHUNKS_PER_CONSOLIDATION = 100`
(`consolidator.py:73`). Residual claimed in
`docs/.../HELD-55_history_order.md:114` and owned by HELD-F1.
Analysis: for a batch that fits one page the max-`(created_at,id)` cursor excludes
the whole batch (safe). For a batch larger than the 100-row page, the checkpoint
is the lexicographically-largest id among the first 100 by `event_seq`, so
unprocessed rows in the same batch with a smaller id are never returned again →
missed extraction. No duplicate risk, but a skip risk at `batch_size > 100`.
`add_chunks_batch` has no cap (`context.py:214`, `agent.py:1324`), so the
boundary is unenforced.
Recommendation: either cap the flush batch at the consolidation page size, or
thread `event_seq` through the cursor (HELD-F1 scope). At minimum, narrow the
HELD-55 doc's "cannot skip or duplicate" to "while a batch fits one page".

### Q-1 — Test files pin some private names / literals
Cites: `tests/test_r5_018_audit_redaction.py:21` imports the private
`_sanitize_value`; `tests/test_r5_011_013_provider_transport.py:30,169-177` pins
`_SSE_FILES` and scans sources for the literal `'startswith("data: ")'`.
These are unit-level adversarial tests with boundary mocks elsewhere; the
source-scan is an intentional anti-duplication guard. Acceptable, but they couple
to names that are free to change.
Recommendation: keep, but assert through the public `audit_log`/`_sanitize_value`
seam and treat the source scan as a lint-style check with a comment that it is
structural.

### Positive: two "second implementation" candidates were correctly avoided
- One SSE parser: `machina/core/provider/sse.py`; all five provider transports
  import `sse_data`/`sse_event` (`grep` for manual `startswith("data`/`[6:]` is
  empty). `api/routes/streaming.py:97-102` emits SSE — a producer, not a parser.
- The RAG audit fix reuses `redact_url_credentials` rather than adding a second
  URL regex (`rag/pipeline.py:63-71`), and `execution_context.py` re-exports the
  single `session_mode` turn-scope rather than re-implementing it.
- `observability/tracing.py` (OTel `span`) and `observability/traces.py`
  (in-process run tree) are two back-ends over the same events, not two copies of
  one implementation — noted as a naming ambiguity, not a duplication finding.

---

## Ratchet honesty verdict (commit `a424c26`)

**HONEST.** Re-measured on the current tree (`wc -l`, identical to the guard's
`_line_count`):

| ceiling | tree | | ceiling | tree |
|---|---|---|---|---|
| core/agent.py 2110 | **2110** | | provider/audit.py 219 | **219** |
| provider/rate_limit.py 120 | **120** | | provider/sse.py 46 | **46** |
| provider/ollama.py 388 | **388** | | provider/openrouter.py 608 | **608** |
| provider/direct.py 348 | **348** | | provider/anthropic.py 580 | **580** |
| provider/chatgpt.py 680 | **680** | | provider/copilot.py 427 | **427** |
| provider package total 4958 | **4958** | | | |

- Delta table arithmetic checks exactly: provider `+178 = 72+46+41+10+3+2+2+1+1`
  and `4780+178 = 4958`; agent `+55 = 9 (R4-025) + 46 (R5-F1)`, `2055→2110`.
- Deferred-import census measured = **687 ≤ 689 cap** (AST walk over `machina/`,
  function-body ancestors only, matching `_deferred_import_count`). Honest.
- One prose discrepancy (not dishonesty): the deferred-import table's final entry
  records "census 688"; the tree measures 687 — one row of slack. The cap is a
  ceiling, so the guard is still truthful.
- Stale-slack note: `core/compression.py` ceiling 999 vs actual 852;
  `provider/transport.py` 382 / `base.py` 60 unchanged. These "only fall" so they
  are honest, but the ceiling no longer tracks the file — a dead-ish guard for
  those entries, not an inflated one.

---

## Persistence-format changes

All three are additive/idempotent and preserve legacy semantics:

- **`event_seq` ordinal** (`schema.sql:99-113`, `connection.py:22-33,247`):
  `CREATE SEQUENCE IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS` /
  `ALTER COLUMN … SET DEFAULT` / `ALTER SEQUENCE … OWNED BY` /
  `CREATE INDEX IF NOT EXISTS`, applied by `initialize_runtime_schema` under the
  existing `machina:runtime_migrations` advisory lock after the fragment files.
  Legacy rows keep `event_seq NULL`; every touched reader uses
  `event_seq DESC/ASC NULLS LAST`, so legacy rows fall back to the old
  `(created_at, id)` order. `tests/test_held_55_history_order.py:125-210` pins
  double-apply idempotency and `:213-234` pins the legacy UUID tiebreak.
- **`agent_messages.tokens_used` nullable**
  (`delegation_schema.sql:34-36`): `ALTER COLUMN … DROP NOT NULL`, applied on
  both the fresh (`connection.py:222-231`) and runtime (`:239-246`) paths;
  idempotent. `NULL = unknown` is the documented contract
  (`orchestrator.py:481`) and readers pass it through (`orchestrator.py:599-613`).
- **`agents.max_iterations` DROP** (`schema.sql:275-283` fresh +
  `soulspec_schema.sql:4-6` runtime): `DROP COLUMN IF EXISTS`, idempotent, applied
  on both paths. The UI surface was removed in `978aab8`.

Ordering/dedup/enumeration for legacy rows is preserved (NULLS LAST + UUID
fallback); the only ordering residual is the batch>page cursor case (P3-3) and
the compaction keyset (P3-2), both scoped to *new* single-transaction batches,
not legacy rows.

---

## New test-file architecture

Sampled `test_held_55_history_order.py`, `test_r5_018_audit_redaction.py`,
`test_r5_011_013_provider_transport.py`, `test_sse_stream_adversarial.py`,
`test_cli_turn_e2e.py`, `test_r5_003/004`, `test_r5_019_skill_atomicity.py`,
`test_permission_risk_policy.py`, `test_r4_105_*`.

- They reach real seams: real Postgres via `isolated_schema`, the real ASGI route
  + `TurnEngine`, real subprocess CLI, real flock, real registry.
- Mocks sit at boundaries (httpx `MockTransport`, provider factory, file-module
  `_BASE_DIR` for hermetic temp dirs) — consistent with the fix plan's rule.
- Assertions are behavioral (ordering, redaction outcomes, round-trips,
  idempotency), not implementation mirrors, with the two exceptions in Q-1.
- The R5-S3 delete-only verdict (`backfill_embeddings`, no production caller) is a
  clean dead-surface removal, corroborated by the caller census in its proof.

---

## Architectural debts accepted (documented residuals — do not re-raise)

1. **Test-only seams are kept and justified.** `machina/core/session.py:161`
   (`update_state`), `gateway/server.py:261` (`turn_running`),
   `db/connection.py:249` (`initialize_growth_schema`), `core/provider/__init__.py:27`
   (`_cache_clear`), `tools/base.py:286`, `metrics.py:216/222`,
   `memory/redaction.py:48/410/480`, `memory/store.py:384/436`,
   `memory/forgetting.py:58/73`, `memory/persona.py:369`, `memory/identity.py:334`,
   `memory/user_profile.py:138/359`, `memory/scorer.py:62`, `repomap/evidence.py:68`,
   `mcp_client.py:545`, `research/workflow_trial.py:103`. Each carries a
   grep-backed "Kept (R4-106)" justification (R4-106 / 150c3fc).
2. **Vendor-compliance deletion** of the Claude/Gemini subscription transports and
   auth stores (`core/provider/{claude,gemini}.py`,
   `security/{claude,gemini}_auth.py`) — owner-directed, with the ratchet and
   `RESERVED_NAMES` updated in the same wave.
3. **Two redactors** (`memory/redaction.py` full pattern table vs
   `core/provider/audit.py::_sanitize_value` partial) — pre-existing; this round
   composed them rather than merging. Tracked here (P2-1) as debt, not a new
   regression.
4. **Consolidation cursor remains `(created_at, id)`** and the degraded
   no-advisory-lock path stays in-process single-flight only — explicitly deferred
   to HELD-F1 (`R5-F1_consolidation_cursor.md` §5; `HELD-55_history_order.md`
   residual note).
5. **Compaction keyset left on `(created_at, id)` by design** — HELD-55 residual
   ("compaction consumes a set, not a replay").
6. **R5-S3 backfill method deleted** as dead code (no production caller), not
   "fixed".
7. **Observability has both `tracing.py` and `traces.py`** — two tracing back-ends
   by design (optional OTel vs in-process run tree); naming is ambiguous but the
   split is intentional.
