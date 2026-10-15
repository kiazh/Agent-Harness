# M5 — holistic review fixes: H04 (compaction summaries in prompts) + H24 (/delegate hides paused approval)

- Repo: `/Users/shadow/machina`, base revision `3ef6d5b` (working tree, uncommitted by instruction)
- Date: 2026-10-15
- Lane files (ownership): `machina/core/compression.py`, `machina/core/assembler.py`,
  `machina/core/orchestrator.py`, `machina/gateway/features/agents.py`,
  `ui/src/features/agents.ts`, and their tests. `machina/core/agent.py` untouched.
- Environment: `.venv` (Python 3.14, pytest 9.1.1), `MACHINA_TEST_DATABASE_URL='postgresql://postgres:postgres@localhost:5432/machina_test'`,
  `-p no:cacheprovider`; UI: `cd ui && npm run check` (typecheck + node tests + eslint).

---

## H04 — compaction summaries are omitted or reduced to 100 characters in subsequent prompts

**Reproduced: YES.**

### Mechanism (observed, not just read)

1. `ContextCompressor` (LLM path) stamps the summary chunk with
   `max(created_at of the old chunks)` — deliberately *behind* the preserved
   recent chunks (`compression.py:529-535`). After persistence,
   `context_manager.get_recent_context()` orders by `created_at DESC`, so with
   the default `preserve_recent=3` the summary is the 4th-newest row.
2. `PromptAssembler.assemble()` consumed only `recent_chunks[:3]`
   (`assembler.py:153-157`), so the summary was **entirely absent** from the
   prompt. RED excerpt (log `M5_h04_red.txt`, test
   `test_compaction_summary_survives_the_recent_window`):

   ```
   assert "## Compacted Context" in prompt
   AssertionError: assert '## Compacted Context' in 'You are a test agent.
   ...## Recent Activity\n[user_message] recent turn two\n  -> ok: recent turn one
   \n[user_message] recent turn zero\n\n\n## Current Query\ncontinue the task'
   ```

3. If a summary did reach recent/retrieved context, `_compress_chunk()`
   (`assembler.py:200`) had no `compression_summary` case and returned
   `f"[compression_summary] {str(payload)[:100]}"` — the first 100 characters
   of the payload **dict repr**. RED: the 7 new tests all failed against the
   pre-fix assembler (`M5_h04_red.txt`, 7 failed).

### Fix

`machina/core/assembler.py` only:

- New reserved `summary` prompt section (`## Compacted Context`), ordered
  `system → goal → summary → recent → retrieved → query`. It is added through
  the existing `add()` helper, so the assembler's token budget accounting stays
  authoritative (the section is prefix-truncated to fit and dropped only when
  the budget cannot hold any of it).
- `_latest_compaction_summary()` selects the newest usable summary:
  first `compression_summary` in the newest-first `recent_chunks` (now scanned
  across the whole provided window, not just the newest three); otherwise the
  newest archived summary in `retrieved_chunks` by `(created_at, id)`.
- `_render_compaction_summary()` renders free-form content explicitly and
  appends the validated checkpoint (objective/artifact/constraints/read-only
  scope/findings with exact counts/open questions/next action). An incomplete
  checkpoint is rejected with a warning, never rendered silently; its
  free-form content is still used.
- Summary chunks are skipped in the `recent` and `retrieved` renderings so the
  compacted context is not duplicated or mangled.

`compression.py` deliberately unchanged: the timestamping is the intended
ordering behavior; the defect was the consumer. No change was needed in
`orchestrator.py` / `gateway/features/agents.py` for H04.

### Tests (RED first)

