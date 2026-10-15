# M1b / H08 follow-ups — boot migration + pending approval tagging

- **Lane:** M1b (holistic review fix wave, 2026-10-15) — closes the two
  follow-ups specified in
  `docs/verification/proofs/holistic_fixes_2026-10-15/M1_embedding_space.md`
  §"Callouts (format change) and follow-ups" items 2 and 3.
- **Findings:** (a) fresh/standard init never wired the H08 additive migration
  into `initialize_runtime_schema`, so an existing database that skipped the
  store's lazy path got no `memories.embedding_space` until first embedding
  use; (b) a vector submitted under embedding space A and approved after a
  config switch to B was tagged **B** — `pending_memories` carried no
  submission space.
- **Verdict:** **REPRODUCED → FIXED (RED→GREEN)** for both.
- **Base commit:** `3ef6d5b` (HEAD at lane start; other lanes edit other
  files in the same worktree — this lane made **no commit**).
- **Sole-writer scope respected:** only `machina/db/connection.py`,
  `machina/memory/approval.py`, `machina/db/schema.sql`,
  `tests/test_memory_embedding_space.py`, and
  `tests/test_runtime_migrations.py` are modified. `machina/memory/store.py`
  was **not** changed by this lane (its lazy ensure remains the fallback);
  `machina/services.py`, `machina/core/provider/copilot.py`,
  `machina/permissions/policy.py`, and `machina/gateway/**` were not touched.

## Claim under test

1. `Database.initialize_runtime_schema()` — the standard boot path for
   already-initialized databases — must apply the same idempotent DDL that
   `MemoryStore` applies lazily, so the column/index exist before any memory
   operation (and regardless of whether one ever runs).
2. A pending memory's vector must keep the identity of the space that
   produced it. `gate.submit()` must persist the submission space on
   `pending_memories`, and `approve()`/`approve_all()` must pass it through to
   `memory_store.add(..., embedding_space=...)` so a completion after a
   model/config switch is tagged with the **original** space, not the
   currently configured one.

## Reproduction (RED, before the fix)

`docs/verification/proofs/holistic_fixes_2026-10-15/M1b_repro_pending_space.py`
(fake embedder `model-a` → submit → switch to `model-b`, same 1536 width →
approve; isolated PostgreSQL schema, no store/approval mocks):

| Evidence | Result |
|---|---|
| `M1b_red_repro.log` | `pending row space : <column unavailable: UndefinedColumnError>` / `completed memory space : _fake:model-b:1536` → **RESULT: BUG — completed row was tagged with the post-switch space** (exit 1) |
| `M1b_red_pytest.log` | **7 failed**, 10 passed — the 6 new pins + the new runtime-migration test fail on the pre-fix tree |

Pre-fix failures: the pending INSERT/SELECT never carried a space, the table
had no column (`SELECT embedding_space FROM pending_memories` →
`UndefinedColumnError`, and the migration test observed
`assert None == 'text'` after `initialize_runtime_schema()`).

## Root cause

`pending_memories` persisted the vector but not its producing space, and the
approval completion resolved the space at completion time via the shared
embedder — so a switch between submission and approval silently re-tagged the
vector. Separately, the H08 runtime migration existed only inside
`MemoryStore`, so databases whose operators never trigger an embedding-backed
store operation were left un-migrated at boot.

## Fix (GREEN)

### Follow-up 1 — `machina/db/connection.py` (verbatim M1 delta)

```python
_MEMORY_EMBEDDING_SPACE_MIGRATION = """
ALTER TABLE memories ADD COLUMN IF NOT EXISTS embedding_space TEXT;
CREATE INDEX IF NOT EXISTS idx_memories_embedding_space ON memories(embedding_space);
"""
```

executed in `initialize_runtime_schema()` beside `_CONTEXT_EVENT_ORDER_MIGRATION`
(after the fragment files). Comment style matches the existing HELD-55
migration; the DDL text is the single authoritative form already used by
`schema.sql` and `MemoryStore._MEMORY_EMBEDDING_SPACE_MIGRATION`. The store's
lazy ensure is unchanged and remains a no-op defense-in-depth fallback.

### Follow-up 2 — pending approval tagging

