# R4-101 / R4-102 / R4-103(non-locomo) — test-truth lane B evidence (G2)

Scope: R4-101 + R4-102 + the non-locomo half of R4-103 from
`docs/verification/FIX_PLAN_ROUND4_TASKS.md`.
Sole-writer files: `test_rpc_gateway.py`, `test_chaos.py`,
`test_memory_bug_fixes.py`, `test_review_round2_fixes.py`,
`test_scheduler_coverage.py`, `test_property_based.py`,
`test_controller_triage.py`, `test_load_concurrency.py`,
`test_learning_review.py`, `test_handoff_regressions.py`,
`test_api_bugfixes.py`, `test_bug9_bug15_fixes.py`.
(`test_gateway_providers.py` and `test_fm6_locomo.py` untouched.)

Method per item: re-read production seam at HEAD → tighten the test to drive
the real seam → run against current code (RED-first) → GREEN, or document the
real product bug without weakening the test. Boundary mocks limited to
provider/network/DB edges; the seam under test is never mocked.

## Per-file table

| # | File (line) | Item | Seam now driven | Result | Evidence |
|---|---|---|---|---|---|
| 1 | `test_rpc_gateway.py:79` `TestRpcDispatchReuse` | R4-101: old tests constructed `_RpcGateway()` directly and never touched the route | Real `/rpc` route through ASGI: 6 tests — dispatch+reuse through `app.state._rpc_gateway` (one `Gateway` built), redaction of result secrets, `RpcError→HTTP` mapping (400), missing-response→500, prompt-method rejection, 503 without gateway | GREEN 6/6 | full-file run |
| 2 | `test_chaos.py:598` `TestDatabaseChaos` | R4-101: DB-failure tests patched `add_chunk`, which the `run()` path never calls (flushes go through `add_chunks_batch`) | All three tests now patch `add_chunks_batch`; connection-drop asserts the 2nd flush fails at the real seam; slow-db asserts both flushes ran; pool-exhaustion asserts every flush fails | 1 GREEN + **2 RED = real product bug PB-1** | `R4-101_chaos_db_red.txt` |
| 3 | `test_memory_bug_fixes.py:254` `TestRerankTimeout` | R4-101: single candidate never reaches `_rerank` (`len<=1` early-return) and `wait_for` was stubbed to raise | ≥2 candidates; real `asyncio.wait_for` with compressed bound; asserts LLM started, `complete` awaited once, production `timeout=10.0` observed, fallback returns input order | GREEN | `-k` run 15 passed (mine) |
| 4 | `test_memory_bug_fixes.py:376` `TestConsolidatorBatchDedup` | R4-102: asserted `status=="empty"` with zero chunks — extraction/dedup never ran | Real chunk → real `_extract_memories` boundary → both embedded candidates hit `search_by_embedding` (await_count==2); paraphrase deduped + access-bumped, fresh candidate the only write, `status=="complete"` | GREEN | `-k` run 15 passed (mine) |
| 5 | `test_handoff_regressions.py:471` `test_agent_binds_scope_for_rag_tools` | R4-101: set ContextVars in the test body, never invoked the probe (`seen == {}` asserted) | Real agent instance drives real `_execute_tool_calls_stream` with an LLM `scope_probe` call; probe reads the REAL `active_agent_scope()` (only its one DB read stubbed); negative control proves the gate refuses unscoped calls | GREEN + RED-verified (binding disabled → FAIL) | `R4-101_ah018_red.txt`, `R4-101_ah018_green.txt` |
| 6 | `test_scheduler_coverage.py:258` `test_keep_lease_renews_periodically` | R4-101: asserted `renew_lease` NOT awaited (proved the loop never runs) | Compressed `RUN_LEASE_SECONDS=0.3` → 0.1 s cadence observed 0.45 s; asserts `await_count >= 2` with the job id | GREEN | full-file 46 passed |
| 7 | `test_property_based.py:457` `TestAsyncTokenBucketProperties` | R4-101: mutated `bucket.tokens` directly, never called `acquire()` | All three tests call real `acquire()`; debit exactness, immediate within-capacity success, frozen-clock drain-to-zero-then-block (`wait_for` TimeoutError, tokens never negative) | GREEN 6/6 with ContextManager class | full-file 36 passed |
| 8 | `test_property_based.py:632` `TestContextManagerProperties` | R4-101: `test_add_chunk_preserves_type` asserted a literal list; `test_batch_size_respected` asserted a constructor arg | Real `ContextManager.add_chunk` / `add_chunks_batch` with DB-boundary echo fakes (real MessagePack round-trip + row mapping); type/payload/order preservation properties | GREEN | full-file 36 passed |
| 9 | `test_controller_triage.py:269` `test_importing_controller_pulls_no_provider_modules` | R4-101: in-process `sys.modules` diff is vacuous under suite ordering | Fresh-interpreter subprocess import; asserts zero banned modules. **RED-found real product bug PB-2** (`machina/core/__init__.py` eagerly loaded config → memory → provider stack); fixed minimally (see below) | GREEN after fix | `R4-101_bug15_red.txt`, `R4-101_bug15_controller_green.txt` |
| 10 | `test_load_concurrency.py:316` `test_concurrent_context_mixed_read_write` | R4-101: readers read disjoint sessions (could not observe the write path) + phantom cache claim | Readers target the SAME writer sessions; concurrent reads bounded `0..M`; final fresh read asserts exact count M + exact payload set per session | GREEN 10/10 | `R4-101_load_full.txt` |
| 11 | `test_learning_review.py:857` `test_review_cap_still_enforced_after_recovery` | R4-102: learning review disabled by default → `_learning_tasks==0` proved nothing | Enables `learning_review_enabled`; under-cap case proves scheduling works amid stale `reviewing` rows; at-cap case proves the process-wide cap blocks + reviewer never runs | GREEN 36/36 (file) | full-file runs ×3 |
| 12 | `test_api_bugfixes.py:344` `TestRpcGatewayTTL` | R4-103: asserted dict entries persist / poked `_cleanup_old_responses` directly — never drove `gw.call()` | All three drive real `gw.call()` with aged entries: stale swept by next call, 100 unanswered calls leave 1 (not 100), recent entries survive | GREEN 3/3 | full-file 15 passed |
| 13 | `test_bug9_bug15_fixes.py:240` `TestConfigNotLoadedAtImport` | R4-103: reset `_config` in-process then asserted the reset (proves nothing about import) | Fresh-interpreter subprocess asserts `_config is None` after import, loaded + cached after first access. **RED-found real product bug PB-2**; fixed minimally | GREEN after fix | `R4-101_bug15_red.txt`, `R4-101_bug15_controller_green.txt` |
| 14 | `test_review_round2_fixes.py:53` `test_connect_without_dsn_uses_default` | R4-101/R4-102: never called `connect()` (asserted only `dsn != ""`) | Awaits real `connect()` with only `asyncpg.create_pool` mocked; asserts fallback DSN value, `connected`, pool created with that DSN, `close()` disconnects | GREEN | class run 3 passed |

