# R4-065 · R4-066 · R4-067 · R4-068 — approval deadline truthfulness + store ordering

Date: 2026-10-14 · Agent: R4-APPROV (round-4 implementation subagent)
Repo state: shared working tree at `cdc5232` + concurrent round-4 agent changes
(no commit made by this task).

## Scope and files

| Task | Production change | Test change |
|---|---|---|
| R4-065 | `machina/permissions/broker.py` (resolution sink + expiry emit), `machina/gateway/server.py` (6-line transport wiring), `ui/src/app.ts` (terminal-status pause retires card/footer) | `tests/test_r4_approvals_deadline.py`, `ui/test/r4-approval-expiry.test.ts` |
| R4-066 | `machina/permissions/broker.py` (`_approval_deadline`) | same backend test file |
| R4-067 | `machina/permissions/broker.py` (`_audit_previews`, audit payload) | same backend test file |
| R4-068 | `machina/permissions/store.py` (`complete_execution` durable-first, `create_approval` insert-failure cleanup) | same backend test file |

`machina/gateway/server.py` was touched outside the nominal file list because the
gateway is the transport that owns the decision channel (`_write`/`event_payload`);
the broker is transport-agnostic by design. The change is confined to the
approval-handler setup region (lines ~899-910) and does not overlap the
concurrent R4-010/R4-064 edits in the same function (verified by exact-match
edits against the live tree).

## Root causes (one line each, mechanism not symptom)

- **R4-065**: the broker's deadline path voluntarily resolved the record
  (`resolve_decision(..., "expired")`) but the store has no transport channel,
  so no `approval.resolved`/`permission.resolved` frame was ever emitted while
  the transport handler was cancelled — the UI card and `awaiting_approval`
  footer had no event that could retire them.
- **R4-066**: the deadline path treated `resolve_decision` as infallible — a
  conditional expire that lost to an already-`approved` row (`store.py:585-611`)
  still produced `NeedsApproval(status="expired")`, contradicting the durable
  record and leaving an approved-but-unclaimed row behind.
- **R4-067**: `audit_permission_verdict` emitted identity/operation metadata
  only; routine allowed actions (no card, no proposal in the UI) left audit
  rows that never named the acted-on path/argv.
- **R4-068**: `complete_execution` advanced the memory mirror **before** the
  durable conditional UPDATE (divergence on failure/outage), and
  `create_approval` left its in-memory pending record behind when the INSERT
  raised.

## R4-065 — expiry frame on the decision channel (E2E, compressed timeout)

`broker.py`:

- `set_approval_handler(..., on_resolved=...)` now carries an optional
  transport resolution sink (`_approval_resolution` ContextVar, default None,
  cleared by `set_approval_handler(None)`).
- `_notify_resolution(card)` calls the sink exactly once for a broker-recorded
  terminal decision; failures are recorded (`broker.approval_resolution`) and
  never alter the authorization outcome.
- The sink is invoked **only** on a genuine expiry — human decisions are
  recorded and announced by the transport that made them (no double emit).

`gateway/server.py` (`_run_turn`): `_approval_resolved` emits
`approval.resolved` (`requestId`, `status`) via the same `emit` closure used
for cards and `message.complete` — byte-identical channel/`event_payload` path
as `mode.approvals_resolve`'s decision frame; protocol v1 maps it to
`permission.resolved` through the existing `LEGACY_NAMES` table (additive, no
schema change).

`ui/src/app.ts` (`onEvent`, pause branch): a `needs_approval`/`permission.required`
frame whose `status` is present and not `pending` is a resolution, not a card:
retained evidence is dropped, an open card is recorded/closed, the footer
leaves `awaiting_approval` (via the existing `close()` → `showApprovalDock()`
path), a notice is shown for the visible session, and no decision RPC is ever
sent. The `approval.resolved` branch continues to retire frames of that shape.

### E2E proof — `tests/test_r4_approvals_deadline.py`

