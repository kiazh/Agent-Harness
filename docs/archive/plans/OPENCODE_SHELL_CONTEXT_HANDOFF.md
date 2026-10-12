# OpenCode continuation: shell approvals and model context capacity

Updated 2026-10-11. This is unfinished work, not a completion claim.

## Start here

Continue in `/home/user/Agent-Harness1`, branch `main`, HEAD at handoff
`b33b8eb changes`. Preserve all existing uncommitted changes. Run and record
`git status --short`, `git log --oneline -3`, `git branch --show-current` first.
Do not commit, push, deploy, change git configuration, reset files, or edit the
operator's home/configuration/credentials/shell startup files/terminal.

Read `AGENTS.md` if present, this file, and
`docs/archive/plans/SHELL_AND_CONTEXT_LIMITS_PLAN.md`. Inspect the actual diff before
editing. The changes below are already in the working tree; do not recreate
them from scratch or discard them because they are uncommitted.

The user wants this completed end to end. Do not stop at a plan. Finish the two
review defects, verify, and document. No live provider calls or paid model use
are required. Other UI/MCP/persona work is outside this continuation's scope.

## User's exact problem

The agent attempted:

```text
echo "shell ok" && uname -s && date -u +%Y-%m-%dT%H:%M:%SZ && pwd
Error: Command contains dangerous characters: '&'
echo "shell ok"; uname -s; date -u; pwd
Error: Command contains dangerous characters: ';'
```

Individual commands succeeded, but the run then ended with:
`Reached maximum token budget. Partial results may be available.`

The user wants valid shell commands to execute, with dangerous actions going
through approval rather than being categorically rejected after approval.
They also want model context capacity (approximately one million for their
chosen model), not an arbitrary cumulative 50,000-token stop or hidden 8,000
token prompt budget. Never assume every model has a million-token context.

## Implemented, already tested, but not ready to declare complete

### Shell

- New `ah/permissions/shell.py` shares parsing between permission proposal and
  terminal execution. Simple commands retain literal argv. Shell grammar uses
  explicit `/bin/sh -c` and exact raw payload, with `shell=False` at subprocess
  creation. No login or interactive shell.
- Recognized literal stages joined by `&&`, `||`, `;`, newline or pipe may be
  routine only when **every stage** passes existing routine-project policy.
  Sensitive later stages cannot inherit permission from a harmless first stage.
- Expansion, redirects, backgrounding, compound syntax and unknown commands
  require broker approval. Quoted literal metacharacters remain data.
- Terminal no longer rejects approved commands merely because they contain
  `&`, `;` or approved options such as `git -c`. Shell execution requires a
  broker snapshot matching exact argv and shell payload; changed commands cannot
  reuse that snapshot. Unbrokered direct calls retain narrow restrictions.
- Existing cwd restrictions, secret-filtered environment, sandbox execution,
  timeout/output bounds and process-tree cancellation remain.
- Changed permission files: `tools.py`, `commands.py`, `policy.py`, `broker.py`;
  execution file: `ah/tools/terminal.py`.
- New `tests/test_shell_commands.py` executes actual safe subprocesses inside
  temporary workspaces and tests sensitive stages, approval, redirects, identity
  mismatches, literal metacharacters and approved options.

### Context

- Removed `MAX_TOKEN_BUDGET=50_000` and the cumulative-spend turn cutoff.
  Cumulative parent/delegated tokens still count for accounting. Explicit user
  usage-ledger limits remain enforced.
- Before ordinary and streamed provider requests, estimate current messages,
  tool schemas, framing and reserved generated output against model capacity.
  Actual overflowing requests stop before provider execution with a context
  overflow message. Repeated small requests may accumulate usage above capacity.
- Prompt assembly and compaction/display now use model capacity instead of the
  legacy 8k setting. `context_budget` remains for configuration/storage
  compatibility. `Session.context_window` is derived runtime state, not stored
  authority.
