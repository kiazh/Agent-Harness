# Current Issues and Release Checkpoint

Date: 2026-10-12  
Checkout: `main` at `566cf63` (`mcp`)  
Purpose: handoff of the current OAuth, MCP, image attachment, and release-readiness review.

This is a working checkpoint, not release approval. The working tree contains uncommitted changes from the current review. Do not treat an implemented fix or a focused passing test as proof that the full release gates pass.

## Confirmed issues and current status

### OAuth and MCP security

| Issue | Evidence and impact | Current fix status |
|---|---|---|
| OAuth DNS resolution could block the event loop and delay process shutdown after timeout. The HTTP transport used executor-backed DNS; OAuth also performed a synchronous precheck outside its request deadline. | Astra reproduced both with synthetic resolvers: a 150 ms resolver blocked a 10 ms request and a concurrent heartbeat; a cancelled executor resolver delayed `asyncio.run()` shutdown. | A bounded daemon-thread DNS helper is in `machina/connectors/http_transport.py`; OAuth and MCP prechecks and connection pinning now use it. Worker count is bounded and releases capacity on completion. Focused DNS tests passed, but the final complete suite is pending. |
| Custom pinned HTTP transport could honor hostile `SSL_CERT_FILE` or `SSL_CERT_DIR` despite `trust_env=False`. | Synthetic invalid CA path caused HTTPX2 context creation to fail; the default previously ignored that environment under `trust_env=False`. | Transport now creates its context with `http_module.create_ssl_context(verify=True, trust_env=False)`. A regression covers both supported HTTP libraries; full verification pending. |
| DNS rebinding could change the destination after policy validation, or allow a mixed public/private answer set to send credentials to an unsafe IP. | Tests exercise the actual HTTP serialization path with a fake network backend, preserving URL hostname for `Host`/TLS SNI while dialing validated numeric IPs. Additional checks cover mixed answers and literal/alias loopback consent. | OAuth/MCP use a shared pinned transport and fail closed on unsafe answers. Focused tests passed; complete verification pending. |
| OAuth save/forget leaked a file descriptor when index-lock acquisition timed out. | Astra reproduced two fake lock timeouts and observed two descriptors remain open. | `oauth_store.py` now closes the descriptor when acquisition fails. Four regression cases were added; focused verification passed, complete verification pending. |
| Concurrent grant rotation could make a valid credential load fail after it read an old index snapshot and the old envelope was removed. | Deterministic two-store reproduction returned `ValueError` during rotation although a valid new grant existed. | A bounded three-attempt retry handles only a missing envelope paired with a changed index; unchanged missing files and corruption still fail. Two regressions passed; complete verification pending. |
| A rotated OAuth bearer token could be sent without registering it with the MCP connection redactor. | Astra's fake coordinator returned an initial token and then a rotated token; an echoed rotated value survived redaction. | Auth now registers each token before sending the request. A regression was added and passed in focused verification. |
| MCP discovery could expose a credential echoed in a tool name or input schema. | Fake server reproduced raw credential material in `tools_describe`; rewriting schema fields could alter dispatch validation. | Discovery now rejects credential-bearing names or schemas rather than modifying the dispatch contract. Regression tests passed in focused verification. |

### Image attachment context and lifecycle

