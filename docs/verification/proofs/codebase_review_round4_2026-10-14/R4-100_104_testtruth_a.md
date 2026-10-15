# R4-100 + R4-104 (test-file half) — test-truth A evidence (lane R4-G1)

Date: 2026-10-14 (resumed 2026-10-15) · Scope: test files only, no production
edits, no commit. Mandate: each tightened test run against current code; where
a real assertion would fail, report the product bug instead of weakening.

Raw logs/probes: `R4-100_104_logs/` in this directory.

## R4-100 — per-file table

| # | File:line | Before | After | Result vs current code |
|---|---|---|---|---|
| 1 | `tests/test_repo_install.py:236` | `"reference" in registry.get_skill_content("alpha") or True` (tautology) | `get_skill_content("alpha") == "Help with alpha tasks."` + `ref.txt` round-trips through staging/activation | GREEN. Probe verified this is falsifiable (item 1 leaks `None` → FAIL). |
| 2 | `tests/test_integration_permissions.py:91` | `"passthrough" in IdentityReranker.__doc__.lower() or True` (docstring, tautology) | `IdentityReranker.backend == "passthrough"` (the real `fallback`/`status` contract seam in `reranker.py:208,444`) | GREEN. Probe mutates `backend` → FAIL. |
| 3 | `tests/test_gateway_providers.py:77` | `"ultra" not in luna["advertisedEfforts"] or True` | Same predicate, `or True` removed | GREEN (luna advertises low…max, no ultra). Probe adds `ultra` to luna → FAIL. |
| 4 | `tests/test_chaos_extended.py:792` | `assert evicted >= 0` | `assert evicted == 2` (12 chunks − newest-10 floor; batch archive fails once, both survive via individual retry) | GREEN; value confirmed by direct probe run against real `evict_old_chunks` (returned 2). Probe replaces the method with a `return 0` regression → FAIL. |
| 5 | `tests/test_code_review_fixes.py:185` (`over_token_limit`) | `assert result > 0` | `assert result == 2` (12×100 tokens; newest-10 floor caps the evictable set at 2 even though 700 tokens requested) | GREEN; value confirmed by direct probe run (returned 2). |
| 6 | `tests/test_code_review_fixes.py:216` (`over_chunk_limit`) | `assert result > 0` | `assert result == 5` (15 chunks − 10 preserved, all evicted) | GREEN; confirmed by direct probe run (returned 5). |
| 7–12 | `tests/test_memory.py` retriever/consolidator ×8 (`716,747,768,789,~844,939,970,988`) | `isinstance(results, list)`-only | Deterministic content: `[(content, source)]` pairs, `results == []` for empty/invalid/empty-response, `batch_update_access` awaited once with `[entry.id]`, SQL param assertions for agent/category filter, `(content, category)` for consolidate | All GREEN. Mocks now inject at `_row_to_entry` / `db.fetch` so the real `_keyword_search` + `_merge_results` seams execute. Probe replaces `retrieve` with `return []` → FAIL. |

Notes on memory-test facts (verified by probe runs against current code):
- Keyword-only retrieval yields `("…", "sparse")` with score `1.0 × SPARSE_WEIGHT
  (0.3)`; hybrid orders dense (`0.9 × 0.7 = 0.63`) before sparse (0.3).
- `consolidate_from_text` with the fixture LLM yields exactly one entry
  `("User likes Python", "preference")`.
- The `test_retrieve_updates_access` mock previously also failed to isolate: it
  asserted `isinstance(mock_store.update_access, AsyncMock)` while the code
  calls `batch_update_access`. New assertion pins the real call.

**Product bugs found: none.** Every tightened assertion passes against current
code, so no strict-xfail was needed. No production change was required by any
item (lane forbids them; none was needed).

### Discrimination proof (RED-first on the tightened assertions)

- `falsify_red.log`: 5 targeted violations (registry returns `None`, reranker
  `backend="unknown"`, luna advertises `ultra`, eviction returns 0,
  retriever returns `[]`) → **all 5 tightened tests FAIL**.
- `falsify_green.log`: same 5 nodes unpatched → **5 passed**.
- Mechanism: `R4-100_104_logs/probe_falsify.py` (pytest `-p` plugin, evidence
  only, lives outside `tests/`). Re-verified at final HEAD after other lanes'
  production edits (memory consolidator/store, registry, catalog): still 5/5
  red-vs-green.

## R4-104 — test-file half

