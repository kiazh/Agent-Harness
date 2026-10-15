# HELD-44 — Cancelled file writes can overwrite later edits (finding 4)

- **Lane:** HELD-44, per `docs/verification/FIX_PLAN_ROUND5_2026-10-15.md` (finding 4).
- **Finding:** `docs/verification/CODEBASE_REVIEW_CURRENT_2026-10-14.md` §4, "P1 — Cancelled
  file writes can overwrite later edits" (`machina/tools/file.py:546`, `:285`, `:289`;
  `machina/core/agent.py:1033`).
- **Verdict:** **REPRODUCED → FIXED.** Not refuted; the late overwrite is real and
  deterministic on a delayed filesystem.
- **Base commit:** `e6f836c` (+ concurrent lanes' uncommitted work). No commit made.
- **Sole-writer scope respected:** `machina/tools/file.py` (write path only) and the new
  `tests/test_r5_004_write_cancel.py`. `machina/core/agent.py` was **not modified**
  (`git diff --quiet machina/core/agent.py` → clean) — see "agent.py verdict" below.

## Claim under test

`write_file` ran its worker through `asyncio.to_thread`. Cancelling the await
(`asyncio.wait_for` hard timeout at `agent.py:1033`, or turn cancellation at
`agent.py:1181-1188`) stopped the *await*, not the worker thread. The old turn could
therefore report cancelled and release ownership while the worker later performed the
final `os.replace`, overwriting an edit from a newer turn or a user.

## Reproduction (RED, before the fix)

Two standalone harnesses against real syscalls (only the *first* atomic-write call is
delayed; temp creation + replace are real):

1. **File level** — `HELD-44_probe_red.py`. Turn 1's worker is parked at the
   filesystem gate; turn 1 is cancelled and returns; turn 2 writes `NEW`; a wall-clock
   timer then releases the orphaned worker, which runs its real replace with `OLD`.
   Pre-fix (`HELD-44_red_file_level.txt`):

   ```
   turn-1 cancelled=True after_new='NEW' final='OLD'
   VERDICT: LATE OVERWRITE (old cancelled write clobbered newer edit)
   ```

2. **agent path** — `HELD-44_probe_agent_path.py`, using the exact agent primitives
   (`registry.execute` inside `asyncio.wait_for`; task cancel + `gather`). Pre-fix
   (`HELD-44_red_agent_path.txt`):

   ```
   [A wait_for]    outcome=timeout final='OLD'
   [B turn-cancel] cancelled=True final='OLD'
   VERDICT: LATE OVERWRITE on scenario(s) ['A', 'B']
   ```

Committed regression tests captured RED pre-fix (`HELD-44_pytest_red.txt`):

```
FAILED tests/test_r5_004_write_cancel.py::test_cancelled_write_joins_worker_before_releasing_ownership
FAILED tests/test_r5_004_write_cancel.py::test_tool_timeout_joins_worker_and_preserves_newer_edit
FAILED tests/test_r5_004_write_cancel.py::test_turn_cancel_gather_waits_for_worker
FAILED tests/test_r5_004_write_cancel.py::test_cancellation_join_is_bounded_by_worker
4 failed, 2 passed
```

All four fail for the same named reason: the cancellation returned while the worker's
replace had **not** completed (`assert gate.finished.is_set()` → `False`).

## Fix (GREEN)

`machina/tools/file.py` — one new helper used by the `write_file` worker call:

- `_run_worker_joined(operation)` creates the worker with
  `asyncio.ensure_future(asyncio.to_thread(operation))` and awaits it under
  `asyncio.shield`, so caller cancellation can no longer detach the worker future.
- On `CancelledError`, it joins the worker to completion before re-raising, so
  `CancelledError` does not propagate — and ownership is not released — until the
  filesystem effects are done. Each join is itself shielded and the loop re-joins on a
  repeated cancellation, so a second cancel cannot orphan the running thread mid-write.
- A worker failure during the join is logged (`logger.warning`) rather than silently
  lost; the cancellation remains the primary outcome.
- `write_file` calls `_run_worker_joined(_write)` instead of `asyncio.to_thread(_write)`.

The R5-B dir-fd anchored write path (`_write_anchored_inside` / `_atomic_write_file_at`
/ `_open_parent_dir_fd`) is untouched and is the worker body that now runs to completion
under the join. `read_file` / `list_files` are unchanged: they have no filesystem
effects, so a late/no-join read is harmless and leaving them keeps read cancellation
latency unchanged.

**Behavior/API change (recorded):** a cancelled/timed-out `write_file` now returns its
`CancelledError` only after the worker's effects finish. Cancellation is therefore
bounded by the worker's completion (a single file write), not by an arbitrary wait.

## agent.py verdict — no change required

The finding cites `agent.py:1033`. Root cause and fix live in the file worker; both agent
cancellation entry points already await completion, so they inherit the join:

- `agent.py:1033` — `await asyncio.wait_for(registry.execute(...), timeout=...)`. On
  timeout (Python ≥3.11, and this repo requires ≥3.11) `wait_for` cancels the inner task
  and **waits for it**; the inner task now joins the worker, so the tool is reported timed
  out only after the write finished. Proven by scenario A above.
- `agent.py:1181-1188` — on turn cancellation the `finally` block cancels every task and
  `await asyncio.gather(*tasks, return_exceptions=True)`. With the join, the gather does
  not return until every worker has finished, i.e. before turn ownership is released.
  Proven by scenario B above.

`machina/core/agent.py` is byte-identical to HEAD; touching it would have been an
unnecessary change outside the root cause.

## Verification (GREEN)

| Evidence | Result |
|---|---|
| `HELD-44_green_file_level.txt` | `final='NEW'`, **CONTAINED** |
| `HELD-44_green_agent_path.txt` | both scenarios `final='NEW'`, **CONTAINED** |
| `HELD-44_pytest_green.txt` | **6 passed** (RED→GREEN: 4 failed/2 passed → 6 passed) |
| `HELD-44_pytest_focused.txt` | **176 passed** (R5-B atomic-escape, R4-090 tools, handoff regressions, parallel tool exec, tool timeout contract/timeouts, tool-cache, execution-context boundary, code-quality, architecture-layers, security) |
| `HELD-44_pytest_turn_sweep.txt` | **85 passed, 1 failed** — the one failure is pre-existing and unrelated (see below) |
| `HELD-44_pytest_adjacent.txt` | **68 passed** (env-file adversarial, profile/evict/skill atomicity, godfile, repomap, benchmark, deadlock guard) |
| `HELD-44_ruff_check.txt` | `All checks passed!` |
| `HELD-44_ruff_format.txt` | `2 files already formatted` |

**Cancellation still bounded:** `test_cancellation_join_is_bounded_by_worker` asserts the
cancellation joins the worker (the write completes before cancel returns) while completing
well inside a 5 s bound; the join is bounded by the write, not an arbitrary wait.

### Pre-existing, unrelated failure (not this lane)

`tests/test_a5_turn_flow.py::test_agent_py_shrank_below_its_pre_extraction_size` asserts
`len(agent.py) < 2056`, but `machina/core/agent.py` is 2110 lines at HEAD. `agent.py` is
byte-identical to HEAD in this working tree (`git diff --exit-code machina/core/agent.py`
clean) and the test reads only `agent.py`, so the failure is independent of this lane and
is not a regression.

## Files changed

- `machina/tools/file.py` — `write path only`: +33 lines (`_run_worker_joined` helper,
  `collections.abc.Callable` import, one call-site swap). R5-B anchored-write path intact.
- `tests/test_r5_004_write_cancel.py` — new (6 tests).
- Proofs (this directory): `HELD-44_write_cancel.md`, `HELD-44_probe_red.py`,
  `HELD-44_probe_agent_path.py`, and the `HELD-44_*.txt` logs.

## Notes / residual risk

- The join's bound is the worker's own completion. A genuinely wedged write (e.g. a hung
  network filesystem) will hold the cancellation open for as long as the syscall blocks —
  the same duration it would have held the worker even without cancellation. Gateway turn
  retirement already bounds its *observation* with `CANCEL_JOIN_TIMEOUT_SECONDS` while
  retaining the inner task's claim (`gateway/server.py:_run_turn_with_timeout`), so a
  resistant worker cannot yield session ownership to a newer turn.
- Deliberate consequence: a `write_file` that was already dispatched now always runs to
  completion even if cancellation arrives before the worker thread has produced its
  effects. This is the intended "ownership retained" semantics — the operation was
  authorized and is the one the caller asked for — and it is the price of guaranteeing the
  write can never land after a newer edit.
- The same `to_thread`-detach class affects other blocking tools (e.g. terminal/exec);
  those are owned by other lanes (MCP child retirement is R5-A A2) and were not touched.
- No commit (coordinator commits).
