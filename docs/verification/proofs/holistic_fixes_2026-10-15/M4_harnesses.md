# LANE M4 — holistic-review fixes: H09 (harness argv) + H22 (UI/backend deadline)

Date: 2026-10-15 · Repo: `/Users/shadow/machina` · Findings source:
`docs/verification/CODEBASE_REVIEW_HOLISTIC_2026-10-15.md` (§H09, §H22).

Owned files (only): `machina/interop.py`, `machina/gateway/features/interop.py`,
`ui/src/features/harnesses.ts`, `ui/src/app.ts`, and their tests.
No commit, no push.

---

## H09 — delegated harness prompts can become command options (P2)

### Reproduced? YES

`docs/.../logs/H09_repro_before.log` (probe loads `HEAD:machina/interop.py`,
runs against stub CLIs whose argv parsers emulate the installed binaries;
prompt `--help`):

| platform | argv sent (before) | result |
| --- | --- | --- |
| opencode | `opencode run --help` | stdout = `OPENCODE HELP (usage text, exit 0)`, `exit_code=0`, **`ok=True`** — no task ran |
| codex | `codex exec --help` | stdout = `CODEX HELP (usage text, exit 0)`, `exit_code=0`, **`ok=True`** — no task ran |
| hermes | `hermes -z --help` | `exit_code=2`, `ok=False`, `argument -z/--oneshot: expected one argument` — a legitimate prompt cannot be delegated at all |