| Issue | Evidence and impact | Current fix status |
|---|---|---|
| Image context could be lost after tool-result churn or history compaction. | Latest-image bytes stored only in ordinary chunks could be evicted with conversation history. | A separate `session_image_context` cache, keyed by session and agent, is implemented with atomic updates and runtime schema migration. Tests cover tool churn, compaction, resume, replacement, isolation, batch failure, and validation. Focused suites passed; final complete verification pending. |
| Valid maximum-size image sets with long Unicode filenames exceeded the persistence metadata allowance. | Astra packed five valid images totaling 20 MiB with 255 astral Unicode characters per filename; the payload exceeded the old 4 KiB overhead cap. | The cache limit was raised to 20 MiB plus 8 KiB, and the database constraint migration was added. Regression tests were added and passed in a focused run; complete verification pending. |
| Existing installations with image-bearing history had no cache row after the new table was introduced, so follow-up turns could lose those images. | The previous cache lookup returned no images when the new table had no row; older image-bearing chunks remained in history. | VERIFIED 2026-10-12: legacy fallback now seeds the durable cache (`ON CONFLICT DO NOTHING`, never clobbering newer writes). Regression reads an upgraded image-bearing session, compacts away legacy chunks, and proves a later turn still receives the original bytes. Scan is capped (50 rows), skips corrupt rows, uses the epoch-floor cursor, and negative-caches exhausted sessions (invalidated on image writes). |
| A corrupt `session_image_context` row failed every follow-up turn with no recovery path. | Cache-row validation raised `ValueError` on every imageless turn via an unguarded call in `_prepare_context`; cache rows are never evicted. | VERIFIED 2026-10-12: corrupt rows are logged, quarantined (deleted), and served from legacy chunks instead. Regressions cover corrupt-with-legacy recovery, corrupt-without-legacy empty result, and steady-state reseeding. |
| Every agent turn failed on existing installations (including text-only turns) because `session_image_context` existed only in `schema.sql`, which `initialize_runtime_schema()` never applies. | Reported 2026-10-12 with screenshot (`agent turn failed` on image and text turns). | VERIFIED 2026-10-12: table moved to `machina/db/image_context_schema.sql`, applied by both `initialize_schema` and `initialize_runtime_schema`; reads degrade and writes fall back with warnings. Regressions: unmigrated-DB turns complete, migration restores round-trip, text preserved without the table. |
| ECC review agents (silent-failure-hunter, database-reviewer, security-reviewer, python-reviewer via OpenCode multiagent orchestration against the installed `ecc-universal@2.2.3` plugin) returned 1 HIGH, 4 MEDIUM, plus logging findings. | Independent read-only review of the working-tree diff. | VERIFIED 2026-10-12: all addressed — corrupt-row self-heal above; credential-echo refusal message no longer masked by the generic discovery handler; `delete_chunks` also clears the image cache; DNS futures cancelled/consumed on timeout; OAuth destination errors carry host+stage; seed/fallback logs carry session/agent IDs with tracebacks; legacy relation check matches the table name; SQL CHECK bound pinned to the application constant by test. Tracked but unchanged (pre-existing, outside the diff): plaintext-`http` MCP endpoints can carry bearer tokens; global 32-slot DNS semaphore blast radius; zero-timeout classification path; no `(session_id, agent_id)` covering index. |
| A gateway test helper could reuse a previous completion and falsely satisfy a later-turn assertion. | Astra found the helper selected an earlier completion instead of matching the requested turn. | Helper now matches `turnId`. Focused test passed; full verification pending. |
| Every agent turn failed on existing installations after the image-cache table was introduced, including text-only turns. | `session_image_context` was added to `schema.sql`, which `initialize_runtime_schema()` never applies; `get_latest_images()` is called on every text-only turn, so the missing table raised on all turns. | VERIFIED 2026-10-12: table moved to `machina/db/image_context_schema.sql`, applied by both `initialize_schema` and `initialize_runtime_schema`; reads degrade to no images and writes fall back to plain chunks with warnings. Regressions: unmigrated-DB image+text turns complete, migration restores round-trip, writes preserve text without the table. |
| A test simulating Windows by globally changing `os.name` caused pytest itself to instantiate `WindowsPath` on Linux when the assertion failed. | Latest hosted CI run failed at `tests/test_production_readiness_p2.py::test_broken_pipe_is_startup_failure` with a pytest `WindowsPath` internal error that masked the underlying assertion. | Test now uses a module-local OS identity rather than patching global `os.name`. Hosted CI has not yet been rerun on the changed tree. |

### Image input and provider behavior

The implementation under review accepts bounded PNG/JPEG/GIF/WebP attachments through drag/drop and clipboard paste, displays numbered `[image N]` markers, and sends actual image bytes to supported provider adapters. It validates attachments before claiming a turn, persists user attachments, preserves follow-up image context, and avoids sending image kwargs to legacy text-only agents when no images are attached.

Terminal clipboard behavior depends on the terminal: macOS `Cmd+V` works when the terminal forwards the key; `Ctrl+V` is the native clipboard fallback. This platform behavior still needs a user-facing smoke test on the intended terminal.

## Verification evidence so far

These are focused, intermediate results from the current dirty tree. They are not one combined full-suite run:

- 55 focused tests passed on Python 3.14 during the initial implementation pass.
- 45 gateway-image and production-readiness tests passed on Python 3.11.
- 62 DNS, OAuth store/refresh, execution, and deadline tests passed on Python 3.11.
- The combined client/store/DNS run first had one new test failure due to a missing `json` import; that test was corrected. The subsequent three rotated-token/catalog regressions passed on Python 3.11.
- Ruff check passed for `machina`, `tools`, and `tests`; Ruff format check passed for 350 files.
- GitHub Actions for committed `566cf63` reported UI and Docker jobs passing. The backend job failed in the Windows-simulation test described above. No hosted run has checked the current uncommitted changes.
- No final full backend suite, full integrated UI check, Bandit scan, lockfile check, wheel/sdist build, artifact validation, or post-fix hosted CI result is available yet.