- **Persisted-format change (called out explicitly):** `pending_memories`
  gains an **additive** `embedding_space TEXT` column.
  - **Fresh installs:** `schema.sql` defines the column in `CREATE TABLE`
    plus the idempotent `ALTER TABLE pending_memories ADD COLUMN IF NOT
    EXISTS …` pair (same pattern as `memories.embedding_space`).
  - **Existing installs:** `MemoryApprovalGate` applies the same idempotent
    DDL lazily before the first embedding-backed submit/approve
    (`_PENDING_EMBEDDING_SPACE_MIGRATION`, memoized per instance, executed
    over the base pool handle; degrade-not-fail). This mirrors
    `MemoryStore._ensure_embedding_space`; it is intentionally **not** wired
    into `connection.py` — M1's item 2 delta covers the memories table only,
    and the gate is the sole reader/writer of this table.
  - **No backfill.** Pre-existing pending rows keep `embedding_space = NULL`;
    their completion follows the configured embedder (the pre-fix behavior,
    now confined to untagged rows and visible as NULL).
- **Write path — `gate.submit()`:** resolves `embedding_space_id()` for the
  submitted vector and persists it in the same INSERT (`embedding_space`
  precedes `embedding` so the embedding stays the trailing bound parameter of
  the established INSERT shape). No vector, no resolvable embedder, or
  unavailable DDL → exact pre-migration INSERT (no tag).
- **Completion path — `approve()` / `approve_all()`:** the additive migration
  is resolved first; the locked SELECT then includes `embedding_space` and
  the row's value is forwarded as `embedding_space=row["embedding_space"]`
  into `memory_store.add(...)` (the parameter already existed). If the DDL is
  unavailable, both fall back to the exact pre-migration SELECT shape and no
  pass-through (degradation, never failure).
- **NULL semantics:** legacy/pre-migration pending rows (`NULL`) and degraded
  databases complete with the configured space — documented fallback, pinned
  by the degradation test.

## Verification (GREEN)

| Evidence | Result |
|---|---|
| `M1b_green_repro.log` | `pending row space : _fake:model-a:1536` / `completed memory space : _fake:model-a:1536` → **RESULT: OK — completed row keeps the submission space** (exit 0) |
| `M1b_green_pytest.log` | `17 passed` — the H08 pin file (9 M1 tests + 6 new) + runtime migrations (2) |
| `M1b_green_focused.log` | `374 passed` — memory store/approval/bug-fixes/RL, embedding storage/embedder-local/fail-open, approval batch, shared bus, persona memory, agent-factory memory, fm4/fm5 filters + ranking + redaction, recall, retrieval eval, **runtime migrations + schema conformance** |
| `M1b_green_adjacent.log` | `87 passed` — gateway memory features/usage, fm3 approval store, approvals ownership/deadline/wait, HTTP approval resume, learning approval recovery |
| `M1b_green_runtime_boot.log` | `82 passed` — held-55 history order, soulspec runtime, delegation boundaries, usage cleanup, boot-config failure path, phase6 (all `initialize_runtime_schema` callers) |
| `M1b_green_gateway_images.log` | `32 passed` — gateway images (heaviest `initialize_runtime_schema` user) |
| `M1b_green_adjacent2.log` | `35 passed` — recent review-wave gateway-obs/memory, persona review batch, personality growth, fm3 approval store |
| `M1b_red_repro.log` / `M1b_red_pytest.log` | red evidence above |
| `M1b_ruff_check.log` / `M1b_ruff_format_check.log` | `All checks passed!` / `5 files already formatted` |
| `M1b_ratchet_status.log` / `M1b_deferred_census_delta.log` | architecture ratchet attribution (below) |

New pins (6 in `tests/test_memory_embedding_space.py`, 1 in
`tests/test_runtime_migrations.py`):

- `test_submit_persists_embedding_space` — INSERT contains the space; the
  ALTER runs; embedding remains the trailing parameter (SQL shape).
- `test_approve_passes_submission_space_through` — SELECT fetches the space
  and `memory_store.add` receives it (SQL shape + pass-through).
- `test_pending_submission_keeps_original_space_across_model_switch` —
  behavioral core: submit A → switch B → approve → memory row tagged **A**
  (real isolated schema; red pre-fix observed the `model-b` tag).
- `test_approve_all_keeps_original_space_across_model_switch` — batch
  variant (two rows, one `approve_all`).
