# R4-092 — Research cluster: benchmark timeouts/exit codes, cleanup isolation, `-O` safety, decimal normalization, loader validation

- **Task:** R4-092 (bullets a–e) · P2 · 2026-10-14
- **Writer:** R4-F3 subagent (sole writer of `machina/research/benchmark.py`,
  `machina/research/workflow_benchmark.py`, `machina/research/locomo.py`,
  `machina/research/identity_eval.py`, `machina/research/workflow_trial.py` + the three
  listed test files). **No commit** (parent session commits).
- **Skills lenses:** `skills/ecc/skills/python-patterns/SKILL.md`,
  `skills/ecc/skills/error-handling/SKILL.md` (fail-loud, never swallow, surface at the
  boundary).
- **HEAD at RED run:** `6f0df5b` (working tree also carried an unrelated in-progress
  change to `machina/memory/redaction.py`, untouched by this task).

## Environment / commands

```bash
uv run pytest tests/test_benchmark_tool.py tests/test_fm6_locomo.py tests/test_locomo_answers.py \
    -q -p no:cacheprovider                       # RED: 21 failed, 46 passed
uv run pytest tests/test_locomo_answers.py tests/test_fm6_locomo.py tests/test_benchmark_tool.py \
    tests/test_research_phase2.py tests/test_research_integration.py tests/test_architecture_layers.py \
    -q -p no:cacheprovider                       # GREEN: 134 passed
uv run ruff check … && uv run ruff format --check …   # clean
```

All logs live next to this file: `R4-092_red_pytest.txt`, `R4-092_green_pytest.txt`,
`R4-092_red_probe_optimized.txt`, `R4-092_green_probe_optimized.txt`,
`R4-092_green_e2e_db.txt`, `R4-092_green_cli.txt`, `R4-092_normalize_probe_caught_bug.txt`,
`R4-092_green_normalize_probe.txt`, `R4-092_ruff.txt`, `R4-092_probe_optimized.py`.

## (a) Benchmark subprocess timeouts + non-zero exit on any section error

- **Mechanism (RED):** `_suite()` / `_ui()` called `subprocess.run(...)` with no `timeout`
  (an unbounded child could hang the whole benchmark forever), and `main()` returned `0`
  unconditionally — a broken section printed `{error: …}` and a green `done.`; a failing
  `--with-suite` row (`exit_code=1`) was equally invisible to CI.
- **Fix:** bounded subprocesses (`_SUITE_TIMEOUT_SECONDS = 3600`,
  `_UI_TIMEOUT_SECONDS = 900`); `TimeoutExpired` becomes an error payload
  (`exit_code=1`, message `… timed out after Ns`); `main()` records any section whose
  payload carries `error` or a non-zero `exit_code` (including `suite`/`ui`) and returns
  `1` after printing the failed section list. `--json` evidence is still written first.
- **RED evidence:** `R4-092_red_pytest.txt` — `test_section_failure_exits_nonzero`,
  `test_suite_failure_exits_nonzero`, `test_subprocess_sections_have_timeouts`,
  `test_suite_timeout_is_reported_as_an_error` all fail (rc was 0 / no `timeout` kwarg).
- **GREEN evidence:**
  - `R4-092_green_pytest.txt` (4 tests pass).
  - `R4-092_green_cli.txt` — real CLI E2E: malformed LoCoMo dataset →
    `failed sections: locomo: ValueError …`, **EXIT=1**; healthy `--section environment`
    → `done.`, **EXIT=0**.
- **Residual:** the default timeouts (1 h suite / 15 min UI) are generous by design; they
  exist to bound hangs, not to police duration.

## (b) Cleanup isolation per step

- **Mechanism (RED):** three cleanup blocks awaited operations sequentially without
  per-step guards:
  - `workflow_benchmark.compare` finally (`effects` → `runs` → `session` → `close`): the
    first failing DELETE skipped the remaining deletes **and** `database.close()`, and its
    exception replaced any original trial error.
  - `locomo.evaluate_session_recall` finally: the first failing session delete skipped all
    later sessions and masked the original error.
  - `identity_eval.evaluate_identity_scenarios` finally: the first failing table delete
    skipped the other five tables and masked the original error.
