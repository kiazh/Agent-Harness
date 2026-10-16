# Head-to-head: Machina vs OpenCode on the same DeepSeek flash model — 3-task matrix

- **Date:** 2026-10-13 (runs 18:39–18:43 EDT)
- **Harness under test:** machina repo at `2011f3f` (dirty set sha256_16 `02a37cece6247ec9`; `machina/core/agent.py` and `machina/tools/*` stable at this revision)
- **OpenCode:** 2.0.26, agent `build`, model pinned `deepseek/deepseek-flash`
- **Machina:** agent `harness`, authority `full`, model recorded `direct:deepseek-flash` in `llm_usage`
- **Method:** one clean run per task × harness (6 runs, no transients requiring a re-run); each run gets a fresh copy of the git fixture; identical prompt per task across harnesses; 240 s cap with process-group kill; grading = repo-venv pytest (`tests/<file>.py -q`) + sha256 of the whole `tests/` tree before/after the grade. Success = pytest exit 0, ≥1 passed, tests unchanged, not timed out. Nothing was committed; the fixture copies' own `git status` was captured per run (`M filters.py`, `M pipeline.py`, …).

## Fixtures and prompts

| Task | Tests | Bug shape | Baseline |
|---|---|---|---|
| task1 | 3 | midnight rollover in `calc.py` time arithmetic | 3 failed (pilot-shaped; re-run in final matrix) |
| task2 | 5 | two-stage filter/aggregate; boundary-exclusive predicate in `filters.py` **and** divide-by-zero on empty selection in `pipeline.py` | 4 failed / 1 passed |
| task3 | 4 | `datetools.py` helper documented as returning `datetime.date` but returning `str`; `report.py` relies on the string behavior, `export.py` relies on the documented type | 2 failed / 2 passed |

Prompts (identical per task across harnesses, same wording style):

- task1: `Make all tests in tests/test_calc.py pass. Do not modify the tests. Work until pytest passes.`
- task2: `Make all tests in tests/test_pipeline.py pass. Do not modify the tests. Work until pytest passes.`
- task3: `Make all tests in tests/test_expiry.py pass. Do not modify the tests. Work until pytest passes.`

Fixtures were verified before the matrix: naive one-file fixes fail ≥1 test on task2 (filters-only ⇒ 2 empty-input tests fail; pipeline-only ⇒ 2 boundary tests fail); on task3 a naive helper-only fix breaks `report.py`'s tests, and both a two-file fix (helper + report) and a one-file caller fix (export parses the string) pass all 4.

## Consolidated matrix (3 tasks × 2 harnesses)

| Task | Harness | Result | Wall s | Tokens in / out (/reason) = total | Tool calls | Cost USD | Repo rev / status hash | tests/ sha256 unchanged |
|---|---|---|---|---|---|---|---|---|
| task1 | opencode | PASS | 14.2 | 4,192 / 682 / (367) = 5,241 | 9 | $0.001725 | 2011f3f / 02a37cece6247ec9 | yes |
| task1 | machina | PASS | 17.5 | 55,792 / 1,461 = 57,253 | 9 | n/a | 2011f3f / 02a37cece6247ec9 | yes |
| task2 | opencode | PASS | 15.9 | 8,518 / 1,179 / (202) = 9,899 | 11 | $0.002649 | 2011f3f / 02a37cece6247ec9 | yes |
| task2 | machina | PASS | 167.3 | 324,394 / 3,448 = 327,842 | 33 | n/a | 2011f3f / 02a37cece6247ec9 | yes |
| task3 | opencode | PASS | 23.4 | 8,512 / 1,081 / (1,098) = 10,691 | 13 | $0.003123 | 2011f3f / 02a37cece6247ec9 | yes |
| task3 | machina | PASS | 25.5 | 88,022 / 1,724 = 89,746 | 14 | n/a | 2011f3f / 02a37cece6247ec9 | yes |

Test-file sha256 before → after (all identical, grader plus on-disk fixture re-hash):

- task1 `tests/test_calc.py`: `ea99ea4c…50071c3ae` → same
- task2 `tests/test_pipeline.py`: `12c4d67d…f38cee6ed` → same
- task3 `tests/test_expiry.py`: `fd721754…ad516b574` → same

Fixture revisions: task1 `196888d`, task2 `d9c3b5e`, task3 `ca61046`; all three fixture repos `git status --short` = clean after the matrix.

## Aggregates

| Harness | Successes | Median wall | Total tokens (in+out[+reason]) | Cache reads (excl. from total) | Total tool calls | Total cost |
|---|---|---|---|---|---|---|
| opencode | 3/3 | 15.9 s | 21,222 + 2,942 + 1,667 = 25,831 | 516,224 | 33 | $0.00750 |
| machina | 3/3 | 25.5 s | 468,208 + 6,633 = 474,841 | not reported by ledger | 56 | n/a (no cost column for `direct`) |