Nuance (correction to the finding's one-size wording): for the verified
installed binaries the false-success signature holds for opencode and codex;
hermes/argparse refuses an option-like value after `-z` (a spurious usage
error, not a false success). The root cause — the prompt is a bare argv token
— is the same on all three.

Host-level verification that was done read-only (no real tasks were run):
the opencode v2.0.26 bundle's parser (`na()` splits argv at the first `--`,
`yo()` appends trailing operands to `run`'s variadic message), `codex exec
--help` (“If not provided as an argument (or if `-` is used), instructions are
read from stdin”), and the real hermes argparse parser invoked in-process
(`build_top_level_parser().parse_args(["-z","--help"])` → SystemExit 2;
`["--oneshot=--help"]` → `oneshot='--help'`; its pre-argparse `command_argv`
scanner skips `=`-forms without treating the value as a subcommand).

### Fix

`run_task()` now delivers the prompt through a per-platform *literal*
mechanism; no prompt is ever appended as a bare argv token:

| platform | mechanism | spawned argv |
| --- | --- | --- |
| opencode | `--` option terminator (trailing operands become `run`'s variadic message) | `<bin> run -- <prompt>` |
| codex | documented stdin form (`-`) | `<bin> exec -`, prompt on stdin |
| hermes | argparse `=` binding (`--oneshot=<prompt>`) | `<bin> --oneshot=<prompt>` |

New helper `_literal_prompt_invocation()` returns `None` for a run-capable
platform without a mechanism; `run_task()` then returns a **typed refusal**
(`ok=False`, `unsupported_reason=...`) and never spawns — the correction's
“typed validation error” arm, forward-proofed by test.

Second half of the correction (“distinguish task completed from command
exited 0 as far as the protocol allows”): the child protocol exposes only
exit code/output, so `ok` stays `exit_code == 0 and not timed out`; what
changed is that literal delivery removes the option-interpretation path by
construction, and that is documented on `run_task`/`RunResult`. A
zero-exit help screen can no longer masquerade as a completed task because
the prompt cannot select the help flag.

### Files

- `machina/interop.py` — `RUN_PLATFORMS` values/comments, `_RUN_DESCRIPTIONS`,
  new `_literal_prompt_invocation`, `run_task`, `RunResult` docstring.
- `tests/test_harnesses_core.py` — stub-CLI tests + updated expectations.

### Tests

- `test_run_task_delivers_option_like_prompts_literally` — **executable
  stubs on PATH** for all three platforms; asserts the stub-captured argv is
  `run -- --help` / `exec -` (stdin `--help`) / `--oneshot=--help` and that
  each receives `TASK: --help`. RED pre-fix (opencode/codex: help text with
  `ok=True`; hermes: exit 2), GREEN post-fix.
- `test_run_task_codex_prompt_goes_on_stdin_and_never_touches_argv`
- `test_run_task_refuses_a_platform_without_a_literal_prompt_mechanism`
- Updated: `test_run_task_builds_the_documented_headless_command_and_is_typed`
  (opencode `--`, `stdin_text is None`),
  `test_run_task_not_installed_is_typed_and_never_spawns`
  (`["hermes", "--oneshot=x"]`).

### Callouts (public-shape/behavior changes)

- `RUN_PLATFORMS["hermes"]` value changes from `["hermes", "-z"]` to
  `["hermes"]` (the one-shot flag now carries the prompt). Key set unchanged
  (`== set(PLATFORMS)`), and the frozen `RunResult` field set is unchanged.
- `RunResult.command` shapes change for all three platforms
  (`run -- <prompt>`, `exec -`, `--oneshot=<prompt>`); for codex the prompt is
  intentionally **not** in argv (stdin), so the command display no longer
  echoes it.
- `supported_runs()` hint strings change (UI `runHint`): e.g.
  `codex exec - (prompt on stdin) …`.
- CLI (`machina harnesses run`) exit-code mapping is unchanged.

---

## H22 — harness UI times out before the backend execution deadline (P2)

### Reproduced? YES

`docs/.../logs/H22_ui_red.log` (fake timers, real `JsonRpcChannel`): with
`interop.run {timeout: 300}` in flight, ticking **120 000 ms** rejected with
`gateway did not answer interop.run within 120000 ms` — the client abandoned
an RPC whose backend deadline is 300 s. `H22_ui_red.log` also shows the
harnesses feature sending `interop.run` with `{platform, prompt}` only (no
deadline coordinated; the gateway defaulted to 300 s server-side).
`H09_H22_python_red.log` shows `interop.list` did not surface any deadline.

### Fix (deadline aligned across both sides; smallest safe; backend never shortened)

1. Backend surfaces the authoritative number (single source:
   `DEFAULT_RUN_TIMEOUT_SECONDS = 300.0`):
   `interop.list` now returns `runTimeoutSeconds`.
2. `/harnesses` reads it from the list payload and passes it as the explicit
   `timeout` param of `interop.run`, so the backend applies exactly the
   deadline the client knows about. If the gateway does not report a deadline
   (payload field missing/malformed), the feature **refuses to start** the
   task with a warning (includes the shell alternative) rather than launching
   a host-accessing process its clock could abandon.
3. `App.requestTimeoutMs()` sizes the client clock for `interop.run` as
   `timeout * 1000 + HARNESS_RUN_GRACE_MS` (15 s wind-down grace for
   kill-tree + drain + redaction), so a late-but-successful result still
   lands. Other methods keep the existing budgets (`agents.run` 150 s,
   generic 120 s); the backend deadline is unchanged (300 s default, ≤ 900 s
   when explicitly requested).

### Files

- `machina/gateway/features/interop.py` — `interop_list` returns
  `runTimeoutSeconds`.
- `ui/src/features/harnesses.ts` — reads `runTimeoutSeconds` (local
  intersection type), threads it to `runTask`, passes `timeout` on
  `interop.run`, refuses when absent.
- `ui/src/app.ts` — `requestTimeoutMs()` + `HARNESS_RUN_GRACE_MS`, used by
  `FeatureHost.request`.
- `tests/test_harnesses_feature.py`, `ui/test/harnesses.test.ts`,
  `ui/test/app.test.ts`.

### Tests

- Python: `test_interop_list_surfaces_the_authoritative_run_deadline` (the
  surfaced value equals both the list field and the timeout the run handler
  applies); updated exact-payload tests.
- UI feature: run-a-task asserts
  `interop.run {platform, prompt, timeout: 300}`; new refusal test when the
  deadline is absent (no `interop.run`, warning printed).
- UI app (fake timers, real channel): at 120 s and at 300 s the request is
  still pending; a reply at 300 s resolves it (old code rejected at 120 s);
  companion test pins that a silent backend still ends via the client clock.

### Callouts

- Wire addition: `interop.list` payload gains `runTimeoutSeconds` (number,
  seconds). **Out-of-ownership follow-ups** (reported, not touched):
  - `ui/src/protocol.ts`: add `runTimeoutSeconds: number` to
    `HarnessListResult` (harnesses.ts currently reads it through a local
    intersection type).
  - `communications/ui-gateway-protocol.md`: document the new field.
- New UI behavior: `/harnesses → Run a task` refuses to start when the
  gateway does not report a deadline (impossible against a same-revision
  gateway; the guard exists so the bug cannot silently return on drift).
- Cancellation of an in-flight run stays the backend's deadline (plus H21's
  parallel dispatch lane); no new cancel RPC was added.

---

## Verification (final state)

| check | command | result |
| --- | --- | --- |
| Python focused (H09/H22 + adjacent) | `pytest tests/test_interop.py tests/test_harnesses_core.py tests/test_harnesses_feature.py tests/test_features_split.py tests/test_fm7_cli.py tests/test_final_review_cli_config_setup.py tests/test_installer_env.py -p no:cacheprovider` | **171 passed** (`logs/H09_H22_python_green.log`) |
| Python RED (pre-fix) | same core/feature pair | 8 failed, 25 passed (`logs/H09_H22_python_red.log`) |
| UI focused | `node --test test/harnesses.test.ts test/app.test.ts` | **28/28 pass** (`logs/H22_ui_green.log`); RED pre-fix (`logs/H22_ui_red.log`) |
| UI full | `cd ui && npm run check` (typecheck + 506 tests + eslint) | **exit 0**, 500 pass / 6 skipped / 0 fail (`logs/ui_npm_check_green.log`) |
| Lint/format | `ruff check` + `ruff format --check` on the four Python files | clean (`logs/ruff_green.log`) |

RED-first evidence: the regression tests above were committed to the working
tree before the fixes and failed exactly as the findings describe; the
RED logs are saved alongside the green ones.

### Non-findings noted (not mine)

- `tests/test_architecture_layers.py` currently fails 3 tests
  (`copilot.py` ceiling, provider-package total, deferred-import ratchet
  5015 > 5003). These are concurrent lanes' working-tree edits
  (`core/provider/copilot.py`, `memory/`, `gateway/__main__.py`, …);
  my changed files add **zero** deferred imports (counts: interop.py 2 → 2,
  gateway/features/interop.py 5 → 5) and no provider lines.
- `machina/cli/interop.py` (not owned) prints `RunResult.command`; for codex
  this now shows `codex exec -` without the prompt (the prompt is on stdin).
  No change requested — the run output still carries the task result.