- **Fix (all three, same contract):** every cleanup step is attempted in its own
  `try/except`; failures are collected, the full cleanup always runs (including
  `database.close()`), and afterwards: with no in-flight error a `RuntimeError("… cleanup
  failed: <steps>")` is raised; with an in-flight error the original exception is re-raised
  unchanged and the cleanup failure is attached via `BaseException.add_note` (so it is
  visible in the traceback, never swallowed and never masking).
- **RED evidence:** `R4-092_red_pytest.txt` —
  `test_compare_cleanup_attempts_every_step_when_a_delete_fails`,
  `test_compare_cleanup_failure_does_not_mask_the_trial_error`,
  `test_session_recall_cleanup_isolates_delete_failures`,
  `test_session_recall_cleanup_failure_does_not_mask_the_original_error`,
  `test_identity_cleanup_attempts_every_delete`,
  `test_identity_cleanup_failure_does_not_mask_the_original_error` fail (later steps never
  ran; original errors were masked).
- **GREEN evidence:**
  - `R4-092_green_pytest.txt` (6 tests pass; DB-backed cleanup suites in
    `test_research_phase2.py` also pass).
  - `R4-092_green_e2e_db.txt` — real PostgreSQL + langgraph `compare(dsn, 1)` run:
    `(sessions, trial_runs, trial_effects)` before `(0, 0, 0)` → after `(0, 0, 0)`.
- **Residual:** `graph.delete_thread` calls inside the measured section are not isolated
  (out of the named scope; `GraphApprovalTrial.__aexit__` still closes the saver on
  failure). No claim is made about checkpoint rows when a thread delete itself fails.

## (c) `-O`-safe measurement (no side-effecting awaits inside `assert`) — `workflow_benchmark`

- **Mechanism (RED):** `compare()` measured with
  `assert await native.start(...) == "pending"` / `assert await graph.resume(...) ==
  "complete"` (4 sites). Under `python -O` the asserts are stripped, so the measured calls
  never ran and the tool reported fabricated timings (and `0` graph checkpoint rows).
- **Fix:** explicit status checks that raise typed `RuntimeError` on any unexpected status;
  the awaits are plain statements.
- **RED evidence:**
  - `test_workflow_benchmark_has_no_side_effects_inside_asserts` (AST scan) fails on lines
    55/56/63/66 — see `R4-092_red_pytest.txt`.
  - `R4-092_red_probe_optimized.txt` — real `uv run python -O R4-092_probe_optimized.py`
    against unfixed code: `starts=0` / `resumes=0`.
- **GREEN evidence:** same probe after the fix: `starts=2` / `resumes=2`
  (`R4-092_green_probe_optimized.txt`); AST test + call-count test pass
  (`R4-092_green_pytest.txt`); real `compare` run exercised every step
  (`R4-092_green_e2e_db.txt`).
- **Note:** the probe file is kept in this directory as rerunnable evidence.

## (d) `_normalize_answer` must not merge decimals

- **Mechanism (RED):** punctuation stripping removed the decimal point, so `format(3.5,
  "g") == "3.5"` became the token `"35"` — gold `3.5` scored a perfect F1 against the
  wrong prediction `35`, and `_normalize_answer(2024.5) == "20245"` was pinned by a test.
- **Fix:** a decimal point *between digits* survives the strip while every other punctuation
  character is still removed (named-group alternation
  `(?P<decimal>(?<=\d)\.(?=\d))|[^\w\s]`, replacement `match.group("decimal") or ""`);
  whitespace still collapses.
- **Tests updated deliberately:** `tests/test_fm6_locomo.py` expectations changed
  `(2024.5, "20245") → (2024.5, "2024.5")` plus `(0.5, "0.5")`; added the decimal-preserving
  non-merge case (`token_f1("35", 3.5) == 0.0`, `token_f1("3.5", "35") == 0.0`,
  `_normalize_answer(3.5) == "3.5"`).
- **RED evidence:** `R4-092_red_pytest.txt` —
  `test_non_integral_float_keeps_its_decimal` (now asserts `"3.5"`),
  `test_decimal_gold_does_not_merge_with_integer_prediction`, and the two new parametrized
  expectations fail. (The old `test_non_integral_float_keeps_its_decimal` only passed
  because *both* sides were mangled to `"35"` — collision, not correctness.)