- `LLMProvider.context_window()` supports fallback resolution;
  `OpenRouterProvider.context_window()` reads `/models` with a bounded timeout,
  validates positive integer `context_length`, and caches metadata. Failure
  logging contains exception type only; no response bodies or keys.
- Keep API generated-output limits separate from simultaneous context capacity.
  Do not set `max_tokens` to one million. Existing configured response limits and
  provider validation are unchanged.
- Context files: `ah/core/{agent,context_windows,models,provider,config}.py`,
  `ah/services.py`, `ah/gateway/features/sessions.py`, `ah/api/app.py`,
  `ah/cli/__init__.py`.
- New `tests/test_model_context_limits.py`: metadata caching/offline fallback,
  cumulative usage beyond old cap, true current-request overflow in both paths,
  real safe-shell agent round trip and long prompt preservation.
- Adjusted existing context, delegation, failed-delegation and review-round2
  tests to verify behavior instead of the removed constant. Failed delegated
  spend still counts; unavailable explicit usage ledger still fails closed.

## BLOCKERS: two Important review findings still unfixed

Write a failing regression for each, demonstrate failure, fix, then rerun.

1. **Wrong model selected for context sizing.**
   `_turn_model(session, provider)` in `ah/core/agent.py` prefers stored
   `session.model` over the actual selected `provider.model`. Scheduler/factory
   supports `model_override`. A saved Gemini session with a codellama provider
   override currently resolves 1,048,576 instead of 16,384. Reviewer demonstrated
   an approximately 24,134-token request incorrectly passing the large limit.
   Prefer the actual provider-selected model; use session/config only as fallback.
   Update the existing test explicitly expecting session priority in
   `tests/test_context_windows.py`. Add streamed and ordinary override coverage
   proving an oversized codellama request never reaches the provider.
   Consider tagging ephemeral resolved capacity with the model so stale
   `session.context_window` cannot survive a model change. Do not reuse capacity
   blindly when provider/model changes. Recompute per turn as necessary.

2. **OpenRouter metadata leaks across providers.**
   `context_window_for(model)` currently consults global `_provider_windows`
   regardless of provider. Reviewer populated a one-million OpenRouter catalog
   entry, then a base/local provider with the same ID received one million
   instead of its 32,000 fallback. Scope metadata lookup to provider/service.
   Base providers must not inherit OpenRouter-specific serving capacity.
   An explicit provider argument or provider-scoped cache is suitable; choose
   the smallest clear implementation. Keep OpenRouter's own metadata resolution.
   Audit every caller (agent, services, API, gateway, CLI) so provider identity
   is passed where available. Session/config fallback must not override the
   actual live provider. Tests use mocked HTTP, never live credentials.

The read-only reviewer found no Critical/Important shell grammar or approval
snapshot bypass in the inspected paths. This is not a substitute for final
verification. Optional follow-up: ensure literal tokenizer special tokens in
user/tool text cannot crash new request estimation; add regression first if
reproduced. Avoid unrelated refactors or speculative permission weakening.

## Verification evidence at handoff

- Initial regressions reproduced the old defects (19 failed / 1 passed); later
  focused integration suites passed **174 tests** in 3.03s. Logs:
  `/tmp/ah-shell-context-red.log`, `/tmp/ah-model-metadata-red.log`,
  `/tmp/ah-shell-context-integration.log`.
- Full backend run just finished: **2142 passed, 5 skipped in 83.41s**.
  `/tmp/ah-shell-context-backend.log`. This was before fixing the two reviewer
  findings, so it is a baseline for this working tree, not final acceptance.
- Native UI log `/tmp/ah-shell-context-ui.log`: **251 passed, 5 skipped, zero
  failures**, followed by ESLint invocation. Confirm command exit before claiming
  complete check. No UI files changed in this continuation.
