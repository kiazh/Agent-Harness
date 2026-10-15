# M1 / H08 — stored memory vectors have no embedding-space identity

- **Lane:** M1 (holistic review fix wave, 2026-10-15).
- **Finding:** `docs/verification/CODEBASE_REVIEW_HOLISTIC_2026-10-15.md` §H08
  (P2): memory rows store vectors with no provider/model/embedding-space
  version (`db/schema.sql:188`, `store.py:292`, `memory/embeddings.py:20`).
  Queries used the currently configured embedder against **all** stored
  vectors; switching models with the same dimensions left old vectors
  accepted by SQL but semantically incompatible → bad similarity ranking and
  misfiring embedding-based duplicate decisions.
- **Verdict:** **REPRODUCED → FIXED (RED→GREEN).**
- **Base commit:** `3ef6d5b` (HEAD at lane start; other lanes edit other
  files in the same worktree — this lane made **no commit**).
- **Sole-writer scope respected:** only `machina/memory/store.py`,
  `machina/memory/embeddings.py`, `machina/db/schema.sql`, and this lane's
  test file `tests/test_memory_embedding_space.py` are modified.
  `machina/memory/retriever.py`, `machina/tools/memory.py`,
  `machina/api/routes/memory.py`, `machina/gateway/features/memory.py`,
  `machina/db/connection.py`, and `machina/memory/approval.py` were **read but
  not changed** (see *Follow-ups* for the exact deltas they need).

## Claim under test

`MemoryStore.add()` accepted an `embedding` and persisted it; nothing recorded
which embedder produced it. `MemoryStore.search_by_embedding()` built its SQL
from `embedding IS NOT NULL` (plus optional agent/category/quarantine filters)
and ranked the query vector against every stored vector. Same-dimension model
switches therefore mixed two incompatible embedding spaces, and the
consolidator's 0.85-cosine dedup used those mixed results.

## Reproduction (RED, before the fix)

`tests/test_memory_embedding_space.py` was committed first and run against the
pre-fix tree (source files stashed via `git stash push -- <3 files>`, exact
final test file):

| Evidence | Result |
|---|---|
| `M1_red_pytest_final.log` | **9 failed** — every pin |

Behavioral core (`test_model_switch_excludes_foreign_space_vectors`, real
isolated PostgreSQL schema, no store/retrieval mocks):

```
store.add(model-a, embedding=e0)      # row A
store.search_by_embedding(e0)          -> [A]                      (passes)
# switch the shared embedder to model-b (same 1536 width)
store.add(model-b, embedding=e0)      # row B
store.search_by_embedding(e0)          -> [A, B]  ← BUG
FAILED ... AssertionError: vectors from the previous embedding space
must not be ranked against queries from the new one
  Left contains one more item: UUID('63b3fb25-…')   # row A, foreign space
```

The legacy-fallback test shows the same mixing under the second space
(`test_legacy_untagged_rows_stay_retrievable`):

```
FAILED ... assert {legacy, A} == {legacy}
  Extra items in the left set: UUID('4510ee4a-…')   # tagged with the old space
```

The migration test proves the column did not exist and was never added:
`assert None == 'text'` (`test_existing_database_gains_embedding_space_on_first_use`),
and the new API surface was absent:
`ImportError: cannot import name 'embedding_space_id'`, `TypeError:
MemoryStore.add() got an unexpected keyword argument 'embedding_space'`.

## Root cause

The persisted row carried the vector but not its identity, and the read path
had no notion of compatibility — dimensions were the only (implicit) contract.
`get_shared_embedder()` rebuilds on config change, but that only affects new
queries, never the already-stored vectors.

## Fix (GREEN)

### Persisted-format change (called out explicitly)

`memories` gains an **additive** `embedding_space TEXT` column and a
supporting index `idx_memories_embedding_space`.

- **Fresh installs:** `schema.sql` defines the column in `CREATE TABLE` plus
  the idempotent `ALTER TABLE memories ADD COLUMN IF NOT EXISTS …` /
  `CREATE INDEX IF NOT EXISTS …` pair (the pattern already used for
  `quarantined`).
