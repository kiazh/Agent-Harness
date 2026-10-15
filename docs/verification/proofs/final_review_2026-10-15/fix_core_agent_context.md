# Fix lane — core agent/context (final review 2026-10-15)

Date: 2026-10-15
Lane: FIX (final review). Sole writer scope: `machina/core/agent.py`,
`machina/core/context.py`, `machina/memory/consolidator.py`,
`machina/core/models.py`, extended by the second addendum to
`machina/core/session.py` (and `serialization.py`, ultimately not needed), plus
the tests pinned by the changed behavior. No commits; worktree only.

Baseline HEAD at lane start: `1e1edbf` / `56f8ad3` (the tree moved under a
concurrent lane; all numbers below are measured on this worktree).

## Findings fixed

### 1. P2 — consolidation cursor skip (HELD-55 F1 / adversarial F2 / core-runtime F1)

`get_chunks_since` ordered by `(created_at, event_seq NULLS LAST, id)` but the
predicate and the checkpoint used only `(created_at, id)`. A same-`created_at`
batch straddling `MAX_CHUNKS_PER_CONSOLIDATION` could be permanently skipped.

Fix (smallest 3-key cursor):

- `context.py::get_chunks_since` — new `since_seq` keyword; the predicate is a
  `CASE` on `since_seq IS NULL`: a legacy cursor keeps the old `(created_at, id)`
  path unchanged, a 3-key cursor compares
  `(COALESCE(created_at, epoch), COALESCE(event_seq, <bigint max>), id)`. The
  `ORDER BY` now uses the same `COALESCE(event_seq, <bigint max>)` so order and
  predicate share one key and `NULLS LAST` is reproduced. `event_seq` is added
  to the `SELECT`.
- `models.py::ContextChunk` — additive `event_seq: int | None = None`.
- `context.py::_row_to_chunk` — copies `event_seq` defensively from the row
  (readers that omit the column fall back to `None`).
- `consolidator.py::_checkpoint_for` — max by the same 3-key
  (`created_at.isoformat()`, `COALESCE(event_seq, _SEQ_NULL_SENTINEL)`, `id`);
  the normalized-content **persisted cursor** gains an optional `seq`.
- `consolidator.py::_consolidate_session_impl` — resolves a 2- or 3-tuple
  `since`; passes `since_seq` only when present (keeps duck-typed readers happy).
- `agent.py` cursor reader/writer — `_parse_consolidation_cursor` returns the
  legacy `(at, id)` 2-tuple when no `seq` is stored and `(at, seq, id)` when it
  is; `_persist_consolidation_cursor` writes `seq` only when the checkpoint
  carries one; the duck-typed branch passes `since_seq` conditionally.

### 2. P1 — F02 cancelling a tool batch loses completed effects

Completions were emitted as tasks finished, but context recording,
`tool_calls_made` bookkeeping and approval retirement ran only in Phase D after
the whole batch, so cancellation skipped them (an approved action could stay
`claimed`; the completed record was absent from `pending_chunks`).

Fix: **finalize completed-but-unrecorded items on cancellation**, preserving the
normal path byte-for-byte (provider order):

- `agent.py::_record_tool_action` — one extracted, idempotent
  (`item['_recorded']`) body for the tool_call context chunk + approval
  retirement + `tool_calls_made` entry; called by Phase D (provider order) and by
  the cancellation finalizer.
- `agent.py::_tool_entry` — extracted entry builder (no duplication).
- `agent.py::_finalize_completed_tools` — on cancellation, records items that
  finished (`elapsed_ms` set, not denied, not yet recorded) in provider order,
  each under `asyncio.shield`.
