# R4-066b — approval deadline report-vs-durable window

Date: 2026-10-14 · Agent: R4-DW (root-cause/fix subagent, no commit)
Parent failure: commit `1695f7c` gate —
`tests/test_r4_approvals_deadline.py::test_decide_at_deadline_invariant_never_mismatches_durable[1.0]`
failed with `AssertionError: R4-066: expiry reported while durable truth is 'approved'`
(broker raised `NeedsApproval(status="expired")`, post-hoc `get_approval` read durable
`"approved"`). Isolated runs passed (5/5 ×3, full file 17/17); the leak appeared only
under heavy CPU/DB contention.

## Root cause (one sentence)

`PermissionBroker._approval_deadline` derived its report from the local
`resolve_decision("expired")` return (this worker's commit-time view) instead of the
durable record, so a human `approved` decision that became durable in the window between
the expire's conditional UPDATE and the broker's report was misreported as `"expired"`
and left as a claimable/replayable approved row.

## Interleaving

Pre-fix `_approval_deadline` (`machina/permissions/broker.py`):

```
broker worker W (deadline path)            human decision / other worker H
─────────────────────────────────          ───────────────────────────────
await wait_for(handler) → TimeoutError
resolve_decision(rid, "expired")
  SELECT … FOR UPDATE → pending            H: resolve_decision(rid,"approved")
  UPDATE … SET expired … → 1 row             (blocked on the row lock, or its
  COMMIT "expired"                             commit lands a moment later)
  returns {"status": "expired"}            H: COMMIT "approved"  ← TOO LATE
                                             for W's conditional UPDATE,
                                             but DURABLE before W reports
if resolved is not None:
    raise NeedsApproval(status="expired")  ← report from the local return
post-hoc get_approval → "approved"         ← durable truth disagrees: INVARIANT BROKEN
+ durable row "approved"+unclaimed left behind → replayable by a later retry
```

The conditional UPDATE is atomic and race-safe (exactly one of approve/expire wins the
`pending` row — verified separately), but atomicity of the *write* does not cover the
*report*: any commit landing after the UPDATE yet before the report (a second worker's
decision, or an in-flight commit whose row lock was queued behind the expire's) was
invisible to the `resolved is not None` branch.

Suspects (a)–(c) from the task brief, adjudicated:

- (a) memory mirror advancing on a lost race — NOT the cause here: `resolve_decision`'s
  durable branch mirrors the verdict into `_mem` only after the conditional UPDATE
  confirms, and on a lost race mirrors durable truth (`stale` path, `store.py:611-618`).
- (b) re-read racing between UPDATE and RETURNING — NOT the mechanism (single-statement
  conditional UPDATE has no such gap); the real gap is UPDATE-vs-report across workers.
- (c) `get_approval` precedence — NOT the cause when the DB is reachable: the durable
  branch returns the row directly and never consults the mirror (`store.py:779-795`).

## Fix (`machina/permissions/broker.py`, `_approval_deadline` only; `store.py` unchanged)

The report is now derived from a fresh durable read, never from the resolve return:

```python
resolved = await _store.resolve_decision(req.request_id, "expired", principal)
# F6: only the durable record may be reported as the verdict. Re-read
# the authority after the conditional expire …
current = await _store.get_approval(req.request_id) or {}
state = str(current.get("status") or "")
if state == "expired":
    if resolved is not None:
        _notify_resolution({**approval, "status": "expired"})  # R4-065, self-recorded only
    raise NeedsApproval({**approval, "status": "expired"}) from None
if state == "approved" and not current.get("consumed"):
    … claim_execution → execute exactly once …
if state == "pending":
    raise NeedsApproval(current) from None          # never a false expiry
… terminal … → ApprovalDenied("approval already <state>")   # never replayable
```

Why this holds for all interleavings:

- Expire won durably → state `expired` → report `expired` (notify only if this broker
  recorded it, preserving R4-065 single-emit). An `expired` verdict is now true iff the
  durable row carries it.
- Approve won durably (before, during, or after the expire attempt) → state `approved`
  → claimed and executed exactly once via the conditional `claim_execution`
  (exactly-one-winner across workers); a later retry sees `claimed`/terminal and is
  denied — never replayable, never reported expired.
- Neither won (still `pending`, e.g. expire lost to a concurrent non-terminal path) →
  report the live `pending` record, never a false expiry.
- F6 preserved: no memory write in this path at all; `resolve_decision` /
  `claim_execution` keep mirror-after-durable-confirm; `get_approval` reads the durable
  authority whenever reachable.

## Regression tests (`tests/test_r4_approvals_deadline.py`, +2, file now 19 tests)

- `test_deadline_report_derives_from_durable_record_not_resolve_return` — DETERMINISTIC
  pin of the fixed interleave (no wall-clock dependence): a hook wraps
  `resolve_decision` so that after the broker's `expired` commit, a decision commit
  lands durably before the report. Asserts allowed-once semantics, row
  `claimed`+`consumed`, `find_approved_unclaimed` empty, same-request replay denied.
- `test_get_approval_prefers_durable_truth_over_a_stale_mirror` — F6 pin: with the DB
  reachable, a mirror status the durable row never carried (`expired` durable vs
  `approved` mirror) is never surfaced and never authorizes `claim_execution` (the
  conditional claim fails closed and marks the mirror consumed).
- The existing jitter suite
  (`test_decide_at_deadline_invariant_never_mismatches_durable[0.0…1.0]`) is kept as
  the standing N=5 timing safety net.

## RED / GREEN evidence

- RED (pre-fix broker, new pin): `1 failed in 0.59s` —
  `Failed: R4-066b: reported 'expired' while the durable row is 'approved'`
  (raised from old `broker.py:312`, the exact gate failure class).
- GREEN (fixed broker): new pins `2 passed`; full file `19 passed`;
  required sweep
  `test_r4_approvals_deadline.py + test_fm3_approval_store.py +
  test_approvals_ownership.py + test_integration_permissions.py` →
  `89 passed, 1 skipped`; `test_architecture_layers.py` → `25 passed`;
  `ruff format --check` + `ruff check` clean on all three touched files.
- Jitter suite ×10 sequential on a quiesced DB: `5 passed` all ten runs.
- Load runs (`yes` CPU hogs ×6 and concurrent DB suites): failures observed were
  exclusively `asyncpg.exceptions.DeadlockDetectedError` in `create_approval`'s INSERT
  (cross-process DDL/teardown contention on the shared `machina_test` DB — concurrent
  `initialize_schema` ShareLocks vs the INSERT's `sessions` FK check), occurring before
  any deadline logic executes. No `expired`-over-`approved` invariant violation was
  observed in any loaded run. The deadlock class is a shared-test-DB isolation issue
  (parallel suites + parallel schema init against one database), outside this lane's
  broker/store invariant.

## Residual risks

1. The deadline path adds one durable read per expiry (~1 SELECT on a human-paced path;
   no hot-path impact — approvals resolve at human speed, and the read only runs after
   a timeout fires).
2. A decision landing durably *after* the broker's re-read is still honoured on retry:
   the row stays `approved`+unclaimed and any resume path (`_guard_interactive`
   approved-branch, headless `find_approved_unclaimed`) claims it exactly once — no
   replay, no loss. The window is narrowed to read-to-report (microseconds) rather than
   closed absolutely; absolute closure would require holding the row lock across the
   claim, which would serialize all approvals on one lock.
3. Shared-DB cross-suite deadlocks under parallel full-suite load remain (see above);
   mitigation belongs to test isolation (per-suite databases/serial gates), not to the
   authorization invariant.

## Files changed (no commit)

- `machina/permissions/broker.py` — `_approval_deadline` reports from the durable
  re-read; notify gated on self-recorded expiry.
- `tests/test_r4_approvals_deadline.py` — two deterministic regression pins above.
- `machina/permissions/store.py` — read, verified F6-clean; no change needed.

## Verification commands

```
export MACHINA_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/machina_test
.venv/bin/python -m pytest tests/test_r4_approvals_deadline.py tests/test_fm3_approval_store.py tests/test_approvals_ownership.py tests/test_integration_permissions.py -q -p no:cacheprovider
.venv/bin/python -m pytest tests/test_architecture_layers.py -q -p no:cacheprovider
.venv/bin/ruff format --check machina/permissions/broker.py machina/permissions/store.py tests/test_r4_approvals_deadline.py
.venv/bin/ruff check machina/permissions/broker.py machina/permissions/store.py tests/test_r4_approvals_deadline.py
```