Harness: real `Gateway._run_turn_with_timeout` (stdio `write` captured), fake
agent driving the **real** `permission_broker.guard`, real store (`machina_test`
via the isolated-db seam) or the documented memory fallback, compressed
`approval_timeout = 0.2s`.

- `test_expiry_emits_resolution_frame_memory_store`
- `test_expiry_emits_resolution_frame_durable_store`

Assertions: card frame (`permission.required`, status `pending`) precedes a
resolution frame (`approval.resolved`/`permission.resolved`, same `requestId`,
status `expired`); no `error` frame; `message.complete` still arrives (the turn
moved on); store row ends `expired`.

### RED (unfixed, scratch worktree `git worktree add --detach HEAD`)

`R4-065_068_red_backend.log` — both E2E tests fail with:

```
AssertionError: R4-065: the broker-recorded expiry must be emitted on the decision
channel; frames were: [('message.start', None), ('permission.required', 'pending'),
('message.complete', None)]
```

The card is emitted; nothing retires it. UI RED (`R4-065_068_red_ui.log`):
3 of 4 UI tests fail pre-fix (terminal-status pause opens a card / stays open).

## R4-066 — expired-vs-approved durable race (adversarial timing, N=5)

`_approval_deadline(req, approval, principal)`:

1. Attempt the conditional expire. If it commits → notify + `NeedsApproval(expired)`.
2. If it loses (already resolved), read the **durable** record:
   - `approved` + unconsumed + digest matches → `claim_execution` → stamp →
     `permission_allowed: "approval granted at deadline"` → return (execute once).
   - `approved` but not claimable (changed digest/consumed) → `NeedsApproval(record)`
     (live truth, fail closed).
   - `pending` → `NeedsApproval(record)` (never a false expiry).
   - terminal (`denied`/`cancelled`/`claimed`/…) → `ApprovalDenied("approval already <state>")`
     (no stale-approved replay).

Tests (each parametrized ×5, real DB):

- `test_resolve_before_deadline_is_honoured_exactly_once[0..4]` — deterministic
  adversarial construction: the handler commits `approved` durably, then stalls
  past the deadline so `wait_for` cancels it. Asserts allowed semantics, row
  `claimed`+`consumed`, completion exactly once, `find_approved_unclaimed` empty,
  same-request replay denied with the handler never re-invoked.
- `test_decide_at_deadline_invariant_never_mismatches_durable[0.0…1.0]` —
  jittered decide-at-deadline; asserts reported outcome matches durable truth
  (`allowed` ⇒ `claimed`; `expired` ⇒ `expired`). Post-fix run:
  `jitter=0.0/0.5/0.9 → allowed=True`, `jitter=0.99/1.0 → allowed=False (genuine expiry)`.

RED: all five deterministic rounds fail pre-fix with
`machina.permissions.broker.NeedsApproval: needs_approval:<id>` raised from the
unconditional `raise NeedsApproval({**approval, "status": "expired"})` — i.e. a
reported expiry over a durably approved row. The jitter suite held its invariant
in the sampled pre-fix interleavings (the stall window is microseconds wide) and
is kept as a standing N≥5 safety net; the deterministic suite is the RED proof.

## R4-067 — audit target/argv context for routine allowed actions

`audit_permission_verdict` now includes `targets` and `argv` previews built by
`_audit_previews`: at most `AUDIT_PREVIEW_ITEMS = 3` entries per list, each
redacted (`_redacted` → `redact_secrets`, fail-closed marker on error) and
length-capped at `AUDIT_PREVIEW_MAX = 200` with the existing explicit
`…(+N chars)` hidden-count marker. The execution digest prefix remains the
exact, unredacted binding; `audit_log`'s own sanitizer stays as defence in depth.

Proof (`test_routine_allowed_rows_carry_bounded_redacted_target_context`; real
`audit_log` pipeline captured at the persistence sink — only
`audit_persistence.submit` is stubbed):

- routine workspace `file.write` → `permission_allowed` row carries the exact
  target path and `argv: []`;