- `tests/test_holistic_h04_compaction_prompt.py` — 7 tests:
  1. end-to-end: real `ContextCompressor` (mocked provider) + persisted order →
     assembler; summary outside the newest-3 window before the fix, fully
     present in `## Compacted Context` after;
  2. checkpoint + full 220-char content rendered explicitly (tail marker);
  3. `_compress_chunk` direct: content, not payload-dict preview;
  4. archived summary arriving via retrieved context is reserved, not dropped;
  5. only the latest summary is rendered;
  6. no duplication when the summary is inside the recent window;
  7. huge summary respects the token budget and keeps a reserved share.
- RED: `logs/M5_h04_red.txt` (7 failed). GREEN: `logs/M5_h04_green_focused.txt`
  (404 passed across compression/assembler/context, checkpoint, gateway agents,
  orchestrator suites; includes the 7 new tests).
- Note: one intermediate batch run hit a transient failure in
  `tests/test_orchestrator_usage.py::test_orchestrator_delegate_does_not_double_count`
  while other lanes were exercising the same shared test database concurrently.
  The test is untouched by this lane and passes in isolation and across
  repeated batch runs (log: latest `M5_h04_green_focused.txt`, 404 passed).

### Behavior callouts (prompt behavior change)

- Prompts for sessions with a live/retrieved compaction summary now include a
  new `## Compacted Context` section (previously the summary was absent or a
  100-char dict preview). Priority: the summary is reserved ahead of raw
  recent activity and retrieved context, after system/goal/query.
- When no summary is present, output is unchanged (verified by the existing
  assembler prompt tests, e.g. `test_e1_prompt_cache.py`).
- Invalid stored checkpoints are skipped with a warning log while the
  free-form summary content remains in the prompt.

### Residual (outside lane ownership — reported, not fixed)

`get_recent_context(session_id, limit=10)` in `machina/core/context.py`
(called from `machina/core/agent.py`) is the only live source for summaries.
Once 7+ newer chunks exist after a compaction, the summary leaves that
10-chunk fetch and the assembler can only recover it if archive keyword recall
happens to surface it via `retrieved_chunks`. Making the fetch include the
latest live `compression_summary` regardless of window (or having
`maybe_auto_compact` re-materialize it) would close that tail; both files
belong to other lanes (`agent.py` explicitly out of scope).

---

## H24 — delegate UI hides the paused approval continuation

**Reproduced: YES (UI side).**

### Mechanism

The orchestrator returns `status="waiting_approval"` with typed pauses and the
child session id (`orchestrator.py:431-468`), and the gateway serializes
`sessionId` + `needsApproval` (`gateway/features/agents.py:63-75`, already
pinned by `tests/test_delegation_transport.py`). `ui/src/features/agents.ts`
handled only `status === "error"` specially and printed everything else with
`printMarkdown(...)` — a paused child rendered like a finished answer, with
the approval actions and child-session continuation target discarded.

RED (log `M5_h24_red.txt`, before the UI change): 6 of 7 new tests failed;
e.g. the paused payload produced markdown instead of a warning notice, and no
pick/approvals RPC ever happened.

### Fix

`ui/src/features/agents.ts` only (`reportPausedDelegation`):

- `waiting_approval` is now a distinct UI state: a warning notice
  ("<agent> is waiting for approval — it did not finish…"), never markdown.
- Renders the typed approval details as a table (request id, operation,
  target) and keeps the child session id visible as the continuation target.
- Offers actions: `Allow <request>` / `Deny <request>` per pause (resolved via
  `approvals.resolve` bound to the **child** session id, so the parent's own
  pending approvals are untouched), `Switch to the subagent session`
  (`session.resume` + `switchTo`, the same pattern as `/agents sessions` and
  `/resume`), and `Leave it paused` (prints the child session id and the
  `/agents sessions` pointer).
- Complete/error rendering is unchanged.

No Python changes were required: gateway/orchestrator already supply the
continuation fields, and `approvals.resolve` already accepts a child-session
binding (`gateway/features/mode.py:213-236` validates the session binding).

### Tests (RED first)

