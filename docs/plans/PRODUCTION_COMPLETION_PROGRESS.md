# Production completion progress

Plan: [PRODUCTION_COMPLETION_PLAN.md](PRODUCTION_COMPLETION_PLAN.md)

Base: `e879275`; implementation branch: `codex/production-completion`.

The supplied plan is the acceptance specification. An item is complete only after
its implementation, negative cases, integration checks, and documentation agree.
The v1.0.0 release gate remains open until operational requirements are verified.

## Decisions

- Ruling: implement on a dedicated branch in the existing checkout. The user
  requested work in this workspace; creating another checkout is unnecessary.
- Ruling: the supplied document serves as both specification and plan. Follow its
  implementation order; investigate claims against current source because several
  listed fixes already exist. Existing fixes require fresh verification.
- Pre-flight: session modes feed policy, tool backends, scheduler, and transports;
  all must observe durable values before constructing approved execution contexts.
- Pre-flight: approval resume, ownership cancellation, terminal ordering, and the
  versioned protocol share turn IDs and fenced ownership. Resume must create a new
  owner and never restart a consumed approval.
- Pre-flight: external effects cannot be universally exactly-once after a crash;
  document the enforced fencing/idempotency boundary rather than promise impossible
  guarantees for arbitrary programs or providers.
- Ruling: HTTP resume executes the exact approved tool through the shared agent
  tool pipeline and returns a new SSE stream. Clients submit original tool arguments;
  the server reconstructs and compares the full durable canonical proposal, then
  atomically consumes it. This avoids persisting unredacted continuation payloads
  or asking an LLM to regenerate an approved action. Subsequent chat is a new turn.
- Ruling: web extraction uses the plan's direct pinned fetch (Option A); plain
  text is preserved and bounded HTML is converted locally. No target URL is sent
  to a third-party extraction service.
- Task 2 verification: eight new tests RED → GREEN; 37 HTTP/cache tests passed.
  The full run found a cancellation fixture requiring the new start event and a
  skill-cache test rewriting identical bytes; both corrected and rechecked. Full
  release gate will include the newly introduced pinned-extraction regressions.

## Tasks (plan section 11)

1. Durable session execution mode — implemented; six new regressions RED → GREEN;
   complete Python suite: **1,607 passed**, zero skips (98.73s). Schema-upgrade
   verification remains part of task 13.
2. HTTP approval resume — implemented; covered by the 1,632-pass full run.
3. Pinned web extraction — implemented; 15 new regressions RED → GREEN;
   62 extraction/fetch/handoff tests passed. Full-suite failure exposed an existing
   DB/server versus client-clock assertion; corrected to use the database clock.
4. REST ownership-loss cancellation — implemented for cooperating tasks;
   two regressions RED → GREEN and 48 REST/scheduler tests passed. Resistant-work
   retirement and terminal ordering remain part of task 7.
5. Approval timeout accounting — verified: 67 approval/budget/permission tests
   passed, including a simulated 600-second active wait and overlapping waits.
   Canonical regression module: `tests/test_approval_budget_active_wait.py`.
6. Continuous approval-wait renewal — implemented: three new regressions
   RED → GREEN; 10 renewal/budget/cleanup tests passed. Renew every 30 seconds
   (TTL 360 seconds), immediately cancel owned work on failed renewal.
7. Lifecycle-safe terminal completion — implemented. Three resistant-turn
   regressions RED → GREEN; 40 HTTP/gateway/budget tests passed. Gateway terminal
   events follow retirement; HTTP `done` is buffered until release. Resistant REST
   execution retains the claim and provider until it unwinds, reporting cleanup
   pending without false completion. Full suite: 1,639 passed, zero skips.
8. Script dependency trust boundary — in progress.
9. Provider-close visibility — pending.
10. Unified approval protocol — pending.
11. Critical-path deduplication — implemented; final full suite running.
    Shared execution-context resolver freezes mode/backend/workspace; one bounded
    cleanup helper retains resistant tasks. Nine config/secret regressions, two
    workspace regressions, and three diagnostic payload regressions RED → GREEN.
    170 lifecycle/memory checks and 143 observability/transport checks passed.
    Replaced database mocks with proper async connection context managers.
12. Multi-worker/failure/security/smoke verification — pending.
13. Migrations and schema conformance — pending.
14. Documentation and threat model — pending.
15. Full release gate — pending.
16. v1.0.0 — gated on all preceding requirements.

## Environment checks

- Windows Python 3.12.10 and Node installed; dedicated PostgreSQL test database.
- Optional workflow-trial dependencies installed for formerly skipped tests.
- Database suites must run sequentially: overlapping full and focused tests
  caused schema migration lock contention. Recheck the affected suite serially;
  this was a test orchestration error, not evidence of a production defect.
- Full Python verification after tasks 1–4: **1,632 passed**, zero skipped,
  58 existing warnings, 101.75 seconds (`production-task4-suite.log`).
- Additional HTTP regression RED → GREEN: mode changes between validation and
  execution require fresh approval. Resumption authority must be stamped in the
  parent SSE task because successive async-generator advances run in different
  child contexts; a generator-owned ContextVar token was invalid and lost the
  required binding. Positive HTTP tests now explicitly reject error events.
- WSL Ubuntu available. Docker unavailable on the current PATH; investigate before
  deployment verification. No staging or production target has been specified.

- Task 8 ruling: choose Option B (administrator-trusted dependencies). Only
  allowlisted entries outside the agent workspace execute. Declared dependencies
  are hashed and revalidated; dynamic imports, packages, interpreter, undeclared
  resources, and post-check administrator mutations remain trusted. This does
  not claim a complete dependency snapshot or protection from administrators.

- Task 10 ruling: retain v1 when protocolVersion is omitted to preserve existing
  HTTP clients and gateway integrations. The current TUI negotiates v2; HTTP
  clients opt into the identical canonical v2 envelope. Conformance validation
  rejects unknown event names, fields, and top-level types.

- Task 11 ruling: action digest/proposal version 2 also binds workspace identity,
  including absolute actions. Existing version 1 approvals require fresh review.
  No new SQL column is needed: proposals already use JSONB.
- Task 11: corrected live configuration precedence; YAML credentials are ignored.
  Secret set/clear validates before mutation and persists before activating.
  Failed writes retain prior live values. Environment/backend resolution is shared.
- Task 11: durable approval reads now fail closed; optional fallback errors report
  sanitized diagnostic metrics/logs. No broad Exception/pass handlers remain
  outside CLI code. Exception logs retain class/source locations, omit payloads.