- `test_existing_database_gains_pending_embedding_space_on_first_use` —
  drops the column, first gate use restores it (`information_schema` →
  `text`), tags the pending row, and a second gate instance still completes
  under the original space after another switch.
- `test_pending_migration_failure_degrades_to_pre_migration_shapes` — DDL
  failure keeps the pre-migration INSERT/SELECT shapes and no pass-through.
- `test_runtime_upgrade_adds_memory_embedding_space` (runtime migrations) —
  drops the memories column/index, `initialize_runtime_schema()` twice
  restores both (idempotent).

## Files changed

- `machina/db/connection.py` — `_MEMORY_EMBEDDING_SPACE_MIGRATION` constant +
  one `await conn.execute(...)` in `initialize_runtime_schema()`.
- `machina/db/schema.sql` — `pending_memories.embedding_space` in
  `CREATE TABLE` + the idempotent `ALTER TABLE` pair (on top of M1's
  uncommitted `memories.embedding_space` section).
- `machina/memory/approval.py` — `_PENDING_EMBEDDING_SPACE_MIGRATION`,
  `MemoryApprovalGate._ensure_pending_embedding_space` (memoized,
  degrade-not-fail), `submit()` persists the space, `approve()` /
  `_approve_all_locked()` select and pass it through.
- `tests/test_memory_embedding_space.py` — 6 new pins (docstring extended).
- `tests/test_runtime_migrations.py` — 1 new boot-migration pin.
- Proofs in this directory: this report, `M1b_repro_pending_space.py`,
  `M1b_deferred_census_delta.py`, `logs/M1b_*.log`.

## Callouts / follow-ups

1. **Persisted-format addition:** `pending_memories.embedding_space TEXT`
   (nullable, no default, no index — nothing filters on it; the tag is
   read/written per row). Additive only; no rewrite/backfill. Together with
   M1's `memories.embedding_space` this is the complete H08 format surface.
2. **Migration-path split (deliberate):** `memories.embedding_space` now has
   a boot-time entry in `initialize_runtime_schema()` (item 2's verbatim
   delta) plus the store's lazy fallback; `pending_memories.embedding_space`
   is lazy-only in `MemoryApprovalGate` (item 3's "same pattern" — the owning
   module, memoized, degrade-not-fail). Both are idempotent and safe to race
   across processes.
3. **Known residual:** legacy pending rows (`embedding_space IS NULL`) and
   degraded databases still complete under the configured space at approval
   time. This is the documented pre-migration fallback; rows submitted after
   the upgrade always keep their original space.
4. **Schema-conformance scan delta:** `test_schema_conformance`'s static SQL
   discovery went 272 → 268 queries because four approval statements
   (submit INSERT, approve/approve_all SELECTs) are now built from dynamic
   fragments — the same tradeoff M1's store INSERT made. The floor
   (`count >= 100`) still holds and the suite passes; the exact tagged and
   degraded SQL shapes are instead pinned by the new mocked tests
   (`test_submit_persists_embedding_space`,
   `test_approve_passes_submission_space_through`,
   `test_pending_migration_failure_degrades_to_pre_migration_shapes`) and by
   the real-database pins; the remaining UPDATE literals stay scanned.
5. **Architecture ratchet:** deferred-import census is `720 > 717` on this
   shared worktree; `M1b_deferred_census_delta.log` shows the net +2 since
   M1's log is `machina/core/scheduler.py` +3 / `machina/core/turn_engine.py`
   -1 (other lanes) and `machina/services.py` +1 (H20 lane). **This lane's
   files are ±0.** `test_god_file_ceilings_only_fall[core/agent.py-2267]`,
   `[core/provider/copilot.py-431]`, and `test_provider_package_total_only_falls`
   also fail from other lanes' in-flight edits (provider package 5015 > 5003).
   Not fixed here (not this lane's files); `machina/memory/approval.py` is not
   in the god-file ceiling table.
6. **Transient non-repros (environmental, not kept as failures):** one
   `test_research_integration` deadlock and one
   `test_personality_growth::test_ui_checks_with_real_gateway_restart`
   assertion failed once while other lanes were active on the shared test
   database; both passed on isolated re-runs (and the full adjacent batch
   re-ran green, `M1b_green_adjacent2.log`).
7. **No commit** (coordinator commits). No re-embed/backfill entry point is
   wired (follow-up 4 of the M1 report remains open, outside this lane).
