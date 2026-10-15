# Final-review gate reconciliation — 2026-10-15

Fix lane to reconcile the no-mistakes gate rounds against earlier pins. Four
diagnosed failures were resolved with the smallest safe changes, keeping the
joined / close-once invariants in the messages. No commits were made.

## Files changed

| File | Change |
| --- | --- |
| `machina/core/agent_factory.py` | `close_agent_provider`: on caller cancellation, cancel the in-flight close, bounded-join it, then re-raise (was a shielded join that let the close hit its cleanup timeout). |
| `tests/test_chaos.py` | `test_agent_handles_db_connection_drop`: replaced the `await_count == 2` pin with a behavioral retained-batch retry assertion. |
| `tests/test_handoff_regressions.py` | `_page()` double: added the `before_seq` kwarg and threaded it into the 3-key keyset predicate/order. |
| `tests/test_integration_permissions.py` | `test_owned_provider_close_once`: re-anchored from a source grep to a behavioral exactly-once assertion across both close paths. |
| `tests/test_final_review_2026_10_15_gate_reconciliation.py` | New evidence test file pinning all four reconciled invariants. |
| `machina/core/cleanup.py` | Unchanged — the existing `cancel_and_join` helper provides the cancel-then-bounded-join seam. |

## Per-item status

### 1. `test_chaos.py::TestDatabaseChaos::test_agent_handles_db_connection_drop` — FIXED

Root cause: the run path flushes the user message, then the assistant message;
a failing assistant flush is *retained* (`_flush_pending_chunks` returns
`False` without clearing) and retried by the turn-close `joined_teardown`
cleanup. That is a legitimate third `add_chunks_batch` call, so the old
`await_count == 2` pin was stale.

Fix: replaced the count pin with a behavioral assertion that snapshots every
offered batch (the live pending list is cleared on success, so a stored
reference would not prove what was persisted): exactly **3** flush attempts,
and `batches[2] == batches[1]` — the retried batch is precisely the one whose
flush failed. `response.content == "test"` still holds, preserving the RED
meaning: *flush failure must not fail the turn*.

### 2. `test_handoff_regressions.py::test_keyset_pagination_counts_and_concurrent_insert` — FIXED

Root cause: `ContextManager.get_chunks_before` now takes the 3-key keyset
`(created_at, event_seq, id)` cursor and `get_all_chunks` passes `before_seq`
once it advances. The local `_page()` double lacked the kwarg → `TypeError`.

Fix: added `before_seq=None` to `_page()` and threaded it into the fake query
exactly like `before_id` — the fake now orders/filters on the same 3-key tuple
with `COALESCE(event_seq, <bigint floor>)` semantics. Pagination semantics are
unchanged (for the all-`None` `event_seq` fixtures the tuple reduces to
`(created_at, id)`).

### 3. `test_integration_permissions.py::test_owned_provider_close_once` — FIXED

Root cause: ownership handling moved out of `close_agent_provider` into
`_close_owned_provider`, so the `inspect.getsource(close_agent_provider)`
`"_owns_provider" in src` assertion no longer matched the seam.

Fix: re-anchored to a **behavioral** assertion — an owned provider is closed
exactly once across both paths (re-entering via `close_agent_provider` and via
`_close_owned_provider` issues no second `close()`), and ownership is consumed
(`agent._owns_provider is False`).

### 4. `test_provider_close_visibility.py::test_parent_cancellation_propagates_and_cancels_close` — FIXED

Root cause: `close_agent_provider` wrapped the work in `await_joined`, which
**shields** the operation from caller cancellation. On `task.cancel()` the
inner close never observed `CancelledError`; instead `bounded_cleanup` waited
out its 5s timeout and recorded `provider.close.timeout` (not
`provider.close.cancelled`).

Fix (in `close_agent_provider`): run the release/close in a task and `shield`
it; on `asyncio.CancelledError`, call `cancel_and_join([operation], timeout,
"owned provider")` — which cancels the **inner** close first (so
`bounded_cleanup` takes its `CancelledError` branch: cancels the close
coroutine and records `provider.close.cancelled`), bounded-joins it, then
re-raises. The independent timeout/quarantine path is untouched, so a genuinely
wedged close still retires and is observed.

## Verification

All logs under
`docs/verification/proofs/final_review_2026-10-15/logs/`.

### RED (pre-fix) — `fix_gate_reconciliation_RED.log`

Reverted the source/test changes (git stash) and ran the four diagnosed tests:

```
4 failed in 5.75s
FAILED tests/test_chaos.py::TestDatabaseChaos::test_agent_handles_db_connection_drop
FAILED tests/test_handoff_regressions.py::test_keyset_pagination_counts_and_concurrent_insert
FAILED tests/test_integration_permissions.py::test_owned_provider_close_once
FAILED tests/test_provider_close_visibility.py::test_parent_cancellation_propagates_and_cancels_close
```

### GREEN — `fix_gate_reconciliation_GREEN.log`

```
12 passed in 0.69s
```

(the four diagnosed tests + all of `test_provider_close_visibility.py` + the new
evidence test file).

### Adjacent suites — `fix_gate_reconciliation_adjacent_suites.log`

handoff, provider-close/visibility, deep-runtime-cleanup, integration_permissions,
chaos, agent_factory_memory, r4_105 caller_scope, the new evidence file, plus the
other `close_agent_provider` consumers (turn_engine, cli_turn_ownership, fm7_cli,
rest_ownership_loss, trace_hierarchy):

```
224 passed, 1 skipped in 19.01s
```

### Ruff — `fix_gate_reconciliation_ruff.log`

```
All checks passed!
7 files already formatted
```

## Invariants preserved

- **Flush failure never fails the turn** and never silently drops the batch
  (retained + retried at turn close).
- **3-key keyset cursor** `(created_at, event_seq, id)` for stable pagination
  under identical timestamps and concurrent inserts.
- **Close-once** for owned providers across both close entry points.
- **Cancellation** reaches the in-flight close (counted as
  `provider.close.cancelled`) and is bounded-joined before the error
  propagates; wedged closes still retire via the timeout/quarantine path.