- Ruff formatting ran across touched Python files. Final Ruff check/format,
  Bandit and `git diff --check` still need running after remaining fixes.
- No final README/handoff/verification update yet for these changes.

## Test isolation: mandatory

Use `.venv/bin/python -m ...`; pip's shebang is stale. Only test database:

```text
postgresql://postgres:postgres@localhost:5432/agentharness_test
```

Never access the production `agentharness` database. Do not run pytest with the
operator's normal home or live key configuration. Existing isolation bootstrap:
`/tmp/agentharness-ui-reference-runtime/sitecustomize.py`. Inspect it before use.
It redirects `Path.home()`/`expanduser()` to disposable state, uses a blank env
file, clears API-key environment variables, and sets database variables to the
dedicated test DSN. It preserves temporary HOME values used by child tests.
It initializes the test database even for focused suites; **run backend pytest
suites sequentially**, never concurrently against the shared test DB.

```sh
AGENT_HARNESS_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/agentharness_test \
PYTHONPATH=/tmp/agentharness-ui-reference-runtime:/home/user/Agent-Harness1 \
.venv/bin/python -m pytest \
tests/test_shell_commands.py tests/test_model_context_limits.py \
tests/test_context_windows.py tests/test_review_round2_fixes.py \
tests/test_delegation_boundaries.py tests/test_failed_delegation_usage.py \
-q --tb=short -p no:cacheprovider
```

After focused tests, run the full backend suite with the same isolated env:

```sh
AGENT_HARNESS_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/agentharness_test \
PYTHONPATH=/tmp/agentharness-ui-reference-runtime:/home/user/Agent-Harness1 \
.venv/bin/python -m pytest -q --tb=short -p no:cacheprovider
npm --prefix ui run check
```

If `/tmp` bootstrap is missing, recreate equivalent isolation before importing
the application. Do not omit isolation to make tests run. Set all relevant DB
variables to the test DSN, blank key/env storage, redirect home to a disposable
directory, and inspect `tests/conftest.py` startup behavior.

Run `.venv/bin/python -m ruff check` and `ruff format --check` on all touched
Python files, `.venv/bin/python -m bandit -r ah/permissions/ ah/tools/terminal.py`
(inspect any findings, no new medium/high), and `git diff --check`. Inspect
untracked Python files too: ordinary `git diff --name-only` omits new files.

For any regression: reproduce on a separate clean checkout/worktree of current
HEAD `b33b8eb`; use the root venv interpreter with that checkout's root in
PYTHONPATH plus isolation bootstrap. Never reset this working tree. Old managed
worktrees at `27d1`/`38df` are older commits, not suitable evidence for b33b8eb.
Property tests have previously exhibited 200ms deadline timing flakiness; prove
on clean HEAD and report rather than silently relaxing a deadline.

## Finish documentation and report

1. Replace README's obsolete quarter-window / 8k floor / 50k ceiling / explicit
   context-budget-wins description. Explain current-request capacity, provider
   metadata and conservative fallback, cumulative accounting, and separate
   generated-output/user-defined usage limits.
2. Append dated `docs/HANDOFF.md` entry with actual final files, commands/results,
   approval boundaries, metadata provider scoping and any limitations.
3. Record final evidence in a verification document (for example
   `docs/verification/SHELL_AND_CONTEXT_LIMITS_REVIEW.md`). Do not claim live
   million-token inference: metadata tests use mocked HTTP and agent tests use
   fake providers with actual temporary-workspace shell execution.
4. Mark plan complete only after reviewer defects and required checks pass.
5. Final reply: what changed, focused/full/UI/lint/security results, any proven
   pre-existing failures, and meaningful limitations. Do not claim everything
   fixed solely because the current pre-review full suite is green.

The previous committed UI/persona self-knowledge work is separate. MCP remote
agent-loop dispatch, OAuth and other previously documented deferred work remain
deferred. Do not expand this handoff into rebuilding those features.