- routine `echo` exec whose argv embeds `Bearer sk-ant-…` → argv preview keeps
  the shape (`echo`, `Authorization …`) but the raw secret appears nowhere and
  `[REDACTED_…]` is present;
- secret-shaped file name → target preview redacted, no raw secret in the row;
- 5 targets / 5 argv items → exactly 3 previews; 244-char name → truncated with
  `…(+`.

RED: `KeyError: 'targets'` on the first assertion (rows carried no target
information at all).

## R4-068 — store ordering (F6)

`complete_execution` (durable-first): the durable conditional UPDATE runs
first; the memory mirror advances (`status = outcome`) only after it confirms.
A durable conditional failure fails closed in memory (`consumed = True`, status
untouched). With the DB unreachable, a durable-origin record is never completed
from the mirror (`return False`, mirror stays `claimed`); a memory-authority
record still completes locally (`_last_durable = False`).

`create_approval`: when the INSERT raises, the just-created in-memory record is
removed under the lock (`is rec` identity guard, so a concurrent different
record is never popped) before the exception propagates.

Proof:

- `test_create_approval_insert_failure_leaves_no_orphan` — real `guard` against
  a DB boundary whose INSERT raises: `RuntimeError` propagates (never
  swallowed), `request_id` is absent from the mirror, `get_approval` is None.
  RED: the orphan remained.
- `test_complete_execution_durable_first_under_failure` — real DB row claimed,
  durable completion UPDATE injected to fail: exception propagates, mirror and
  durable row both still `claimed`; after removing the failure, the completion
  confirms and mirrors once, duplicate completion returns False.
  RED: memory showed `completed` while durable truth was `claimed`.
- `test_complete_execution_refuses_durable_record_without_db` — claimed row,
  DB unreachable: returns False and the mirror stays `claimed`. RED: returned
  True with a memory-only `completed`.
- `test_memory_only_completion_still_works_without_db` — control (passes both
  states).

## Commands and evidence

```bash
# RED (scratch worktree at cdc5232, unfixed production code + new tests)
git worktree add --detach <tmp>/machina-r4-red HEAD
cp tests/test_r4_approvals_deadline.py <wt>/tests/; cp ui/test/r4-approval-expiry.test.ts <wt>/ui/test/
cd <wt> && PYTHONPATH=<wt> .venv/bin/python -m pytest tests/test_r4_approvals_deadline.py -q -p no:cacheprovider
cd <wt>/ui && node --test --test-reporter=spec test/r4-approval-expiry.test.ts

# GREEN (fixed tree)
MACHINA_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/machina_test \
  .venv/bin/python -m pytest tests/test_r4_approvals_deadline.py -q -p no:cacheprovider   # 17 passed
cd ui && node --test --test-reporter=spec test/r4-approval-expiry.test.ts                 # 4 passed

# Required CHECK list + wider broker/store/gateway sweep (521 passed, 1 skipped)
MACHINA_TEST_DATABASE_URL=... .venv/bin/python -m pytest \
  tests/test_r4_approvals_deadline.py tests/test_fm3_approval_store.py \
  tests/test_approvals_ownership.py tests/test_integration_permissions.py \
  tests/test_http_approval_resume.py tests/test_approval_batch.py \
  tests/test_architecture_layers.py tests/test_fm3_audit_verdicts.py \
  tests/test_approval_budget_active_wait.py tests/test_approval_wait_renewal.py \
  tests/test_architecture_pairs.py tests/test_connectors_execution.py \
  tests/test_connectors_oauth_execution.py tests/test_debug_permissions_hardening.py \
  tests/test_deep_gateway_cleanup.py tests/test_deep_job_script_snapshot.py \
  tests/test_deep_scheduler.py tests/test_fm8_s5_broker.py \
  tests/test_history_condensation.py tests/test_multi_worker_fencing.py \
  tests/test_permission_risk_policy.py tests/test_s5_guard_coverage.py \
  tests/test_scheduler_coverage.py tests/test_scheduler_scripts.py \
  tests/test_script_dependency_boundary.py tests/test_shell_commands.py \
  tests/test_turnscope_cache.py tests/test_turn_engine.py \
  tests/test_deep_runtime_shutdown.py tests/test_observability_payload_boundary.py \
  -q -p no:cacheprovider

.venv/bin/python -m ruff check machina/permissions/broker.py machina/permissions/store.py \
  machina/gateway/server.py tests/test_r4_approvals_deadline.py
.venv/bin/python -m ruff format --check machina/permissions/broker.py \
  machina/permissions/store.py machina/gateway/server.py tests/test_r4_approvals_deadline.py
cd ui && npm run check     # typecheck + 430 passed / 6 skipped + eslint, all green
```