- Phase C wraps its completion loop with an `except CancelledError: cancelled=True;
  raise` and, in the existing `finally` (after cancelling/joining tasks), calls
  the finalizer. Phase D's loop is wrapped `except CancelledError -> finalize;
  raise`. Provider-message assembly stays in Phase D untouched (order-sensitive).

### 3. P3 — `run()` immediate user-message flush not cancellation-safe (core-runtime F2)

`run()`'s first `await self._flush_pending_chunks(...)` is now wrapped exactly
like `run_stream()`: `except asyncio.CancelledError ->
_flush_pending_cancellation_safe(...); raise`.

### 4. P3 — multi-approval pauses dropped (core-runtime F3)

`models.py::AgentResponse.__post_init__` now derives pauses from the plural
`needs_approvals` (list) when present and truthy, falling back to the singular
`needs_approval` — mirroring `turn_flow.collect_pauses` — deduped by
`request_id`. Both keys are considered; the singular is never dropped.

### 5. P3 — flush docstring/behavior mismatch (code_review #6)

`_flush_pending_chunks` now returns `bool`: `True` only when the batch was
durably written (list cleared); `False` on failure (list retained for the rest
of the turn). `_flush_pending_cancellation_safe` flushes the real list and
clears it only on a confirmed flush; on failure the chunks are retained (and a
warning logged) instead of clearing a batch it never persisted. `CancelledError`
still propagates and a failing store still never fails the turn. Docstrings are
now truthful.

### 6. P2 (second addendum) — event_seq dropped at read and re-derived by compaction/fork

- `context.py::get_chunks_before` — orders by
  `(created_at, COALESCE(event_seq, <bigint min>)) DESC, id DESC` (same
  `NULLS LAST` semantics), with a `before_seq` cursor and a `CASE` legacy
  predicate; `event_seq` added to `SELECT`.
- `context.py::get_all_chunks` — carries `before_seq` from the last row (passes
  it only when non-`None`, so doubles that predate the column still work).
- `context.py::replace_chunks_by_ids` — reads `event_seq` from the locked
  originals inside the transaction (`ordinal_by_id`) and preserves each
  preserved chunk's ordinal exactly; a `-1` sentinel means "draw from
  `context_chunks_event_seq`" for genuinely new chunks while a preserved legacy
  `NULL` stays `NULL`. The compressor (`compression.py`) is unchanged.
- `session.py::fork` — the `INSERT ... SELECT` copies `event_seq` verbatim.

## Refuted / not applicable

- F1's suggested "always pass `since_seq`" would have broken legacy doubles and
  reinterpreted stored 2-key cursors; the conditional legacy path is kept
  instead (no behavior change for pre-ordinal ranges).
- `serialization.row_to_chunk` did **not** need editing: `event_seq` is
  propagated in `context.py::_row_to_chunk`, so the shared converter and every
  non-cursor reader keep their existing contract.

## Persisted-format / API changes (call-outs)

- **Persisted cursor JSON**: `sessions.state.mem_consolidated_up_to` gains an
  optional `seq` (int). Additive and optional; `id`/`at` unchanged; the e2e gate
  check (`id + at`) is unaffected. Legacy cursors (no `seq`) keep the exact old
  `(created_at, id)` predicate.
- **New keyword args** (all default `None`, non-breaking):
  `get_chunks_since(..., since_seq=None)`,
  `get_chunks_before(..., before_seq=None)`.
- **`_flush_pending_chunks`** now returns `bool` (previously `None`); ignored by
  existing callers.
- **`ContextChunk.event_seq`**: additive field, default `None`.

## Architecture ceiling (same commit)

`tests/test_architecture_layers.py` — `core/agent.py` grew `2110 -> 2213` with a
documented delta-table entry added in the comment block immediately above
`GOD_FILE_CEILINGS` (HELD-55 F1 cursor, F02 finalization, run() flush wrap,
flush contract). No new module-level or deferred imports (ratchet unchanged).

## Evidence

- RED (source stashed to HEAD, tests kept):
  - `logs/red_before_fix.txt` — 7 failed / 2 passed:
    `test_cancelled_batch_records_completed_tool`,
    `test_cancelled_batch_retires_completed_approval`,
    `test_flush_retains_on_failure_and_clears_on_success`,
    `test_cancellation_safe_flush_keeps_chunks_on_failure`,
    `test_agent_response_derives_all_plural_pauses`,
    `test_same_created_at_batch_straddling_page_is_not_skipped`,
    `test_compaction_replace_preserves_event_seq`.
  - `logs/red_fork_before_fix.txt` — fork ordinals re-derived
    (`[1001, 1002, 1003, 1004] != [1, 2, 3, 4]`).
- GREEN focused suites (`logs/green_focused_suites.txt`): **448 passed** —
  r5_f1, held_55, timeout contract, tool-execution
  (`test_parallel_tool_execution`, `test_tool_timeouts`), chaos, turns
  (`test_turn_engine`, `test_turns_ownership`,
  `test_turn_completion_lifecycle`, `test_a5_turn_flow`,
  `test_s5_turns_counters`), the new fix tests, plus compression, memory,
  fm5, bug9/15, production-readiness-p1, handoff, core-audit,
  gateway-images, reversible-eviction, transcript-export, audit-remediation
  and property-based.
- `logs/architecture_layers.txt` — `23 passed, 3 failed`; all three failures are
  `core/provider/{audit.py,sse.py}` ceilings and the provider package total,
  i.e. a concurrent lane's uncommitted provider growth — **outside this lane's
  scope**. The `core/agent.py` ceiling and both import ratchets pass.
- `ruff format --check` + `ruff check` clean on every changed file.

## Files changed

Source (in scope):
`machina/core/agent.py`, `machina/core/context.py`,
`machina/memory/consolidator.py`, `machina/core/models.py`,
`machina/core/session.py`.

Tests (pinned by changed behavior):
`tests/test_r5_f1_consolidation_cursor.py`,
`tests/test_held_55_history_order.py`,
`tests/test_final_review_fix_core_agent_context.py` (new),
`tests/test_architecture_layers.py` (delta-table entry + ceiling).