## Verified results 2026-10-12 (this tree, uncommitted)

- Focused OAuth/MCP/DNS/image/ECC suites: **273 passed**.
- Full backend suite (isolated runner, `machina_test` only, serial): **2478 passed, 5 skipped, 10 deselected, 82.12% coverage** (threshold 60).
- UI `npm run check` with real gateway integration (`MACHINA_TEST_PYTHON` + test DB, isolated HOME): **326/326 pass, 0 fail, 0 skipped**; typecheck and eslint clean.
- Ruff check + format clean (347 files); `git diff --check` clean; `uv lock --check --offline` clean; Bandit 0 medium/high severity.
- Wheel + sdist rebuilt to a fresh temp dir and artifact-checked; `image_context_schema.sql` present in both; isolated wheel import smoke passed.
- Still open: hosted CI rerun (blocked, no push) and Astra review (blocked; ECC review agents substituted, findings actioned above).

## Remaining work before release consideration

1. ~~Fix legacy image fallback to atomically seed the durable cache~~ DONE and verified (see table above).
2. ~~Finish focused OAuth/MCP regressions~~ DONE: DNS capacity recovery, cancellation/shutdown bounds, rotated token redaction, and credential-bearing catalog rejection regressions are all present and green in the focused runs below.
3. Full backend suite serially against only `machina_test`, with coverage at the repository threshold. VERIFIED 2026-10-12: **2478 passed, 5 skipped, 10 deselected, 82.12% coverage** (threshold 60), isolated runner, test DB only. Run supported Python 3.11 checks as well as local development checks.
4. UI typecheck/tests/lint with the real gateway integration tests enabled and the dedicated test database. VERIFIED 2026-10-12: **326/326 pass, 0 fail, 0 skipped** with `MACHINA_TEST_PYTHON` + test DB in an isolated HOME.
5. Ruff check/format, Bandit, `uv lock --check --offline`, and `git diff --check`. VERIFIED 2026-10-12: all clean; Bandit 0 medium/high severity (94 lows, same idioms as baseline).
6. Build wheel and sdist into a fresh temporary directory, run `tools/check_release_artifacts.py`, and verify the new schema is present in the packaged distribution. VERIFIED 2026-10-12: both artifacts verified; `image_context_schema.sql` confirmed in wheel and sdist; isolated wheel import smoke passed. Installed-package/CLI smoke checks beyond the import smoke remain open.
7. Rerun GitHub Actions on the final candidate and inspect every job. BLOCKED: no push per safety boundaries; hosted CI has not run on the current tree. Docker was available in hosted CI but is not installed on this workstation.
8. Have Astra review the final diff and exact verification evidence. BLOCKED (Astra unavailable); substituted with four ECC review agents (silent-failure-hunter, database-reviewer, security-reviewer, python-reviewer) run read-only against the diff — findings above. Keep all live-provider, staging, migration/rollback, monitoring, alerting, and backup/restore evidence distinct from fixture results.

## Production status and release boundary

The repository is version `0.2.0`. GitHub Actions runs CI on `main` and pull requests; it builds and uploads CI artifacts but has no production deployment or package-publishing workflow. The installer consumes the `main` source checkout. GitHub API access confirmed `main` points to `566cf63`; the configured SSH remote could not be read with the available SSH key.

The operator has not specified a production server, staging environment, provider provisioning, monitoring/alert route, or verified backup/restore target. `OPERATIONS.md` identifies these as v1.0.0 gates. No commit, push, tag, publication, or deployment has been performed. Do not call this production-ready or publish v1.0.0 based on the current focused tests.

## Safety boundaries for whoever continues

- Preserve the current dirty tree and review each diff before editing.
- Use only `postgresql://postgres:postgres@localhost:5432/machina_test` for tests. Never touch the live `machina` database.
- Do not access live credentials, operator keyring, shell startup files, or operator clipboard.
- Do not commit, push, tag, publish, or deploy until the full checks pass and the production destination/release action is clear.
- Update this checkpoint with verified outcomes as work continues; do not replace pending evidence with assumptions.
