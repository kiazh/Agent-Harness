# R4-096 dead-code wave — evidence (2026-10-14, lane R4-096)

Owner: sole writer of `machina/tools/base.py`, `machina/api/app.py`,
`machina/core/context.py`, `machina/rag/chunker.py`,
`machina/core/compression.py` + the tests pinning the removed behavior.
Untouched as ordered: `core/agent.py`, `core/config.py`, `tests/test_chaos.py`.
No commit (coordinator owns ledger + commit).

## Decisions (recorded here, not in FEATURE_LEDGER.md)

1. **Tool-cache policy (`tools/base.py`) — REMOVE the cache.**
   Probe at HEAD (all shipped tool modules imported, 31 tools):
   0 cacheable, 16 mutating/flushing, 15 read-effect/non-cached.
   Every shipped tool declares broker `effects`, so the TTL result cache
   was unreachable in production — only synthetic effect-free test tools
   could populate it. Removed: `_result_cache`, `_cache_ttl`,
   `_cache_max_size`, `_cache_policy` + the `_SIDE_EFFECT_*` /
   `_UNCACHEABLE_TOOLS` / `_READ_EFFECTS` heuristic sets (sole consumer was
   the cache), `_cache_scope`, cache read/write/flush in `execute()`,
   now-unused `hashlib`/`json`/`time`/`logging` imports.
   Kept (live): `Tool.effects`, `declare_effects` (all tool modules +
   broker consume them), `_definitions_cache`, arg validation, audit.
   `execute()` now always runs the tool fresh.
2. **`_RpcGateway` TTL/lock (`api/app.py`) — simplify.**
   Removed `_RESPONSE_TTL_SECONDS`, `_response_timestamps`,
   `_cleanup_old_responses`, and the `_id_lock` alias (same object as
   `_lock`; the `getattr` dance was dead complexity). Kept the plain
   `asyncio.Lock` around id assignment — the concurrency tests prove ids
   must stay unique under `asyncio.gather`. Rationale: `call()` pops its
   own rid synchronously after `handle_line`, so `_responses` is empty
   whenever no call is in flight; orphans are impossible through this seam.
3. **`ContextManager` dead params (`core/context.py`) — remove.**
   `__init__(batch_size=50, cache_size=128)` + `self._pending`: zero
   readers repo-wide (`_batch_size` stored, never read; `cache_size` not
   even stored; `_pending` never touched by production). `__init__` is now
   empty; all call sites already use `ContextManager()`.
4. **`overlap_offset` (`rag/chunker.py`) — remove writer only.**
   Single repo-wide hit was the writer (`_apply_overlap`); no reader in
   `machina/`, `tests/`, or `ui/`. Overlap merge logic untouched.
5. **`delete_chunks` test-double branch (`core/context.py`) — remove.**
   The `TypeError/AttributeError` fallback (incl. `inspect` import) existed
   only for `MagicMock/AsyncMock` doubles. Production path (single
   transaction, both deletes, missing-table probe) is unchanged.
6. **`compress`/`acompress` duplication (`core/compression.py`) — extract.**
   (R4-011 already landed.) Shared preamble → `_plan_compression`
   (empty/preserve short-circuits, token math, split, checkpoint
   validation — same order as before); shared truncate ladder →
   `_truncate_old` (used by both); sync LLM+fallback ladder →
   `_compress_old` (async path still awaits `_llm_summarize` directly, as
   it must). `_finalize_result` (pre-existing) unchanged.
7. **Out of lane — NOT touched:** `rag/pipeline.py` `_last_search_mode`
   (write-only, but file is owned by another lane), pipeline search caches
   (live: read at search paths), embedder/reranker `cache_size`/`batch_size`
   (live), `session_manager._cache` (live), `ToolRegistry._definitions_cache`
   (live). Flagged for the pipeline owner / coordinator.

## RED on unfixed code (stash sources only, tests stay)

