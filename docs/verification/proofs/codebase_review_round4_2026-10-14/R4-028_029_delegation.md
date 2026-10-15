# R4-028 + R4-029 — delegation artifact honesty and ledger-failure row recovery

- Date: 2026-10-14
- Base tree: `6f0df5b` + in-flight wave-D edits (no commit made, per instruction)
- Owner decision (R4-028): **delete** the dead artifact backtrack
- Verdict: RED-on-unfixed demonstrated; GREEN after fix; no new deps; no schema semantics removed

## R4-028 — artifact-contract "backtrack" is a no-op (delete)

**Probe.** `machina/core/orchestrator.py` called
`_progress.should_write_first(0, True)` and discarded the return; the comment
claimed a "forced synthesize/write attempt". `progress.should_write_first` is a
pure predicate — the call could not write anything.

**Root cause.** A comment described behavior the code never performed; the test
`test_artifact_missing_errors_and_backtracks` pinned the call, laundering the
no-op as a contract.

**Fix.** Deleted the call, its comment, and the try/except that logged
`orchestrator.delegate.backtrack`. The missing-artifact path is otherwise
unchanged: `status="error"`, `final_content=<resolved reason>`, terminal row
recorded. `machina/core/progress.py` is untouched: `should_write_first` remains a
tested pure helper (`tests/test_progress_monitor.py`) — removing the helper is
dead-code-wave work (R4-096), not this task.

**Replacement test.** `tests/test_delegation_contracts.py::
test_artifact_missing_errors_honestly` pins the real behavior: status `error`,
reason names the missing path, terminal `error` row carries the reason, response
makes no "backtrack" claim, and `should_write_first` is **never called again**
(a spy would fail if the no-op is reintroduced).

**RED (against unfixed code).**

```
FAILED tests/test_delegation_contracts.py::test_artifact_missing_errors_honestly
  spy.assert_not_called() -> AssertionError: Expected 'should_write_first' to not
  have been called. Called 1 times.
```

## R4-029 — delegation row recovery on post-run ledger failure

**Probe.** `_charged_child_tokens` raises `DelegationAccountingError` when the
child ledger read fails while the DB is connected. That read runs after the
child has already run (success path, `orchestrator.py:404`) and inside the
child-error and cancellation handlers. Before the fix the exception escaped
without `_record_end`, leaving a `pending` row forever and dropping the typed
outcome; callers only saw the accounting error.

**Root cause.** The accounting read was not part of any row-closing guarantee:
one failing await between `_record_start` and `_record_end` stranded the row.

**Fix (smallest safe).**

- New helper `Orchestrator._close_row_with_unknown_spend(message_id, status,
  response)`: records the terminal row with `tokens=None` — **SQL NULL =
  explicitly unknown, never zero**. A failed bookkeeping write is reported via
  `record_failure("orchestrator.delegate.accounting_row", ...)`; it never masks
  the typed error the caller re-raises.
- All three post-run read sites now close the row before propagating:
  - success path → `status="error"`, `response="Error: <accounting error>"`, then `raise`
  - child-error path → same (exactly one `_record_end`; the child error remains logged)
  - cancellation path → `status="cancelled"`, `response="cancelled"`, then `raise`
    (cancellation is terminal and still propagates)
- Caller mapping unchanged and explicit: `tools/agents.py` re-raises
  `DelegationAccountingError` ("A parent must not continue spending when its
  child's usage is unknown"), so the parent loop still stops (pinned by
  `test_failed_child_spend_is_accounted_without_implicit_turn_cutoff`).
- `_record_end` now accepts `tokens: int | None`. Schema supports the NULL:
  `agent_messages.tokens_used` becomes nullable in `machina/db/schema.sql` and
  via an idempotent
  `ALTER TABLE agent_messages ALTER COLUMN tokens_used DROP NOT NULL;` in
  `machina/db/delegation_schema.sql` (runtime migrations, applied for fresh and
  existing installs). Without the migration the recovery write would violate
  NOT NULL and the row would stay stranded — the migration is part of the fix.

**Tests (deliberate update of the zero-record pin).**

- `test_unavailable_production_accounting_never_reports_zero[raise|success]`
  (was `delegation.recorded == []`) now asserts exactly one terminal `error`
  row with `tokens is None` and the accounting message — and that the typed
  error is still raised.
- `test_failed_child_spend_is_accounted_without_implicit_turn_cutoff
  [ledger_unavailable-*]` now also asserts row closure (`error`, `tokens=None`)
  at the real parent-loop seam (run and stream).
- New `test_ledger_failure_closes_cancelled_row_with_unknown_tokens` covers the
  cancellation branch.
- New E2E `test_ledger_failure_closes_real_row_with_unknown_tokens` (`db_pool`):
  production INSERT creates the row, injected ledger failure raises, production
  UPDATE closes it, and the row is read back from Postgres —
  `status='error'`, `tokens_used IS NULL`, response names the failure. This is
  the real-seam proof (no mocked `_record_end`).