## RED captures

- `R4-101_bug15_red.txt` — subprocess lazy-config test fails pre-fix:
  `assert True is False` (`loaded_at_import`), i.e. importing
  `machina.core.config` (via `machina/core/__init__.py`'s eager
  `from ... import config`) ran `Config.load()` at import time.
- `R4-101_bug15_controller_green.txt` — post-fix: bug-15 subprocess test +
  controller subprocess purity test pass.
- `R4-101_ah018_red.txt` — AH-018 test with the production scope binding
  temporarily disabled (`token/agent_token = None`): FAILS. Reverted;
  `machina/core/agent.py` verified clean (`git status` empty, no
  `RED-PROBE` remnants); `R4-101_ah018_green.txt` records the passing rerun.
- `R4-101_chaos_db_red.txt` — the two DB-failure tests against the real seam:
  `Exception: Connection lost` / `Exception: Pool exhausted` escape
  `agent.run()` (via `_flush_pending_chunks`, `agent.py:1602` and `:1693`
  through `:1327-1332`). This is product bug PB-1 (below), not a test bug.
- `R4-101_load_full.txt` — full load file: 10 passed.
- `R4-101_intrabatch_deadlock.txt` — recorded during triage of the
  other lane's `test_intra_batch_paraphrases_write_once`; see residual R-1.

## Product bugs