```
git stash push -- <5 source files>
pytest test_effect_free_tools_execute_fresh_every_call \
       TestRpcGatewayTTL::test_no_ttl_machinery_remains
→ 2 failed in 4.55s (cache-hit count 1≠2; TTL attrs present)
git stash pop  (clean)
```

## GREEN after fix

- `R4-096_green_focused.txt`: 425 passed — api_bugfixes, rpc_gateway,
  bugfixes_tool_cache_redos, fm4_context, review_round2_fixes,
  compression, handoff_regressions, mcp_manage_tool, comprehensive
  (full file, incl. rewired `TestContextManagerMocked::test_delete_chunks`
  + `TestToolRegistry`), chunker_overlap, fm5_chunker, audit_regressions,
  fm1_regressions, r4_105_caller_scope_real_run,
  production_readiness_p1, skill_workflows.
- `test_load_concurrency.py -k "rpc_gateway or mixed_read_write"`: 2 passed.
- Parity probes (behavior unchanged, real seams):
  - `compress` vs `acompress` identical payloads/token-counts on truncate
    path, LLM-failure fallback path (both `truncate`), and checkpoint path
    (leading `compression_summary` with equal checkpoint).
  - Chunker: multi-chunk overlap still merges; zero `overlap_offset` keys.
  - Registry: no `_result_cache`/`_cache_scope` attrs; identical
    `execute()` calls run the tool twice (no stale hits); workspace A/B
    reads return their own content (converted test).
  - RPC: real `/rpc` route tests (`test_rpc_gateway.py`, 6 tests) green —
    dispatch reuse, redaction, error mapping, missing-response 500 with
    `_responses == {}`; 100-call test leaves `_responses == {}`.
  - `delete_chunks`: `test_fm4_context` transactional doubles green
    (single tx, both DELETEs, missing-table skip).
- `ruff check` + `ruff format --check`: clean on all 14 touched files.
- `bandit` on the 5 source files: no new warnings (one pre-existing
  Low-severity B110 in untouched `_chunk_to_text` fallback).

## Tests converted (never deleted silently)

| Test | Change |
|---|---|
| `test_bugfixes_tool_cache_redos.py::test_effect_free_…` | Phantom cache-hit pin → asserts execute-fresh every call, per context |
| `test_bugfixes_tool_cache_redos.py` fixture + `read_file` assert | Dropped `_result_cache` clears/assert; added cross-workspace re-read |
| `test_api_bugfixes.py::TestRpcGatewayTTL` (3 tests) | Phantom TTL-aging pins → drive `gw.call()`: no TTL attrs, empty dict after 1/100 calls, unanswered→RpcError with no residue |
| `test_comprehensive.py::test_delete_chunks` | Rewired from fallback-dependent double to transactional double; asserts both DELETEs in one tx |
| `test_mcp_manage_tool.py` | Dropped `_cache_policy` asserts; effects asserts kept |
| `test_skill_workflows.py` integrity test | Renamed (was `…_bypassed_by_tool_result_cache`); body still drives real `registry.execute` |
| `test_load_concurrency.py` rpc test | Dropped manual `_response_timestamps` implant |
| `test_r4_105…`, `tests/conftest.py` | Dropped `_result_cache.clear()`; conftest also dropped dead `context_manager._pending.clear()` |
| `test_production_readiness_p1.py` | Dropped dead `_pending` setattr + now-unused import |

## Files changed (lane R4-096)

Sources: `machina/tools/base.py`, `machina/api/app.py`,
`machina/core/context.py`, `machina/rag/chunker.py`,
`machina/core/compression.py` (net −179 lines).
Tests: `tests/conftest.py`, `tests/test_api_bugfixes.py`,
`tests/test_bugfixes_tool_cache_redos.py`, `tests/test_comprehensive.py`,
`tests/test_load_concurrency.py`, `tests/test_mcp_manage_tool.py`,
`tests/test_production_readiness_p1.py`,
`tests/test_r4_105_caller_scope_real_run.py`,
`tests/test_skill_workflows.py`.
Evidence: this file + `R4-096_green_focused.txt`.