**RED (against unfixed code).**

```
FAILED test_unavailable_production_accounting_never_reports_zero[raise]   (recorded == [])
FAILED test_unavailable_production_accounting_never_reports_zero[success] (recorded == [])
FAILED test_failed_child_spend_is_accounted_without_implicit_turn_cutoff[ledger_unavailable-False]
FAILED test_failed_child_spend_is_accounted_without_implicit_turn_cutoff[ledger_unavailable-True]
FAILED test_ledger_failure_closes_cancelled_row_with_unknown_tokens       (recorded == [])
FAILED test_ledger_failure_closes_real_row_with_unknown_tokens
  AssertionError: assert 'pending' == 'error'   <- row stranded in Postgres
```

Full RED log: `R4-028_029_red.txt` (7 failed, 23 passed — the 7th was R4-028).

## Commands (all with `MACHINA_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/machina_test`)

```
.venv/bin/python -m pytest tests/test_delegation_contracts.py \
  tests/test_failed_delegation_usage.py -q -p no:cacheprovider
  -> 30 passed  (R4-028_029_green_focused.txt)

.venv/bin/python -m pytest tests/test_delegation_contracts.py \
  tests/test_failed_delegation_usage.py tests/test_multi_agent.py \
  tests/test_bug_fixes.py tests/test_orchestrator_usage.py \
  tests/test_architecture_layers.py tests/test_a5_turn_flow.py \
  tests/test_delegation_outcomes.py tests/test_delegation_boundaries.py \
  tests/test_review_delegation.py tests/test_phase5_completion.py \
  -q -p no:cacheprovider
  -> 142 passed, twice consecutively  (R4-028_029_green_related.txt)

.venv/bin/python -m pytest tests/test_chaos_extended.py \
  tests/test_progress_monitor.py tests/test_replay_harness.py \
  tests/test_tool_timeout_contract.py -q -p no:cacheprovider
  -> 80 passed

.venv/bin/python -m pytest tests/test_schema_conformance.py \
  tests/test_runtime_migrations.py -q -p no:cacheprovider
  -> 7 passed

.venv/bin/python -m ruff format --check machina/core/orchestrator.py \
  tests/test_delegation_contracts.py tests/test_failed_delegation_usage.py
.venv/bin/python -m ruff check machina/core/orchestrator.py \
  tests/test_delegation_contracts.py tests/test_failed_delegation_usage.py
  -> already formatted / All checks passed  (R4-028_029_green_ruff.txt)
```

Notes on transient runs: one mid-edit run failed on an unrelated
`machina/skills` import (concurrent writer) and one showed
`asyncpg DeadlockDetectedError` + provider-ratchet failures from another lane's
in-flight edits; reruns on the settled tree were green (see the repeat block in
`R4-028_029_green_related.txt`).

## Pre-existing flake found while verifying (and repaired)

`test_failed_child_spend_is_accounted_without_implicit_turn_cutoff` flaked on
**unmodified HEAD** (clean worktree, `6f0df5b`): 9 failing runs out of 20
(`R4-028_029_pre_existing_flake.log`), always through the 20 ms compressed
deadline — `asyncio.wait_for` can expire before the child's first step under
load, so the fixture charge is never set and the row/parent report `tokens=0`
(traceback: `'response': 'TimeoutError: delegation timed out', 'tokens': 0`).
Not caused by this change; it undermines the determinism of this verification,
so the test's deadline setup was made deterministic without weakening any
assertion: only the `timeout` outcome keeps a compressed deadline (0.25 s, wide
startup margin) while `raise`/`ledger_unavailable` use the normal 120 s deadline
(the child settles without the clock). After the repair: 20/20 runs green for
the file.

## Residual risks / honest notes

- If the database is unreachable, the recovery write itself fails; it is
  reported (`orchestrator.delegate.accounting_row`) and the typed accounting
  error still propagates. A fully down DB cannot be given a terminal row by
  definition.
- Consumers of `orchestrator.history()` / gateway `agents` may now see
  `tokens: null` for recovered rows. Checked in-tree: the value is passed
  through serialization only (no arithmetic), and no UI consumer reads that
  field.
- `DelegationResult.tokens` remains `int`; unknown spend never materializes as a
  result because those paths raise the typed error (caller contract unchanged).
- `should_write_first` is now unused by production code but retained as a tested
  pure helper; R4-096 owns any removal decision.

## Files changed (no commit)

- `machina/core/orchestrator.py` — R4-028 delete; R4-029 recovery helper +
  three call sites; `_record_end(tokens: int | None)`
- `machina/db/schema.sql`, `machina/db/delegation_schema.sql` — nullable
  `agent_messages.tokens_used` (unknown = NULL)
- `tests/test_delegation_contracts.py` — honest missing-artifact test
- `tests/test_failed_delegation_usage.py` — updated zero-record pin, three new
  recovery tests, deterministic deadline setup