Evidence files:

- `R4-065_068_red_backend.log` — 11 failed, 6 passed (unfixed worktree).
- `R4-065_068_red_ui.log` — 3 failed, 1 passed (unfixed worktree).
- `R4-065_068_green_backend.log` — 17 passed (fixed tree).
- `R4-065_068_green_ui.log` — 4 passed (fixed tree).
- `R4-065_068_checks.log` — 521 passed, 1 skipped (required list + sweep).

Stability: `tests/test_r4_approvals_deadline.py` run 3× back-to-back: 17 passed
each time. Redaction/truncation probe:
`_redacted("Authorization: Bearer sk-ant-…")` →
`Authorization: Bearer [REDACTED_ANTHROPIC_KEY]`.

## Before / after

| Behavior | Before | After |
|---|---|---|
| Approval deadline (no decision) | store row `expired`; transport handler cancelled; **no frame** — UI card + `awaiting_approval` footer persist | `approval.resolved {status:"expired"}` emitted on the decision channel; card/footer retire; no decision sent |
| Decision lands just before deadline | reported `expired`; durable row `approved` (unclaimed, replayable) | durable `approved` honoured: claimed once, executed as approved, `permission_allowed: "approval granted at deadline"`; replay denied |
| Routine allowed write/exec audit row | no target/argv context | bounded (≤3), redacted (≤200 chars, hidden-count marker), no raw secrets |
| `complete_execution` with failing/absent DB | memory mirror advanced first; could report terminal state durable never saw | durable-first; mirror advances only on confirmation; fail-closed `consumed` marker on conditional failure; durable-origin completion refused while DB is down |
| `create_approval` INSERT failure | orphan pending record left in memory | record removed; exception still propagates |

## Residual risks / notes

1. **Gateway touch outside the nominal file list** (see Scope). The sink param is
   additive (`on_resolved=None` default); existing callers
   (`streaming.py`, connectors' direct ContextVar use, tests) are unchanged.
2. **Sink is opt-in**: transports that never register a sink (headless CLI/REST)
   emit nothing, matching their semantics — there is no interactive deadline on
   the headless path, and the REST resolve response already carries its event.
3. **UI terminal-status guard is defensive**: the TUI's own expiry path retires
   through `approval.resolved`; the guard covers pause frames that already carry
   a terminal status (e.g. SSE v2 forwards store status, redeliveries). The card
   path for `status` absent/`pending` is untouched.
4. **One transient cross-process DB deadlock** (`DeadlockDetectedError` in
   `create_approval`) was observed once while another agent suite ran against the
   shared `machina_test` DB; it was not reproducible in isolation or in subsequent
   runs (`DELETE FROM sessions` + FK lock ordering across processes). Test-only
   environment artifact; no production lock-order change was made.
5. **Audit cost**: up to 3+3 redaction calls added per verdict on the existing
   audit path (one `_redacted(reason)` already ran there); previews are bounded
   and the emission already leaves the hot path via `audit_persistence`.
6. **No new audit verdict names** were added; expiry still appears in the ledger
   as `permission_pending` plus the new resolution frame. Terminal expiry audit
   rows were considered and left out to keep the change minimal.
7. `complete_execution` returning False while the DB is unreachable is an honest
   degradation (durable row stays `claimed`, retry-able); `broker.complete`
   already treats a False/exception as best-effort.
