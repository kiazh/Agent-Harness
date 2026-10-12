# Deep debugging report

Current release evidence and open gates are in [RELEASE_READINESS.md](docs/verification/RELEASE_READINESS.md). Counts below belong to their dated engineering snapshots.

Date: 2026-10-09. Reviewed against the current working tree; no commit or deployment performed.

## Scope and method

Used the Superpowers systematic-debugging, test-driven-development, parallel investigation,
code-review, and verification workflows. Reviewed critical execution paths line by line:
provider retries/streaming/accounting, gateway and HTTP turn ownership, permissions and
scheduled scripts, scheduler leases, runtime shutdown, audit persistence, session caching,
memory consolidation, and RAG indexing/retrieval. Static checks covered all 120 Python
source modules; UI checks covered the TypeScript terminal application.

This is a substantial debugging pass, not a claim that every repository line was manually
reviewed or that every possible defect has been eliminated. Full test execution and
coverage complement the focused source review.

The initial Python run had **10 failures, 1,487 passes, and 4 skips**. Initial UI checks
passed 87 tests but skipped the three real-gateway integration tests. Reproductions used
events, controlled clocks, HTTPX mock transports, local socket pairs, and the dedicated
test database. New behavioral regressions were observed failing before their fixes.

## Confirmed defects and corrections

| Area | Root cause and correction | Regression evidence |
| --- | --- | --- |
| Shutdown and provider cleanup | `wait_for` can wait indefinitely for cancellation acknowledgement. Bounded observation now uses `asyncio.wait`, cancels owned leftovers, and consumes eventual errors. Cancelled shutdown still attempts remaining cleanup before propagating cancellation, preserving audit ownership across restart. | `test_deep_runtime_cleanup.py`, `test_deep_runtime_shutdown.py` |
| HTTP and gateway completion | Optional learning tasks could retain a completed answer indefinitely. Completion now has a bounded learning wait and releases the turn claim before the terminal transport marker. | `test_deep_http_cleanup.py`, `test_deep_gateway_cleanup.py` |
| Gateway construction | The explicitly supplied factory was ignored, which also defeated fake-provider isolation. Explicit factories now receive stored session model/provider settings; default construction still uses the canonical session-aware factory. | `test_deep_gateway_cleanup.py` |
| Gateway ownership | A retiring turn read the replacement turn's token during cleanup. It now captures its own token and only removes its own task/token registration. | `test_deep_gateway_cleanup.py` |
| Approval timeout accounting | Active human waits counted toward compute timeout; overlapping waits could be counted twice. Active and completed waits now contribute one paused interval. | `test_deep_gateway_approval_budget.py` |
| Audit writer lifecycle | Old completion callbacks cleared replacement writers; retiring writers read a replacement queue. Writers now own their queue and clear registrations only by task identity. | `test_audit_lifecycle.py` |
| Session cache | Shallow copies shared nested authority state; insertion paths also retained caller-owned objects. The cache now owns and returns deep snapshots. | `test_deep_session_cache.py` |
| Provider fallback and rate limits | Streaming 404 fallback indexed a nonexistent candidate, final plain-chat fallback was unreachable, streaming 429 retry/quota handling diverged from completion, and errors could leave usage reservations unfinished. Fallbacks preserve the actual failure and finalize failed reservations. | `test_deep_provider_fallback.py` |
| Consolidation scheduling | An agent-wide task gate prevented independent sessions from consolidating. Duplicate protection remains per session. | `test_bug9_bug15_fixes.py` |
| Scheduled approvals | Script failures/cancellation were marked completed, and structured approval IDs were discarded. Outcomes now retain their actual terminal state and jobs park against the precise approval request. | `test_deep_scheduler.py` |
| Scheduled deadlines | The runner awaited cancellation-resistant execution indefinitely after timeout, lease loss, or shutdown. It now observes a bounded grace, retains active execution and its lease, processes other jobs, and commits a fenced outcome only after the old execution actually unwinds. | `test_deep_job_deadline.py` |
| Approved script contents | Execution reopened a mutable script after approval. Scheduler requests now bind exact source bytes and execution uses a private approved snapshot, retaining path checks, working directory, output caps, and process-tree cleanup. | `test_deep_job_script_snapshot.py` |
| Python script identity and output | Snapshot execution preserves original `__file__`, `sys.argv`, sibling imports, main-module identity, pickling, coding cookies, and BOMs. Python pipe output is explicitly UTF-8 to match decoding on Windows. | `test_deep_job_script_snapshot.py` |
| RAG cache | Cache keys omitted retrieval scope, and result objects/payloads remained mutable across calls. Keys now include scope and the cache owns deep snapshots. | `test_deep_rag_scope.py` |
| Embedding compatibility | A 25-row document-only sample missed mixed embedding spaces and custom scopes; database/metadata failures silently bypassed validation. Scoped keyset pages validate all stored vector metadata and fail closed. | `test_deep_rag_scope.py` |
| Embedding backfill | Vector backfills did not update model metadata, causing recovered documents to fail the stronger guard. Vector, text, model, dimensions, and payload metadata now update together. | `test_deep_rag_backfill.py` |
| Outbound fetch | Buffered reads could exceed the advertised overall deadline under a slow drip; connect failures leaked descriptors; TLS inherited a stale pre-connect timeout. Incremental receives use remaining time, TLS refreshes its budget, and an outer `finally` owns socket cleanup. | `test_deep_fetch_deadline.py` |