- **Initialized databases:** `schema.sql` never re-runs there, and
  `machina/db/connection.py` (`initialize_runtime_schema`) is outside this
  lane's ownership — so `MemoryStore` applies the **same idempotent DDL**
  lazily before the first embedding-backed write/read
  (`_MEMORY_EMBEDDING_SPACE_MIGRATION`, executed at most once per instance
  over the base pool handle; once per caller transaction on the
  `connection=` path, where it is not memoized because that transaction can
  roll back). See *Follow-ups* for the exact `connection.py` delta.
- **No backfill / no rewrite.** Existing rows keep `embedding_space = NULL`.
  Rows are only tagged from the moment the migration is applied; the column
  is nullable with no default, so no data is touched and the DDL is additive
  (one cheap btree over an all-NULL column for existing tables).
- **DDL failure degrades, never fails memory ops:** if `ALTER TABLE` cannot
  run (privileges/locks), the store logs + reports the boundary and keeps the
  exact pre-H08 shapes (untagged `INSERT`, unfiltered dense query). Pinned by
  `test_migration_failure_degrades_to_pre_h08_shapes`.

### Identity — `machina/memory/embeddings.py`

Single authoritative implementation: `embedding_space_id(embedder=None)`
returns `"<kind>:<model>:<stored_dimensions>"` plus `"@<base_url>"` for API
backends (kind derived from the embedder class: `OnnxEmbedder` → `onnx`, …).
It reuses the existing shared-embedder resolution, never loads weights, never
raises, and returns `None` in keyword-only operation (no tag, no filter).

Examples: `onnx:BAAI/bge-small-en-v1.5:1536`,
`openai:text-embedding-3-small:1536@https://api.openai.com/v1`.
Credential rotation does **not** change the space (same model, same vectors);
a backend/model/width/endpoint change does.

### Write path — `MemoryStore.add()`

New optional `embedding_space` parameter (explicit override for callers that
supply their own vectors). When an embedding is stored and no override is
given, the shared embedder's current space id is persisted in the same
`INSERT` (`$11`). This covers the production producers that pass vectors:
consolidator candidate embeddings and approval-gate completions.

### Read path — `MemoryStore.search_by_embedding()`

SQL-level compatibility filter:

```sql
AND (embedding_space = $N OR embedding_space IS NULL)
```

- `$N` is the query's space: explicit `embedding_space` argument, else the
  shared embedder's current space.
- **Documented legacy fallback:** rows with `embedding_space IS NULL` (all
  rows written before the migration) stay retrievable and are treated as
  compatible, so existing memories keep working without a backfill. Their
  residual risk is stated below; the upgrade path is re-embedding.
- When no space is resolvable (no embedder configured) the pre-H08
  unfiltered query is kept.
- The new `embedding_space` parameter keeps the change inside `store.py`;
  `retriever.py`/`tools/memory.py`/API/gateway call the method unchanged and
  automatically get the shared embedder's space.

## Verification (GREEN)

| Evidence | Result |
|---|---|
| `M1_green_pytest.log` | `9 passed` — the H08 pin file (`test_memory_embedding_space.py`) |
| `M1_green_focused.log` | `330 passed` — memory store/approval/shared-bus, embedding storage, embedder-local, fail-open, retriever filters (fm4/fm5, recall), persona memory, gateway memory, agent-factory memory, RL memory, **runtime migrations + schema conformance** (SQL-shape/schema compilation) |
| `M1_green_adjacent.log` | `81 passed` — gateway memory feature + chaos-extended store paths |
| `M1_red_pytest_final.log` | `9 failed` on the pre-fix tree (exact final test file) |
| `M1_ruff_check.log` / `M1_ruff_format_check.log` | `All checks passed!` / `3 files already formatted` |
| `M1_deferred_census_delta.log` | architecture ratchet attribution (below) |

Fresh-schema DDL is validated twice: `test_schema_conformance` compiles all
static production SQL against an initialized schema (store INSERT/SELECT SQL
included), and the new migration test drops the column and index to simulate
an existing pre-H08 database, then proves the first embedding-backed write
restores them (`information_schema` → `text`), tags the new row, keeps the
legacy row `NULL`, and serves a repeat run from a second store instance.

