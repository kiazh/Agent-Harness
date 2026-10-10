# Deep debugging report

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