## Test and tooling corrections

- Test credential isolation now blanks config fields, environment aliases, mounted-secret
  file aliases, and external secret backends. Tests explicitly supply their fake credentials.
  The initial baseline exposed incomplete isolation and attempted real provider requests;
  subsequent verification uses isolated credentials and controlled provider boundaries.
- Updated stale fixtures to use current fenced lease signatures, stable tool-call IDs,
  explicit workspace write permission, and a mocked RAG database boundary.
- The paused-job integration probe now uses the database clock, matching production;
  comparing a client timestamp with a server timestamp caused a reproducible false failure.
- Real UI gateway tests now provide matching authentication tokens and search their unique
  memory tag, avoiding interference from older rows in a reused test database.
- Fixed import ordering, duplicate logger initialization, and source formatting failures.
- Reviewed all 21 medium Bandit findings individually. Twenty interpolate fixed SQL
  fragments while binding external values; one describes Docker's private `/tmp` tmpfs.
  Added line-local, rule-specific explanations rather than disabling scanner rules globally.

## Verification

Final checks passed after integrating the behavioral fixes:

| Check | Observed result |
| --- | --- |
| Full Python suite | **1,597 passed, 4 skipped**, 58 warnings; 111.75 seconds |
| Python coverage | **77.80%**, above the repository's 60% gate |
| UI standard check | TypeScript typecheck, lint, and **87 tests passed**; integration cases run separately |
| Real UI/gateway/database integration | **3 passed**, 0 skipped, using the dedicated test database |
| Python lint | All source modules and newly added/adjusted regression files passed |
| Python source formatting | All **120 source files** passed; new deep regression files also checked |
| Bandit CI gate | Passed; 0 unsuppressed medium/high findings, 132 low findings remain |
| Whitespace | `git diff --check` passed |

The final collection contains 100 more test cases than the initial baseline. A final
focused check of the scheduler, script, runtime, and backfill fixes passed 87 tests;
the audit fixture import cleanup was separately rechecked with 33 passing tests.

Logs and XML coverage are retained in `.deep-debug-results-tmp/` (ignored scratch directory):
`completed-python.log`, `coverage.xml`, `ui-final.log`, `ui-e2e-final.log`,
`security-final.log`, and `last-focused-green.log`.
Useful commands:

```powershell
.venv/Scripts/ruff.exe check ah/
.venv/Scripts/ruff.exe format --check ah/
.venv/Scripts/python.exe -m bandit -r ah/ -ll -x ah/cli/
.venv/Scripts/python.exe -m pytest tests/ -q --tb=short --cov=ah --cov-fail-under=60
npm --prefix ui run check
git diff --check
```

## Limits and remaining considerations

- Four optional workflow-trial tests require additional dependencies and remain skipped.
  Live hosted-provider behavior and a Linux CI runner were not exercised in this Windows
  workspace; provider error paths were tested through controlled transports.
- RAG metadata validation and retrieval use separate database snapshots. A concurrent
  cross-process reindex can change candidates between them; transactional embedding-space
  coordination would require a further design change. Full metadata validation also adds
  work proportional to the selected index size on uncached dense queries.
- Python tasks that suppress cancellation cannot be forcibly terminated safely. Bounded
  shutdown quarantines them; deadline completion does not prove their effects have stopped.
  Scheduled workers keep their existing leases while they unwind, except after confirmed
  ownership loss. Process exit or persistent database failure still requires crash recovery
  and idempotent effects.
- Script snapshots pin the reviewed entry script, not its imported dependencies. Bash's
  source-file introspection observes the snapshot path; Linux/bash execution was not
  separately exercised on this Windows host.
- Existing test warnings include deprecated `datetime.utcnow()` calls and incorrectly
  shaped database mocks that produce unawaited-coroutine warnings. They do not represent
  passing coverage of those unexecuted coroutine paths.
- Bandit's configured medium/high gate passes after the explained false positives;
  low-severity findings remain. A passing scanner is not a general security guarantee.

## Production-readiness pass (2026-10-12)

Handoff items P1 1–13 (correctness) + P2 14–18 (verification). Method: reproduce each
bug against current source (shell reproduction for config empty-env/unknown-key;
code-path analysis for the rest), write the regression test, observe it fail on the
unfixed code (verified RED by reverting each fix: P1-01/02/03/04/06/07/09/11/13 and
P2-17/18), then fix and observe GREEN. No commits performed.

Notable finds during the pass (fixed, not just tested):