### PB-2 — FIXED (minimal, unowned-file check passed): eager config load pulls the provider stack
Root cause: `machina/core/__init__.py` did
`from machina.core.config import Config, config`; the module `__getattr__`
in `config.py` runs `Config.load()` on first attribute read, and loading
validates `persona_description`, which imports `machina.memory.redaction`
→ the memory package → `machina.core.provider` (whole provider stack).
So ANY `import machina.core.*` loaded config and the provider tree.
Fix (`machina/core/__init__.py` only, +19/−1): import `Config` alone and
re-export `config` through a module-level `__getattr__` returning
`get_config()` — `from machina.core import config` keeps working, nothing
loads until first use. No other lane owns this file (last touch `4121c97`,
unrelated). Verified: subprocess import-time `_config is None`; post-access
caching intact; `test_config_import_cycle/reentrancy/precedence`,
`test_leaf_independence`, `test_fm3_soulspec_import`, `test_r4_080_boot_config`
(78 passed + 1 pre-existing failure, see R-2), `test_architecture_layers.py`
25 passed, ruff clean.

### PB-1 — REPORTED (not fixed: file owned by another lane): `agent.run()` lets flush DB errors escape
Root cause: `ReActAgent.run()` awaits `self._flush_pending_chunks(...)`
unguarded (`agent.py:1602` post-context, `:1693` post-answer, plus the
budget/LLM-error exits); `_flush_pending_chunks` (`:1327-1332`) awaits
`context_manager.add_chunks_batch()` directly. Any DB failure during a
flush propagates out of `agent.run()` to the transport, contradicting the
tests' documented contract ("handle DB errors gracefully (log and
continue)"). The failure is only observable now that the tests patch the
real seam (`add_chunks_batch`); the old `add_chunk` patch never fired.
`core/agent.py` is owned by the agent-chain lanes (R4-010/019/022/025 per
the collision groups), so per lane rules I report instead of fixing.
Failing tests (honest RED, kept as-is, never weakened):
`test_chaos.py::TestDatabaseChaos::test_agent_handles_db_connection_drop`,
`...::test_agent_handles_db_pool_exhaustion`. Suggested minimal fix for the
owning lane: wrap the `run()`-level `_flush_pending_chunks` calls in
try/except that logs a warning and continues (flush loss is already
possible on cancellation paths; the turn answer itself does not depend on
the flush), or consciously change the contract to fail-fast and update the
two tests. Note the design tension: silent history loss vs. killed turns.

## Residuals / notes for the coordinator

- R-1: `test_memory_bug_fixes.py::TestConsolidatorDedupEmbedding::test_intra_batch_paraphrases_write_once`
  (other lane's R4-089 test, committed in `22f4bc2` together with my items
  #3/#4 above) is contention-flaky against the shared `machina_test` DB:
  failed 4× with `DeadlockDetectedError` (slow 10–16 s runs) during
  concurrent-lane activity, then passed 6/6 in isolation (~0.4 s) plus the
  full-file run. Timing-sensitive; needs the owning lane to harden
  (serial lock ordering or retry), not a G2 item.
- R-2: `test_r4_080_boot_config.py::test_overlapping_first_connects_create_exactly_one_pool`
  fails at HEAD (`backends == 1` assertion, line 137) — pre-existing,
  unrelated to G2 (file is untracked/another lane's; `machina/db/connection.py`
  owned elsewhere). Recorded, not touched.
- R-3: one transient full-file run of `test_learning_review.py` showed 2
  setup/teardown ERRORs under DB contention; three subsequent full-file runs:
  36 passed each. Attributed to shared-DB contention with concurrent lanes.
- The load suite runs fast (~1.5 s full file) on local Postgres; the
  mixed read/write test is non-vacuous by construction (exact payload-set
  equality over 12 writer sessions × 10 chunks against the real DB).
- No `test_gateway_providers.py` / `test_fm6_locomo.py` contact. No commit.
  `machina/core/agent.py` was patched twice transiently for RED probes and
  byte-verified reverted both times.

## Verification commands (all with
`MACHINA_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/machina_test`)

- Each touched file alone: rpc_gateway 6 ✓ · review_round2 32 ✓ ·
  controller_triage + bug9_bug15 72 ✓ ·
  property_based 36 ✓ · chaos 24 passed + 2 RED (PB-1) · memory_bug_fixes:
  my classes 15 ✓ (file: 17 passed + R-1 flake) · scheduler_coverage 46 ✓ ·
  api_bugfixes 15 ✓ · handoff_regressions 39 ✓ · learning_review 36 ✓ ×3 ·
  load_concurrency 10 ✓ ·
- `tests/test_architecture_layers.py`: 25 passed.
- `ruff check` + `ruff format --check` on all 12 test files +
  `machina/core/__init__.py`: clean (two format fixes applied to
  `test_handoff_regressions.py`, `test_property_based.py`; re-verified
  green after).