Observations from the logs (all 6 runs green):

- OpenCode was a tight read → edit → verify loop on every task (2–3 pytest re-runs after the edit); wall 14–23 s, and its task2 was 10.5× faster than Machina's.
- Machina task2 needed 30 iterations / 31 LLM requests / 26 terminal invocations and 167 s: part was environment friction (its terminal tool first tried a Python 3.14 without pytest, then located a usable pytest 3.9 bundle), part careful verification; it fixed both files in one pass and its final summary correctly named both root causes and the untouched test file (md5 unchanged).
- On task3 both harnesses chose the two-file reconciliation (`datetools.py` + `report.py`) — making the helper honor its documented `date` return and adapting the string-dependent caller — rather than the alternate one-file export fix. Tests covering both callers stayed green.
- Machina accounting: ledger `prompt + completion` equals `accounted_tokens` on all three runs (57,253 / 327,842 / 89,746); the in-band `[AUDIT] agent_run_complete` line trails it (55,189 / 323,284 / 87,485, Δ = 2,064 / 4,558 / 2,261).

## Honest caveats

1. **Model mapping is harness-declared, not audited.** OpenCode was pinned to `deepseek/deepseek-flash` and `session_v2` records `{"id":"deepseek-flash","providerID":"deepseek"}`; Machina's `llm_usage` records `direct:deepseek-flash` on all six runs. But Machina's CLI banner prints `Model: deepseek-chat (deepseek)`, its provider default is `deepseek-chat`, and its context-window table has no `deepseek-flash` entry (32k conservative fallback vs 64k for chat). "Same model" is therefore best-effort to layer semantics, not independently verifiable.
2. **OpenCode `cache_read` advantage.** Its 516,224 cache-read tokens are excluded from the headline 25,831 but represent real context volume (~542k total). Cache reads are ~10× cheaper and reflect provider-side prefix caching the harness benefits from; Machina's single `prompt_tokens` figure folds any cache hits into the same number. Token totals and dollars are not like-for-like billing metrics across the two harnesses.
3. **Machina final-call accounting.** We report `llm_usage` sums (the same ledger Machina's own budget checks read), which include the closing call at this revision; the in-band audit line under-reports by roughly 2.1k–4.6k tokens per run (see Δs above). No `cost_usd` exists for Machina's `direct` provider, so cost is only comparable for OpenCode.
4. **Small N.** One run per cell (6 total) and medians of three. No variance estimates; Machina's task2 is heavy-tailed (167 s, 324k prompt tokens) and dominates its aggregates — one re-run could move medians materially.
5. **Single machine, sequential runs.** One macOS host, one run at a time; wall time includes provider network latency and local load. Machina's task2 environment friction (pytest interpreter resolution in its `terminal` tool) is a property of this host, not of the task; grading itself always used the repo venv (Python 3.14 / pytest 9.1.1) and was unaffected.
6. **Task scope and tree state.** Three synthetic fixtures (3–5 tests, 1–2 source files) with no exploration-heavy debugging; the harness tree was `2011f3f` with dirty-set hash `02a37cece6247ec9` (pre-existing docs modifications plus this untracked runner), so provenance is captured but the checkout is not pristine.

## Reproduce the exact matrix

```sh
cd <repo> && bash /private/var/folders/sk/y8w9dwmd4lnc2yvygqqxd4xw0000gn/T/opencode/h2h/run_matrix.sh
```

Equivalent single cell (defaults: `--timeout 240`, OpenCode model `deepseek/deepseek-flash`, agent `build`; Machina agent `harness`; venv `<repo>/.venv`):

```sh
cd <repo> && .venv/bin/python machina/research/head_to_head.py \
  --harness both \
  --task-dir /private/var/folders/sk/y8w9dwmd4lnc2yvygqqxd4xw0000gn/T/opencode/h2h/task2 \
  --prompt "Make all tests in tests/test_pipeline.py pass. Do not modify the tests. Work until pytest passes." \
  --test-path tests/test_pipeline.py \
  --runs-dir /private/var/folders/sk/y8w9dwmd4lnc2yvygqqxd4xw0000gn/T/opencode/h2h/runs \
  --json-out /private/var/folders/sk/y8w9dwmd4lnc2yvygqqxd4xw0000gn/T/opencode/h2h/final-task2.json
```

## Artifacts (scratch, not committed)

- `final-task1.json`, `final-task2.json`, `final-task3.json` — full per-run reports (tokens, tools, sessions, provenance, sha checks)
- `runs/<task>/<harness>/` — post-run working copies; `runs/<task>/<harness>.log` — raw subprocess output
- `task1/ task2/ task3/` — pristine git fixtures (clean, test hashes as listed above)
- `prompts/task1.txt … task3.txt`, `run_matrix.sh`, `aggregate.py`, `pilot-report.json` (superseded by the task1 re-run)
