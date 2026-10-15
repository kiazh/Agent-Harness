# HELD-55 — Batched conversation history loses event order (finding 5)

- **Lane:** HELD-55, per `docs/verification/FIX_PLAN_ROUND5_2026-10-15.md` (launch
  after R4-025 + R4-096).
- **Finding:** CODEBASE_REVIEW_CURRENT_2026-10-14 §5 — a batched chunk insert assigns
  random client-side UUIDs and one transaction `created_at`; retrieval tie-breaks by
  UUID, so a batch's user message / tool results / final answer can replay out of
  order (resumed prompts, exports, consolidation consume that order).
- **Verdict:** **REPRODUCED → FIXED.** Real multi-chunk batch on a real isolated
  PostgreSQL schema replays scrambled pre-fix and in order post-fix.
- **Base commit:** `141675e` (HEAD moved mid-lane as the coordinator committed other
  lanes; the final worktree sits on `141675e` + this lane's uncommitted changes).
  **No commit made by this lane.**
- **Sole-writer scope respected:** only `machina/core/context.py`,
  `machina/db/schema.sql`, `machina/db/connection.py`, and the ordering tests are
  modified. `machina/core/agent.py` was **read but not changed** (see *Agent flush
  order* below); consolidation and flush-error regions were not touched.

## Claim under test

`ContextManager.add_chunks_batch` performed a single `INSERT ... SELECT FROM UNNEST`
and received `created_at` from `now()`, which is constant for every row of one
statement/transaction. Every retrieval reader ordered by `(created_at, id)` — and
`id` is a client-side `uuid.uuid4()` — so rows inside one batch had no reliable
order. Multi-tool turns (parallel/sequence tool calls persisted as one batch) and any
other multi-chunk flush could therefore replay their events in arbitrary order.

## Reproduction (RED, before the fix)

`HELD-55_probe.py` drives the real writer and readers against a private isolated
schema (`tests.support.schema.isolated_schema`, G3 pattern) on `machina_test`. It
inserts one 8-row batch `e0..e7` through `add_chunks_batch` and reads back through
`get_recent_context`, `get_chunks`, `get_transcript_chunks` and `get_chunks_since`.
No writer/reader logic is mocked.

Pre-fix (`HELD-55_probe_red.log`):

```
rows=8 distinct_created_at=1
expected newest-first: ['e7', 'e6', 'e5', 'e4', 'e3', 'e2', 'e1', 'e0']
get_recent_context   : ['e4', 'e5', 'e2', 'e7', 'e6', 'e3', 'e1', 'e0']
get_chunks           : ['e4', 'e5', 'e2', 'e7', 'e6', 'e3', 'e1', 'e0']
expected oldest-first: ['e0', 'e1', 'e2', 'e3', 'e4', 'e5', 'e6', 'e7']
get_chunks_since     : ['e0', 'e1', 'e3', 'e6', 'e7', 'e2', 'e5', 'e4']
raw UUID DESC (proxy): ['e4', 'e5', 'e2', 'e7', 'e6', 'e3', 'e1', 'e0']
VERDICT: SCRAMBLED (get_recent_context)
VERDICT: SCRAMBLED (get_chunks)
VERDICT: SCRAMBLED (get_chunks_since)
```

The probe confirms the premise (`distinct_created_at=1`) and the mechanism: the two
DESC readers return **exactly the raw-UUID descending order**. (The probe also asserts
the premise in code, so it refutes rather than "fixes" if a batch ever stopped sharing
one timestamp.)

The committed regression test was also captured RED by temporarily removing only the
`event_seq` tie-break from the retrieval `ORDER BY` clauses (writer/ordinal left in
place) — `HELD-55_pytest_red_ordering_ignored.log`:

```
FAILED tests/test_held_55_history_order.py::test_batch_replays_in_event_order_across_readers
E  AssertionError: assert ['e7', 'e2', ...6', 'e4', ...] == ['e7', 'e6', ...3', 'e2', ...]
E    At index 1 diff: 'e2' != 'e6'
1 failed, 4 passed
```

## Root cause

`created_at` is not unique within a batch, and the only tie-break was the random
`id`, so no reader could recover the write order that the caller supplied.

## Fix (GREEN)

### Persisted-format change (called out explicitly)

`context_chunks` gains an **additive** `event_seq BIGINT` column backed by a new
sequence `context_chunks_event_seq` (owned by the column, so it drops with it). This
is a persisted-format change: existing rows keep `event_seq = NULL` (no rewrite), new
rows are stamped at write time. A matching index
`idx_context_chunks_session_created_event` supports the new ordering. There is **no
data backfill** — legacy `NULL` rows keep their previous `(created_at, id)` order via
the `NULLS LAST` + `id` fallback, so existing behaviour is preserved.

### Write path — `machina/core/context.py`

- `add_chunks_batch` reserves `len(records)` ordinals in one round-trip
  (`SELECT nextval('context_chunks_event_seq') FROM generate_series(1, $1)`) and
  assigns them **ascending in the caller's list order**, so the i-th chunk always
  receives the i-th ordinal regardless of `nextval` call order. The ordinal travels
  as a new `$10::bigint[]` UNNEST array (the image variant renumbers its arrays to
  `$11/$12/$13`). Ordering is derived from the list the flush already supplies in
  event order.
- `add_chunk` and `replace_chunks_by_ids` need no code change: they omit `event_seq`
  and the column `DEFAULT nextval(...)` stamps them (executemany preserves row order).

### Read path — `machina/core/context.py`

`event_seq` is inserted between `created_at` and the `id` fallback in every reader
that reconstructs conversation order:

| Reader | Consumers | New order |
|---|---|---|
| `get_recent_context` | prompt assembly (`_prepare_context`, orchestrator) | `created_at DESC, event_seq DESC NULLS LAST, id DESC` |
| `get_chunks` | gateway/CLI history, API, newest check | same |
| `get_transcript_chunks` | `session.resume`, transcript export | same (`event_seq` carried through the CTE; archive rows `NULL`) |
| `get_chunks_since` | consolidation extraction | `created_at ASC, event_seq ASC NULLS LAST, id ASC` |

`get_chunks_before` (keyset `(created_at, id)` cursor used only by
`get_all_chunks` → compaction) is intentionally **left unchanged**: compaction
consumes a set of chunks to summarize, not a replay, and changing its keyset cursor
would require a cursor-format migration outside this finding. `get_chunks_since`'s
`WHERE` cursor also stays `(created_at, id)`; its consolidation cursor is the **max**
processed `(created_at, id)`, so adding `event_seq` to the `ORDER BY` cannot skip or
duplicate rows (the cursor is not the last-by-sort row). `HELD-F1` owns consolidation
cursor semantics.

### Runtime migration — `machina/db/schema.sql` + `machina/db/connection.py`

`schema.sql` defines the additive DDL for fresh installs and
`initialize_schema()`. Because `schema.sql` never runs on an existing database,
`initialize_runtime_schema()` now applies the same idempotent DDL
(`CREATE SEQUENCE IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS` / `SET DEFAULT` /
`CREATE INDEX IF NOT EXISTS`) under the existing `machina:runtime_migrations`
advisory lock, after the fragment files.

### Agent flush order — `machina/core/agent.py` (no change)

`pending_chunks` is assembled in true event order: Phase D of
`_execute_tool_calls_stream` iterates `planned` in provider/original tool-call order
and appends each `tool_call` chunk there (out-of-order *completion* only affects the
live stream, not persistence), and `_flush_pending_chunks` passes the list unchanged.
The durable ordinal is therefore assigned over exactly that order at flush. No edit to
`agent.py` was required; its consolidation and flush-error regions were not touched.

## Verification (GREEN)

| Evidence | Result |
|---|---|
| `HELD-55_probe_green.log` | readers return `e7..e0` / `e0..e7`; **VERDICT: IN_ORDER** (raw UUID order still scrambled — proving the ordinal, not luck, restored order) |
| `HELD-55_pytest_focused.log` | `5 passed` — new `tests/test_held_55_history_order.py` (order across readers, monotonic across batches, image-bearing batch, fresh+legacy migration idempotency, legacy UUID fallback) |
| `HELD-55_pytest_red_ordering_ignored.log` | 1 failed / 4 passed with the ordinal ignored in retrieval (committed-test RED) |
| `HELD-55_pytest_adjacent.log` | `127 passed` (runtime migrations, schema conformance, gateway images, transcript export, history condensation, compaction checkpoint, compression, handoff regressions, R5-F1 consolidation cursor, fm4 context) |
| `HELD-55_pytest_sweep.log` | `623 passed` (property-based, memory, API, CLI turn e2e/ownership, phase5/6, skills, architectures, orchestrator tokens, …) |
| `HELD-55_pytest_extra.log` | `155 passed`, `1 failed` — the single failure (`test_context_crud_with_mock_db`) is pre-existing and unrelated: it fails in `delete_chunks`' `async with db.acquire()` against an `AsyncMock` and reproduces with this lane's `context.py` stashed (HEAD-only) |
| `HELD-55_ruff_check.log` / `HELD-55_ruff_format.log` | `All checks passed!` / `4 files already formatted` |

Migration idempotency is proven by `test_runtime_migration_adds_and_defaults_event_seq`:
fresh schema carries the column/sequence/index; a simulated legacy database (column +
sequence dropped, a legacy row seeded) is upgraded twice, the legacy row survives with
`event_seq NULL`, the default is restored, and a subsequent insert draws an ordinal.

## Files changed

- `machina/core/context.py` — batch write stamps durable ordinals (new `$10::bigint[]`
  array; image variant renumbered `$11/$12/$13`); four readers add the `event_seq`
  tie-break; transcript CTE carries it. Additive column, no model/API change.
- `machina/db/schema.sql` — additive `event_seq` sequence/column/default/index.
- `machina/db/connection.py` — `initialize_runtime_schema` applies the additive DDL to
  existing databases.
- `tests/test_held_55_history_order.py` — new (5 tests).
- `tests/test_property_based.py` — `fake_fetch` now models the added ordinal-reservation
  read and the 10th UNNEST array (writer contract is additive).
- `tests/test_integration.py` — `test_context_batch_insert`'s `AsyncMock` models the same
  ordinal-reservation read and expects the two writes (reserve + batch insert).
- Proofs in `docs/verification/proofs/codebase_review_round5_2026-10-15/`:
  `HELD-55_history_order.md`, `HELD-55_probe.py`, and the `HELD-55_*.log` files.

## Notes / residual risk

- **Persisted-format change:** one additive column + one sequence + one index on
  `context_chunks`. Legacy rows are not rewritten; they keep `(created_at, id)` order.
- Each batch flush now costs one extra small `nextval` round-trip; ordinals are
  consumed (gaps allowed) even if the subsequent insert fails, which is normal
  sequence semantics.
- `get_chunks_before` keeps the `(created_at, id)` keyset cursor by design (compaction
  snapshot, not replay). Consolidation cursor internals remain `(created_at, id)` and
  are deferred to HELD-F1.
- No commit (coordinator commits).
