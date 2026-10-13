# Proof — head-to-head matrix summary (Machina vs OpenCode, same DeepSeek model)

- **Date observed:** 2026-10-13, runs 18:39–18:43 EDT
- **Status:** both harnesses **3/3 PASS** with test files hash-intact
- **Provenance:** `docs/verification/HEAD_TO_HEAD_2026-10-13.md` (full report)
  and `docs/verification/COMPLETION_BUNDLE.md` section 10 bullet
  "Head-to-head harness comparison".
- **Harness revision:** machina repo at `2011f3f`, dirty-set sha256_16
  `02a37cece6247ec9`; OpenCode 2.0.26 agent `build`, model pinned
  `deepseek/deepseek-flash`; Machina agent `harness`, authority `full`,
  ledger model `direct:deepseek-flash`.

## Matrix (3 tasks × 2 harnesses)

| Task | Harness | Result | Wall s | Tokens in / out (/reason) | Tool calls | Cost USD | tests/ sha256 unchanged |
|---|---|---|---|---|---|---|---|
| task1 | opencode | PASS | 14.2 | 4,192 / 682 / (367) = 5,241 | 9 | $0.001725 | yes |
| task1 | machina | PASS | 17.5 | 55,792 / 1,461 = 57,253 | 9 | n/a | yes |
| task2 | opencode | PASS | 15.9 | 8,518 / 1,179 / (202) = 9,899 | 11 | $0.002649 | yes |
| task2 | machina | PASS | 167.3 | 324,394 / 3,448 = 327,842 | 33 | n/a | yes |
| task3 | opencode | PASS | 23.4 | 8,512 / 1,081 / (1,098) = 10,691 | 13 | $0.003123 | yes |
| task3 | machina | PASS | 25.5 | 88,022 / 1,724 = 89,746 | 14 | n/a | yes |

Aggregates as recorded: OpenCode 3/3, median 15.9 s, 25,831 total tokens
(excluding 516,224 cache reads), 33 tool calls, $0.00750 total; Machina 3/3,
median 25.5 s, 474,841 total tokens (468,208 in + 6,633 out, no cost column
for `direct`), 56 tool calls. Test-file sha256 before → after identical for
all six runs; fixture copies clean after the matrix. Details, per-run
provenance and the six honest caveats (model-mapping best-effort, cache-read
asymmetry, ledger-vs-audit deltas, small N, single machine, synthetic scope)
live in the source report.

## Why this is recorded here

This pairing is the provenance for `tools/e2e_gate.py`'s design choice:
acceptance comes from real observed outcomes — in the matrix, success was
graded by the repo venv pytest plus a hash check of the whole `tests/` tree
before/after, not by trusting a harness's own "done" message.
