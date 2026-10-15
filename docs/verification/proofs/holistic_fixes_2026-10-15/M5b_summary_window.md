# M5b — H04 residual: the latest usable compaction summary no longer ages out of the live source window

- Repo: `/Users/shadow/machina`, base revision `3ef6d5b` (working tree, uncommitted by instruction)
- Date: 2026-10-15
- Lane files (ownership): `machina/core/context.py`, `machina/core/agent.py`, and their
  focused tests. `machina/core/agent.py` was **not modified** by this lane (the fix lands at the
  live source so both call sites inherit it; the L1 temperature-forwarding diff there is untouched).
  `machina/core/assembler.py` (M5) was read but not edited.
- Environment: `.venv` (Python 3.14.5, pytest 9.1.1),
  `MACHINA_TEST_DATABASE_URL='postgresql://postgres:postgres@localhost:5432/machina_test'`,
  `-p no:cacheprovider`.

---

## Residual closed (as reported by the M5 lane)

`get_recent_context(session_id, limit=10)` in `machina/core/context.py` — called from
`machina/core/agent.py:614`, the only live source feeding `PromptAssembler` — returns only the
newest 10 rows. `ContextCompressor` deliberately stamps its summary behind the preserved recent
chunks (`compression.py:529-535`), so after ~7 newer chunks the summary leaves the window and the
M5 assembler section (`## Compacted Context`) can only recover it if archive keyword recall happens
to surface a copy via `retrieved_chunks`. That tail is now closed at the live source.

## Fix

`machina/core/context.py` only:

- `get_recent_context()` keeps the exact same newest-first `limit`-row window for ordinary chunks.
  While building the result it tracks whether the window already carries a *usable*
  `compression_summary`. If not, it appends **at most one** extra entry: the latest usable live
  summary (new `_latest_usable_compaction_summary()`), in the same
  `{"type", "payload", "tokens"}` shape the assembler consumes.
- `_latest_usable_compaction_summary()` scans a bounded **5** newest
  `compression_summary` rows (`_SUMMARY_RECOVERY_SCAN_LIMIT`) and returns the first usable one —
  never a collection, never an archived copy (archive recall stays the assembler's
  `retrieved_chunks` fallback). It re-checks the row type so a test double that ignores the SQL
  filter cannot inject a non-summary, and skips a corrupt (unreadable) row — recovery reads rows
  the newest-N window never touched, so one bad row must neither fail the turn nor mask an older
  usable summary (same pattern as `_get_legacy_images`).
- `_summary_payload_is_usable()` mirrors the assembler's `_summary_is_usable` contract
  (non-whitespace `content` or a `checkpoint` dict), so a degenerate newest row cannot mask an
  older usable summary.
- The extra entry is appended at the tail: it is provably older than every window row (otherwise
  it would be in the window), so newest-first ordering is preserved and `recent_chunks[:3]` —
  the assembler's recent-activity rendering and `plan_workflow`'s stack inheritance — is
  untouched.
- No API/window widening: `limit` is unchanged, at most one row is added, and the assembler's
  `add()`/token-budget accounting stays the sole authority for what reaches the prompt. The lookup
  uses the existing `idx_context_chunks_session_type_created` index (an equality seek on
  `(session_id, chunk_type)`, empty and cheap for sessions without summaries).
- `machina/core/agent.py` needed no change: it passes `recent` through to
  `assemble_turn_prompt` (`machina/core/prompt.py`) unchanged. `orchestrator._parent_context`
  (the other caller) filters to user/assistant/result chunk types, so it ignores the extra row.

## Reproduction (RED first)

- New suite `tests/test_holistic_m5b_summary_window.py` (7 tests) drives the **real**
  `ContextManager` against an isolated schema: a summary stamped at `BASE`, then 10 newer ordinary
  chunks with explicit, deterministic timestamps (`limit=10`), then inspects
  `get_recent_context()` and assembles its output.
- RED against pre-fix `context.py` (worktree restored to `HEAD:context.py` for the run):
  `logs/M5b_red_final.txt` — **5 failed, 2 passed**; every summary-carrying assertion failed
  because the summary is simply absent, e.g.:

  ```
  E  assert 0 == 1
  E   +  where 0 = len([])          # summaries in the live source
  ...
  E  AssertionError: assert '## Compacted Context' in 'You are a test agent.
  ...## Recent Activity\n[user_message] turn 7\n[user_message] turn 8\n[user_message] turn 9
  \n\n\n## Current Query\ncontinue the task'
  ```

  The 2 pre-fix passes are the controls (no-summary window; in-window summary not duplicated).
