# R4-105 — Missing coverage: scope binding, load race, flock, approval view, advisory-lock pool

- Round: 4 · Date: 2026-10-15 · Lane: R4-G3.
- Scope rule honored: NEW test files only; zero production edits
  (`git status` confirms `machina/core/agent.py`, `machina/core/context.py`,
  `machina/core/session_recall.py` untouched). `permissions/store.py` and
  `permissions/broker.py` used read-only (another lane owns them).
- DB: `MACHINA_TEST_DATABASE_URL` (`machina_test`). Mocks only at the
  LLM-provider boundary; every fixed seam is real.
- Headline results:
  - 5 new files, 9 test outcomes: **8 passed + 1 strict-xfail**, 6/6 full-set
    repeats green at final state.
  - RED-first: `(a)` fails against binding-neutralized `agent.py` (2 failed);
    `(d)` target-column case fails against current `permissions/store.py`
    (pinned as strict-xfail for the store lane).
  - One REAL finding for the store lane: the top-level approval `target`
    column is persisted raw and `public_view()` does not redact it.

## Deliverables

### (a) True caller-scope binding — `tests/test_r4_105_caller_scope_real_run.py`

Replaces the AH-018 vacuum (`test_handoff_regressions.py:471-519` seeds the
ContextVars by hand and asserts `seen == {}` — the probe never runs).
Two tests drive the REAL `ReActAgent.run()` loop, REAL permission broker,
REAL context/session/usage stores, and the REAL
`machina.tools.agents.active_agent_scope()` including its DB ownership check
(`sessions.agent_id` in the test DB). Probe tools call `active_agent_scope()`
for real inside the tool task.

- `test_scope_probe_sees_real_caller_scope_in_agent_run`: 2-tool batch;
  both probes observe exactly `(harness, session.id)`; scope is `None` after
  the run; negative control (unbound `active_agent_scope()` raises
  `ToolError`) in the same process.
- `test_concurrent_runs_keep_speaker_scope_isolated_per_tool`: two
  concurrent `run()`s (harness/researcher, distinct sessions) — every tool
  task sees ONLY its own run's scope.
- RED: with the two `_run_one` binding lines neutralized
  (`current_session_id.set(None)` / `current_agent_id.set(None)`), both
  tests FAIL (probes raise `ToolError: tool requires an active agent
  session`); restored file is byte-identical to HEAD (`git status` clean).

### (b) Same-session read/write load race — `tests/test_r4_105_same_session_load_race.py`

Replaces the wrong-seam `test_concurrent_context_mixed_read_write`
(readers/writers on DIFFERENT sessions; itself admits reader counts "0..M —
that's OK"). `ContextManager` has no read-side cache, so the proven claim is
DB-level. Per session: 1 writer (10 units; `unit_size` 1 and 5) + 2 readers on
the SAME session, ×4 sessions, each unit a single write call.

- Invariants: every sample is a complete duplicate-free unit prefix
  (no gaps/reorders/partial batches); per-reader sequences are monotonic
  (no stale reads); writer blocks after unit 0 until a reader records exactly
  `[0]` (deterministic in-flight proof, not timing luck); authoritative
  post-completion re-read sees exactly the writer-returned ids; DB count
  matches.
- Two harness bugs found-and-fixed during development (both documented as
  non-product issues): merging two readers' sequences breaks per-reader
  ordering; a reader statement snapshot taken before the final commit may
  miss it (read-committed semantics) — completeness is now asserted on a
  sequential post-join read.
- 8/8 single-file repeats + 6/6 full-set repeats green.

### (c) Cross-process flock contention — `tests/test_r4_105_store_fence_flock.py`

Real child process holds production `store_lock`; parent attempts ≥5
contended acquisitions: each raises typed `StoreLockTimeout` inside the
bound (message names the lock file, no credential material), with open-fd
count unchanged; holder release → prompt success; 20 acquire/release
round-trips leak no fds. 5/5 repeats green.

### (d) No secrets in approval view — `tests/test_r4_105_approval_view_secrets.py`

REAL broker (interactive pause) over a REAL approval store, secret
registered via `remember_secret_value` (the provider-key/OAuth-token path)
in argv AND in a target. Asserts the live handler card, the durable
`get_approval` record, and `list_pending` → `public_view` (the gateway/REST/
CLI shared surface); plus display-redaction never weakens execution
identity (approved request still claims with raw argv + exact digest).

- argv case: FULLY GREEN end to end.
- Target case, live card: GREEN. Target case, durable record + listing:
  **FAILS — pinned as `strict=True` xfail for the `permissions/store.py`
  lane**: the top-level `target` column is persisted raw
  (`create_approval`: `"target": "|".join(req.targets)[:500]`) and
  `public_view()` redacts `proposal.targets` but not `target`, so a secret
  embedded in *targets* reaches `list_pending`/`get_approval`.

### (e) Advisory-lock pool path — `tests/test_r4_105_advisory_lock_pool.py`

Prior coverage mapped: `test_load_concurrency` (usage advisory-lock ordering,
exactly-one winner) and `test_memory_bug_fixes`/`test_fm5_consistency`
(consolidator dedicated-connection lock); R10 starvation fix proven in the
review's `memory_pool.py`. Deterministic gap closed here: a REJECTED
reservation must release its transaction-scoped advisory locks AND its pool
connection — 8 concurrent same-session attempts, exactly 1 winner / 7
`UsageBudgetExceededError`, then a bounded retry on the same scopes
succeeds (a leaked lock/connection would block it past the timeout), with
zero orphaned `reserved` rows.

## Verification

- New files: 8 passed + 1 xfail (strict), 6 consecutive full-set repeats.
- Required suites: `test_sse_stream_adversarial.py`,
  `test_architecture_layers.py`, `test_production_smoke.py` — 37 passed.
- Adjacent suites: `test_handoff_regressions`, `test_fm3_approval_store`,
  `test_delegate_scope`, `test_usage`, `test_r4_022_denied_outcome` —
  79 passed.
- `ruff check` + `ruff format --check`: clean on all 5 files.

## Concurrency note (shared-workspace determinism)

The shared `machina_test` database is hammered by parallel lanes
(full-suite runs, public-schema migrations), which caused two classes of
non-product flakes during development: cross-process DDL deadlocks (another
lane's `ALTER TABLE` vs my open writes) and a per-test `db_pool` fixture
deadlock (`initialize_schema` DDL vs concurrent data writes). All four
DB-backed files therefore use the repo's `tests.support.schema`
`isolated_schema` harness with a SELF-MANAGED administrator connection —
deliberately independent of the `db_pool` fixture — so no test in this lane
runs, or deadlocks on, public-schema DDL. No production change was needed.

## Residuals

1. `permissions/store.py` lane: redact the top-level `target` column
   (persist redacted or redact in `public_view`/`get_approval`); then flip
   the strict xfail in `test_target_secret_is_redacted_in_durable_record_and_listing`.
2. No production edits made in this lane; none required.
3. NOT committed, per lane instructions.