- `ui/test/holistic-h24-delegate-pause.test.ts` — 7 tests: distinct warning
  state; approval table contents; action set; allow bound to child session;
  deny bound to child session; switch resumes the child; leave keeps the
  target; complete/error rendering unchanged.
- RED: `logs/M5_h24_red.txt` (6 failed, 1 passed — the unchanged-behavior
  test). GREEN: `logs/M5_h24_green_unit.txt` (7/7), `logs/M5_h24_green_typecheck.txt`
  (typecheck + eslint exit 0), and the final full check
  `logs/M5_h24_npm_check.txt` (`npm run check` exit 0; 506 tests, 500 passed,
  6 skipped, 0 failed).

### Behavior callouts (UI behavior change)

- `/delegate` on a paused child no longer prints a finished-looking markdown
  answer; it prints a warning + approval table and opens an action picker.
- Approve/deny from this flow resolves exactly the child's request(s) with
  `grant` defaulting to once (same as `/approvals allow`).

### Notes

- `npm run check` is green in the final run (`logs/M5_h24_npm_check.txt`:
  exit 0 — typecheck, 506 tests / 500 passed / 6 skipped / 0 failed, eslint).
  During the session the shared tree was briefly red in another lane's
  in-flight H22 work (`ui/test/harnesses.test.ts` and `ui/src/app.ts` were
  modified on disk by that lane at the time). That transient state is kept for
  the record as `logs/M5_h24_npm_check_transient_h22_lane.txt`; this lane
  touches neither file.

---

## Commands run (focused)

```bash
# Python (working dir /Users/shadow/machina)
MACHINA_TEST_DATABASE_URL='postgresql://postgres:postgres@localhost:5432/machina_test' \
  .venv/bin/python -m pytest tests/test_compression.py tests/test_compaction_checkpoint.py \
  tests/test_comprehensive.py tests/test_basic.py tests/test_property_based.py \
  tests/test_fm4_tokenizer.py tests/test_audit_regressions.py tests/test_context_windows.py \
  tests/test_final_review_fix_core_agent_context.py tests/test_e1_prompt_cache.py \
  tests/test_delegation_transport.py tests/test_audit_remediation.py tests/test_bug_fixes.py \
  tests/test_multi_agent.py tests/test_review_delegation.py tests/test_features_split.py \
  tests/test_ecc_agents.py tests/test_fm4_orchestrator.py tests/test_orchestrator_tokens.py \
  tests/test_orchestrator_usage.py tests/test_holistic_h04_compaction_prompt.py \
  -p no:cacheprovider -q          # 404 passed (logs/M5_h04_green_focused.txt)

.venv/bin/ruff check machina/core/assembler.py machina/core/compression.py \
  machina/core/orchestrator.py machina/gateway/features/agents.py \
  tests/test_holistic_h04_compaction_prompt.py    # All checks passed (logs/M5_ruff.txt)

# UI (working dir /Users/shadow/machina/ui)
node --test --test-reporter=spec test/holistic-h24-delegate-pause.test.ts   # 7/7
npm run typecheck && npm run lint                                            # exit 0
npm run check    # exit 0: typecheck + 506 tests (500 pass, 6 skip) + eslint
```

## Files changed

| File | Change |
| --- | --- |
| `machina/core/assembler.py` | H04 fix: reserved `## Compacted Context` section, latest-summary selection, explicit summary/checkpoint rendering, summary dedupe in recent/retrieved |
| `ui/src/features/agents.ts` | H24 fix: distinct paused-delegation state with approval actions + child-session continuation target |
| `tests/test_holistic_h04_compaction_prompt.py` | new RED-first regression suite (7 tests) |
| `ui/test/holistic-h24-delegate-pause.test.ts` | new RED-first regression suite (7 tests) |

No commit/push (per instructions). `machina/core/compression.py`,
`machina/core/orchestrator.py`, and `machina/gateway/features/agents.py` were
inspected and deliberately left unchanged (both findings' Python sides were
already correct).