| Item | Change | Evidence |
|---|---|---|
| `test_repomap.py:11` hardcoded `/Users/shadow/machina` | `REPO_ROOT = str(Path(__file__).resolve().parents[1])` | `file_test_repomap.log`: 7 passed; self-map runs against the checkout root on any machine |
| `live_db`-style fixture ERROR without DSN | `tests/test_turns_ownership.py:15-20`: skip when `MACHINA_TEST_DATABASE_URL` unset (matches the `conftest.db_pool` pattern; `test_compression.py:414` already skipped cleanly) | `live_db_prefix_red.log` (guard temporarily reverted): 10 ERROR; `live_db_after_green.log`: 10 skipped; final: 5× `10 passed` with DSN (`turns_ownership_run1-5.log`); no-DSN recheck: 10 skipped |
| `test_mcp_manage_tool.py` live round trip timeout→skip | Fails with the budget breakdown (`overall=120s, connect=30s, discovery=30s, call=30s`) via `_LIVE_BUDGETS`; timeouts passed explicitly instead of literals | Real round trip `file_test_mcp_manage_tool.log`: 42 passed. Forced-timeout probe (`probe_mcp_timeout.py`): FAIL with the breakdown, never skip (`mcp_timeout_forced_red.log`) |
| `MACHINA_API_KEY` import-time leak (`test_api.py:15`) | Module-level `os.environ[...] = "test-key-123"` → autouse `_api_key` fixture via `monkeypatch.setenv` (restored after each test) | Canary after `test_api.py` in-process: 23 passed (`env_canary_api_key_green.log`); leaky-stub discrimination: FAIL (`env_canary_api_key_red.log`). Note: the *value* `test-key-123` was the leak; the key itself is present via `.env` |
| `MACHINA_GATEWAY_NO_SCHEDULER` leak (`test_gateway.py:82`, inside `Harness.__init__`; same pattern also in `test_gateway_connector_oauth.py:31` outside this lane) | Removed the constructor side effect; suite-owned session fixture `gateway_scheduler_disabled` in `tests/conftest.py:36-52` sets `=1` from session start and restores the prior env after | `file_test_gateway.log`: 27 passed; first-test-hook (`probe_scheduler_flag.py` via `pytest_runtest_call` + `pytest_unconfigure`): flag present at first setup, absent after teardown (`scheduler_flag_probe_green.log`). The flag gates a real seam (`gateway/server.py:413`), so DB-backed scheduler tests that need a runner are unaffected |

### Reported (not fixed — outside lane, needs a production or other-lane change)

1. `tests/test_gateway_connector_oauth.py:31` still sets
   `os.environ["MACHINA_GATEWAY_NO_SCHEDULER"] = "1"` at import inside its
   `Harness` — same leak pattern, owned by another lane/file.
2. Bare `await db.connect()` fixtures that ERROR without a DSN outside this
   lane: `test_basic.py:83,100` (try/except → skip, already clean),
   `test_scheduler.py`, `test_usage_summary.py`, `test_orchestrator_usage.py`,
   `test_multi_agent.py`, `test_usage_cleanup.py`, `test_scheduler_usage.py`
   (`connected_db` fixtures — covered only where `@needs_db` marks every
   consumer; `test_phase6.py:440` is marked). Verify/repair belongs to the
   owning lane — my in-lane file (`test_turns_ownership.py`) is fixed.
3. `test_phase6.py`, `test_http_approval_resume.py`,
   `test_deep_http_cleanup.py`, `test_sse_stream_adversarial.py`,
   `test_tool_timeout_contract.py`, `test_audit_remediation.py`,
   `test_production_smoke.py`, `test_infra_fixes.py` use `monkeypatch.setenv`
   for `MACHINA_API_KEY` (already clean, no action).

## Verification (final HEAD, sequential)

- Each touched file alone: repo_install 34 ✓ · integration_permissions 52+1skip
  ✓ · gateway_providers 23 ✓ · chaos_extended 59 ✓ · memory 60 ✓ · repomap 7 ✓
  · code_review_fixes 38 ✓ · api 22 ✓ · gateway 26+1 flake (see below; 3×27 ✓ on
  rerun) · mcp_manage_tool 42 ✓
  · turns_ownership 5×10 ✓ (with DSN) + 10 skipped (no DSN) · architecture_layers
  25 ✓ (logs: `file_*.log`, `turns_ownership_run*.log`)
- `ruff check` + `ruff format --check`: clean on all 12 touched test files
  (one format fix applied to `test_memory.py`).
- One transient `TestTurnTimeout::test_turn_timeout_configurable` failure seen
  mid-lane under concurrent-lane DB contention; passes in isolation and in all
  3 subsequent full-file runs — classified flake, not a regression.
- No commits made. No files touched outside the lane
  (`tests/test_repo_install.py`, `tests/test_integration_permissions.py`,
  `tests/test_gateway_providers.py`, `tests/test_chaos_extended.py`,
  `tests/test_memory.py`, `tests/test_repomap.py`, `tests/conftest.py`,
  `tests/test_gateway.py`, `tests/test_api.py`, `tests/test_mcp_manage_tool.py`,
  plus in-lane-adjacent `tests/test_code_review_fixes.py`
  (`result > 0` evictions) and `tests/test_turns_ownership.py` (`live_db` skip)).
  Note: other lanes appended tests to `test_repo_install.py` /
  `test_integration_permissions.py` etc. after my edits — left untouched.
  Tree-state note (2026-10-15 resume): commit `0547aea` (R4-091 lane) has
  absorbed `tests/test_repo_install.py` including both R4-100 hunks above
  (verified present at HEAD:236-238, file green at 34 passed); the other 11
  test files remain uncommitted working-tree diffs.