- **Self-adversarial catch (honest note):** the first callback draft kept *standalone*
  punctuation dots (`'...' -> '...'`, `'e.g.' -> 'e.g.'`) — the unit suite did not cover
  that, but the standalone normalization probe did (`R4-092_normalize_probe_caught_bug.txt`).
  The regex now distinguishes the decimal branch by named group, and the missing cases are
  pinned by the new parametrized
  `test_normalization_strips_standalone_punctuation_only` (`"3.5." → "3.5"`, `"$3.50!" →
  "3.50"`, `"e.g. version 1.2.3" → "eg version 1.2.3"`, `"..." → ""`).
- **GREEN evidence:** `R4-092_green_pytest.txt` (134 passed, incl. the 6 new pinning cases);
  `R4-092_green_normalize_probe.txt` (all 9 spot-check rows + decimal semantics); all
  `token_f1` suites (`test_locomo_answers.py`, `test_fm6_locomo.py`) pass.

## (e) `load_locomo` validates text/speaker types (ValueError contract)

- **Mechanism (RED):** `DialogueTurn(row["dia_id"], row.get("speaker", ""),
  row.get("text", ""))` accepted non-strings (`123`, `None`, lists, dicts) silently, so a
  malformed JSON reached retrieval/answer scoring as junk text.
- **Fix:** after the existing `dia_id` check, missing `speaker`/`text` still default to
  `""`, but a present non-string value raises
  `ValueError("dialogue turn text and speaker must be strings")` — matching the loader's
  existing fail-fast contract (`dialogue turn needs dia_id`, `… must be a list`, …).
- **RED evidence:** `R4-092_red_pytest.txt` — 5 parametrized
  `test_load_locomo_rejects_non_string_turn_fields` cases fail with `DID NOT RAISE
  ValueError`.
- **GREEN evidence:** `R4-092_green_pytest.txt`; the missing-fields tolerance is pinned by
  `test_load_locomo_tolerates_missing_turn_fields_as_empty`.

## Verification summary

| Check | Result | Log |
|---|---|---|
| New/changed tests vs unfixed code (RED) | 21 failed / 46 passed | `R4-092_red_pytest.txt` |
| `-O` probe vs unfixed code (RED) | `starts=0 resumes=0` | `R4-092_red_probe_optimized.txt` |
| Focused suites: `test_locomo_answers.py test_fm6_locomo.py test_benchmark_tool.py test_research_phase2.py test_research_integration.py test_architecture_layers.py -q -p no:cacheprovider` | **134 passed** | `R4-092_green_pytest.txt` |
| `-O` probe after fix (GREEN) | `starts=2 resumes=2` | `R4-092_green_probe_optimized.txt` |
| Normalization spot-check, 9 adversarial inputs + decimal semantics | OK | `R4-092_green_normalize_probe.txt` |
| Real DB + langgraph `compare` E2E, row counts unchanged | OK | `R4-092_green_e2e_db.txt` |
| Real CLI: broken section → EXIT=1; healthy → EXIT=0 | OK | `R4-092_green_cli.txt` |
| Related suites: `test_reranker_local.py` (35), `workflow_trial` + checkpoint-count (8) | green | (run inline) |
| `ruff check` + `ruff format --check` on touched files | clean | `R4-092_ruff.txt` |

## Residuals / honest limits

- Fakes are used for the failure-injection tests (a failing real DELETE cannot be produced
  against a healthy test DB); the real DB + langgraph path is additionally exercised
  end-to-end without injected failures (`R4-092_green_e2e_db.txt`), and the DB-backed
  cleanup suites in `test_research_phase2.py` pass against `machina_test`.
- `benchmark._ui()` timeouts are only unit-proven (fake `subprocess.run`); the UI check is
  not run in this environment.
- Not fixed (out of the declared scope): `graph.delete_thread` loop isolation, the
  pre-`try` `connect()`/session-INSERT window in `compare`, and the same cleanup pattern in
  `evaluate_archive_retrieval`.
- No commit made; the parent session owns staging/committing.