- `maybe_auto_compact` had two unguarded escapes beyond the reported silent
  `except: return None`: a `begin_mutation` failure raised out of the function
  (unhandled task exception on the fire-and-forget auto-compact path), and the
  `status_summary` base dict called `config.get` outside any guard, so a total
  config failure crashed the status endpoint instead of degrading. Both now go
  through `record_failure` with conservative fallbacks.
- `get_recent_context` was the only context query without the `(created_at, id)`
  tiebreak; fixed to match.
- `compress_session` conflated five outcomes into `None`; added
  `compress_session_with_status` returning typed `CompressOutcome` while the
  legacy wrapper keeps its `result/None`-or-raise contract for existing callers.
- Document counting had no stable identity at all (distinct serialized payloads);
  added `context_chunks.document_source`, persisted by the RAG pipeline and both
  batch/single insert paths, counted as `COUNT(DISTINCT COALESCE(document_source,
  id::text))`.
- `JsonFormatter` serialized arbitrary extra attributes (prompts, completions,
  tool args/results, credentials could enter structured logs); now allowlisted,
  bounded, redacted, with numerics preserved.

Remaining open (P3 gates 19–24, not verifiable in this workspace): staging
deployment, backup/restore drill, alert delivery, dependency lockfile + release
artifact, production usage budgets, MCP scope decision. See
`docs/plans/PRODUCTION_COMPLETION_PROGRESS.md`. The v1.0.0 gate stays open.

## Full look-through (2026-10-12, user-requested)

Method: gateway-log triage of a live turn failure → import-order reproduction
matrix → traceback capture (`sys.exception()` inside the logging handler) →
cycle mapping via package-init analysis → fix → subprocess regression tests in
three import orders → full-suite verification. Plus a static sweep (two
subagents: silent `except` handlers, SQL columns vs schema) with targeted
triage of Tier-1 hits, and diagnosis of the one pre-existing suite failure.

### Confirmed defects and corrections

| Area | Root cause and correction | Regression evidence |
| --- | --- | --- |
| Config silently discarded (import cycle) | First-`ah.db.connection` import order ran `Config.load` while `connection` was partial: `config.load → validate(persona) → ah.memory init → approval → provider → usage → from ah.db.connection import db` → `ImportError`, and the all-or-nothing loader discarded all 56 keys (wrong model/provider/mode downstream). Nine cycle members now use the import-safe `ah.db.lazy` proxy (`LazyDb` + moved `parse_command_count`, re-exported); `Config.load` isolates per key. | `tests/test_config_import_cycle.py` (5 tests; db-first, per-key, and delegation tests verified RED on old code) |
| Turn capacity precedence | `_turn_model` preferred provider over session, contradicting the spec test (failed on clean HEAD). Session scope now wins; provider/config back it up. | `test_context_windows.py` (now green) |
| Permission-store silence | `_db()` fallback logged nothing; unparseable `expires_at` authorized. Fallback now `record_failure`s; garbage expiry fails closed (matches `policy._grant_usable`); NULL stays live. | `tests/test_debug_permissions_hardening.py` (6 tests; 3 RED on old code) |
| Live-turn outage during P1 series | New `document_source` column shipped in `schema.sql` but never migrated on the live DB (gateway does not self-migrate); first turn crashed with `UndefinedColumnError`. Migrated prod manually, smoke-tested batch insert/delete, and documented the `ah init`-before-workers requirement in OPERATIONS.md. Boot behavior intentionally unchanged. | gateway log turn `58c3ee9f`, smoke test |
| Stale invariant test | `TestCircularImport` asserted the top-level connection import that caused the cycle; updated to the lazy invariant. | updated test green |

### Triaged and left alone (with reason)

- `turns.py::_live_claim` expiry-parse fail-open (`True`): the safe direction
  for a lock (never steal a possibly-live claim); DB timestamps never fail
  parsing in production.
- `session.py::update_state_fields` corrupt-blob fallback (`{}`): requires a
  corrupt msgpack blob; row lock already excludes concurrent writers.
- ~30 remaining silent handlers in the sweep: documented fail-closed/fail-open
  fallbacks on display, timeout, and mock-compat paths; no authorization or
  ownership bypass found among them. `turns.py`/`scheduler.py` fencing checks
  fail closed throughout.
- SQL-vs-schema sweep: zero referenced columns missing; explicit projections
  raise loudly on un-migrated DBs by design (fail fast, as observed).

### Verification snapshot

- Backend: final full run **2184 passed, 5 skipped, 0 failed** (dedicated
  test DB, `not load`); the pre-existing `test_context_windows` failure from
  the earlier run is fixed by D-02.
- UI `npm --prefix ui run check`: 251 passed / 0 failed / 5 skipped; eslint clean.
- Ruff check + format, Bandit medium/high (no new suppressions), `git diff --check` clean.
- No commits performed. P3 gates (staging, backup/restore, alerts, lockfile +
  artifact, budgets, MCP scope) remain open; v1.0.0 stays gated.
