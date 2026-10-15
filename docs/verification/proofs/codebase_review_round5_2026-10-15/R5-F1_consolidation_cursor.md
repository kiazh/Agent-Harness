# R5-F1 — Consolidation cursor lock scope

**Lane:** R5-F1 (Fix plan Round 5, "Additional concerns" #1 / HELD-F1)
**Finding:** follow-up 1 — the consolidation watermark (`mem_consolidated_up_to`)
is read before `memory/consolidator.py` takes its cross-process advisory lock
and persisted after that lock releases, so cross-process workers can use a stale
cursor and publish out-of-order checkpoints.
**Verdict:** **REPRODUCED then FIXED.** The stale-cursor interleave is real and
deterministic; the durable watermark regressed from `c6` back to `c4`.
**Files changed:** `machina/core/agent.py` (consolidation-cursor region only),
`machina/memory/consolidator.py`, `tests/test_r5_f1_consolidation_cursor.py` (new).
**No commit** (coordinator commits).

---

## 1. Reproduction (RED, before the fix)

The failure needs three workers on one session and a bounded page size:

- page size `MAX_CHUNKS_PER_CONSOLIDATION = 2`;
- chunks `c1..c6` with ascending `created_at`; prime `c1,c2` → durable cursor `c2`;
- **stale worker**: reads cursor `c2`, parks *after the read* and *before* the
  advisory lock (controlled `asyncio.Event`);
- **healthy worker A**: reads `c2`, extracts `c3,c4`, commits `c4`;
- **healthy worker B**: reads `c4`, extracts `c5,c6`, commits `c6`;
- release the stale worker: it extracts the *old* page `c3,c4` and republishes
  `c4` — the watermark moves **backwards** `c6 → c4`.

`get_chunks_since` is ascending+limited, so a stale lower bound yields an older
page whose max checkpoint is below what a newer worker already committed.

Repro script + raw logs (real PostgreSQL, private `isolated_schema`, real
advisory lock; only the LLM completion is faked):

- `logs/R5-F1_repro_2026-10-15.py`
- `logs/R5-F1_repro_red_2026-10-15.log` — `final cursor = c4` (expected `c6`):
  `RED REPRODUCED: stale cursor regressed the durable checkpoint`
- `logs/R5-F1_test_red_2026-10-15.log` — the new E2E test fails on the pre-fix
  source with `AssertionError: stale cursor regressed the watermark` (confirmed by
  `git stash` of only the two source files).

## 2. Fix

Both the authoritative cursor read and the checkpoint commit now run **inside the
consolidator's advisory lock**, so read+extract+commit is one serialized
operation per session.

`machina/memory/consolidator.py`:

- `consolidate_session(..., *, cursor_reader=None, checkpoint_writer=None)`.
  When provided, `cursor_reader()` is evaluated inside `_consolidate_session_impl`
  (i.e. under `_advisory_lock`, overriding `since`) and `_commit_checkpoint()`
  invokes `checkpoint_writer()` **before the lock scope exits** — including on the
  no-database and lock-unavailable fallback paths.
- `_commit_checkpoint()` no-ops without a writer or a checkpoint, so existing
  callers that pass `since` and persist themselves are unchanged.

`machina/core/agent.py` (consolidation-cursor region only, ~1417–1540):

- `_consolidate_memories` routes the built-in `MemoryConsolidator` through the
  new serialized contract: it passes `cursor_reader` (a fresh, lock-scoped
  cursor read) and `checkpoint_writer` (the watermark advance). The agent no
  longer reads the cursor before the lock or persists after it.
- New helpers `_parse_consolidation_cursor`, `_read_consolidation_cursor`
  (prefers `session_manager.get_fresh`, AH-AUDIT-019, so a worker's 60 s cached
  session snapshot cannot pin a stale cursor) and `_persist_consolidation_cursor`.
- Duck-typed consolidators (test doubles / embedders) that do not implement the
  serialized contract keep the previous caller-managed cursor behavior verbatim
  (its diagnostics — cursor parse failure, newest-read failure — are preserved).

Why this is correct: every worker for a session serializes on
`pg_advisory_xact_lock(hashtextextended("memory_consolidate:<session>", 0))`.
Worker B therefore reads the cursor **after** worker A has committed and released
the lock, so it can never re-process an older page; and the commit is durable
before the lock is released (the writer is awaited inside the lock scope).

## 3. Verification (GREEN, after the fix)

New test file `tests/test_r5_f1_consolidation_cursor.py`:

- `test_stale_worker_cannot_regress_durable_cursor` — the adversarial interleave
  above, repeated N=5 over real PostgreSQL/`isolated_schema`; asserts the watermark
  is monotonic (`c6` stays `c6`) and that the stale worker genuinely observed the
  pre-advance cursor. **Passes.**
- `test_cursor_reader_overrides_since_and_commits_checkpoint` — contract test:
  a bogus `since` is ignored in favour of `cursor_reader`, and the resulting
  checkpoint is committed via `checkpoint_writer`. **Passes.**

Logs:

- `logs/R5-F1_test_green_2026-10-15.log` — `2 passed`
- `logs/R5-F1_green_2026-10-15.log` — repro script, `NOT REPRODUCED: durable checkpoint stayed monotonic`

Regression sweep (all green, real test DB):

| Suite | Result |
|---|---|
| `test_r5_f1_consolidation_cursor.py` | 2 passed |
| `test_bug9_bug15_fixes.py` | passed |
| `test_growth_status.py` | passed |
| `test_memory_bug_fixes.py` | passed |
| `test_fm5_consistency.py` | passed |
| `test_agent_factory_memory.py` | passed |
| `test_audit_remediation.py`, `test_memory.py`, `test_integration_permissions.py` | 147 passed, 1 skipped |
| `test_chaos_extended.py` (consolidator chaos/dedup) | 7 passed |
| agent/context/session/memory sweep (16 files) | 251 passed |

`ruff check` and `ruff format --check` clean on all three files. The advisory
lock still uses a dedicated (non-pool) connection, so the R10 pool-independence
tests (`test_memory_bug_fixes.py::TestConsolidationPoolIndependence`) stay green.

## 4. Behavior / API notes

- `MemoryConsolidator.consolidate_session` gains two **keyword-only, optional**
  parameters (`cursor_reader`, `checkpoint_writer`). No existing call site or
  return value changes.
- For the built-in consolidator the agent's old "cursor unchanged → skip"
  pre-check is folded into `_consolidate_session_impl` (its `since`-filtered
  `get_chunks_since` already early-returns before extraction), so an unchanged
  cursor still performs no LLM call and still leaves the watermark untouched.
- The degraded path (no DSN / no dedicated lock connection) now also commits via
  the writer, but remains un-serialized — the same documented degradation as
  before, never a new failure mode.

## 5. Residual limitation

If the database cannot provide the dedicated advisory-lock connection, workers
fall back to the in-process single-flight and are no longer cross-process
serialized; a stale cursor could then still regress the watermark. This is the
pre-existing, explicitly documented degradation for an unavailable database
(`_advisory_lock` yields `False`), not introduced here. A monotonic
compare-and-set persist would additionally cover that degraded path, but it
requires a conditional write in `session.py` (outside this lane's ownership) and
is unnecessary while the database is reachable.