## Files changed

- `machina/db/schema.sql` — additive `embedding_space` column (CREATE TABLE +
  `ADD COLUMN IF NOT EXISTS`) and `idx_memories_embedding_space`.
- `machina/memory/embeddings.py` — `embedding_space_id()` (single
  authoritative identity) + `_space_kind()`.
- `machina/memory/store.py` — `add(..., embedding_space=...)` tags writes;
  `search_by_embedding(..., embedding_space=...)` filters compatible rows in
  SQL; `_ensure_embedding_space()` applies the same idempotent DDL on
  already-initialized databases (durable/transactional variants); graceful
  degradation on DDL failure.
- `tests/test_memory_embedding_space.py` — new (9 tests: behavioral switch,
  legacy fallback, migration/fresh/legacy/caller-transaction/idempotency,
  SQL shape, degradation, identity unit pins).
- Proofs in this directory: this report + `logs/M1_*.log`.

## Callouts (format change) and follow-ups

1. **Persisted-format change:** `memories.embedding_space TEXT` + one index.
   Additive only; no rewrite/backfill; legacy rows stay `NULL` and usable via
   the fallback. New writes are tagged; reads filter per space.
2. **`machina/db/connection.py` (other lane's file — precise delta):** add
   ```python
   _MEMORY_EMBEDDING_SPACE_MIGRATION = """
   ALTER TABLE memories ADD COLUMN IF NOT EXISTS embedding_space TEXT;
   CREATE INDEX IF NOT EXISTS idx_memories_embedding_space ON memories(embedding_space);
   """
   ```
   and in `initialize_runtime_schema()` (after the fragment files, beside
   `_CONTEXT_EVENT_ORDER_MIGRATION`): `await conn.execute(_MEMORY_EMBEDDING_SPACE_MIGRATION)`.
   The store's lazy ensure is the *only* migration path that exists for
   existing databases today; once the boot-time entry lands, it becomes a
   no-op defense-in-depth (keep both, both are idempotent).
3. **`pending_memories` / `approval.py` (not this lane's files):** a vector
   submitted under space A and approved after a config switch to B is tagged
   `B` at approval time (the pending row carries no space). Follow-up:
   add `embedding_space` to `pending_memories` (schema.sql + runtime
   migration), persist `embedding_space_id()` in `gate.submit()`, and pass
   `embedding_space=row["embedding_space"]` through `approve()`/`approve_all()`
   into `memory_store.add(...)` — the parameter already exists.
4. **No re-embed/backfill is wired** (deliberate): re-embedding is the
   documented upgrade path for `NULL` legacy rows after a model switch
   (`SELECT … WHERE embedding IS NOT NULL AND embedding_space IS NULL`,
   re-embed content with `embed_texts`, update vector + space in batches).
   It needs a maintenance entry point outside this lane's files; until it
   runs, **pre-migration rows remain mixed into dense comparisons** (the
   exact H08 hazard, now confined to untagged rows and visible as NULL).
5. **Architecture ratchet:** `test_deferred_import_count_ratchet` is at
   `718 > 717` on this shared worktree. `M1_deferred_census_delta.log` shows
   the +1 is `machina/services.py` (H20 lane); this lane's files are ±0
   (store.py 0→0, embeddings.py 2→2). `test_god_file_ceilings_only_fall
   [core/provider/copilot.py-431]` and `test_provider_package_total_only_falls`
   also fail from other lanes' in-flight edits (`copilot.py` 443 > 431).
   Not fixed here (not this lane's files).
6. **Known residual (legacy rows):** after a model switch, untagged legacy
   rows are still compared until re-embedded. This is the required
   "deliberate documented fallback" for old rows; the alternative (excluding
   all untagged rows) would make every pre-upgrade memory invisible without a
   backfill. Queries from a *tagged* space never compare against *other
   spaces*; the migration test pins both halves.
7. **No commit** (coordinator commits). `machina/gateway/features/memory.py`
   and `machina/tools/memory.py` needed no change: they call
   `memory_store.add` without embeddings, and keyword retrieval is
   unaffected.