- GREEN: `logs/M5b_green_final.txt` — 485 passed (full focused set below).
- Note: an earlier RED attempt (`logs/M5b_red.txt`, superseded by `M5b_red_final.txt`) recorded a
  transient `asyncpg DeadlockDetectedError` in the shared test DB's `initialize_schema()` while
  other lanes exercised the same database concurrently (same class of contention M5 documented);
  it is infrastructure, not the finding. The re-run of that single test failed for the expected
  RED reason.

## Tests

`tests/test_holistic_m5b_summary_window.py` — 7 tests (RED before fix):

1. **aged out**: 10 newer chunks push the summary out of the newest-10 window → the summary is
   still the live source's single extra entry, the assembler renders `## Compacted Context` with
   the full marker, and the ordinary window (`turn 9..0`) plus the newest-three recent-activity
   semantics are unchanged (`turn 6` absent);
2. **control, no summary**: exactly `limit` rows, newest-first, and the assembled prompt has no
   `## Compacted Context` (ordinary path unchanged);
3. **control, in-window summary**: the normal post-compaction layout (summary behind 3 preserved
   chunks) is returned exactly once, in place — no duplication;
4. **only the latest**: two live summaries outside the window → only the newer one is carried;
5. **usable fallback**: a degenerate newest row (no content/checkpoint) does not mask the older
   usable summary;
6. **corrupt row**: an unreadable newest summary row is skipped (never raises, never masks the
   older usable summary);
7. **budget control**: the carried entry goes through the assembler's authoritative accounting —
   with a tiny budget the prompt stays within `budget + 5` and keeps the reserved summary share.

Existing suites re-run focused: H04 compaction prompt (M5), context windows, HELD-55 history
order, compression, compaction checkpoint, core agent/context fixes, E1 prompt cache, schema
conformance (the new literal SQL compiles against the schema), comprehensive, integration,
gateway images, chaos extended, multi-agent, orchestrator usage/tokens, skill workflows, delegate
scope, failed-delegation usage, delegation contracts, phase 5 completion. Also re-ran the
L1-adjacent agent suites (`test_holistic_h11_temperature.py`, `test_holistic_h01_turn_source_close.py`
and others, `logs/M5b_green_focused3.txt`) to show this lane does not disturb them.

## Commands run (focused)

```bash
# working dir /Users/shadow/machina
MACHINA_TEST_DATABASE_URL='postgresql://postgres:postgres@localhost:5432/machina_test' \
  .venv/bin/python -m pytest tests/test_holistic_m5b_summary_window.py \
  tests/test_holistic_h04_compaction_prompt.py tests/test_context_windows.py \
  tests/test_held_55_history_order.py tests/test_compression.py \
  tests/test_compaction_checkpoint.py tests/test_final_review_fix_core_agent_context.py \
  tests/test_e1_prompt_cache.py tests/test_schema_conformance.py \
  tests/test_comprehensive.py tests/test_integration.py tests/test_gateway_images.py \
  tests/test_chaos_extended.py tests/test_multi_agent.py tests/test_orchestrator_usage.py \
  tests/test_orchestrator_tokens.py tests/test_skill_workflows.py tests/test_delegate_scope.py \
  tests/test_failed_delegation_usage.py tests/test_delegation_contracts.py \
  tests/test_phase5_completion.py -p no:cacheprovider -q      # 485 passed (logs/M5b_green_final.txt)

.venv/bin/ruff check machina/core/context.py tests/test_holistic_m5b_summary_window.py
.venv/bin/ruff format --check machina/core/context.py tests/test_holistic_m5b_summary_window.py
                                                              # logs/M5b_ruff.txt (exit 0)
```

## Behavior callouts

- `get_recent_context` may now return `limit + 1` entries, but only when the window carries no
  usable live summary and one exists outside it. Both production callers are compatible:
  `agent.py`/`PromptAssembler` renders the summary in its reserved section, and
  `orchestrator._parent_context` filters non-conversation chunk types out. Archive retrieval is
  unchanged (the carried entry is live-only).
- No summary present → the returned window and the assembled prompt are unchanged (control test).
- No new query is issued when the window already carries a usable summary; otherwise one bounded
  indexed lookup (≤5 rows) is added per call.

## Files changed

| File | Change |
| --- | --- |
| `machina/core/context.py` | `get_recent_context` carries the latest usable live `compression_summary` when the window has none; bounded `_latest_usable_compaction_summary` + `_summary_payload_is_usable` |
| `tests/test_holistic_m5b_summary_window.py` | new RED-first regression suite (7 tests) |

No commit/push (per instructions). `machina/core/agent.py` and `machina/core/assembler.py` were
inspected and deliberately left untouched.
