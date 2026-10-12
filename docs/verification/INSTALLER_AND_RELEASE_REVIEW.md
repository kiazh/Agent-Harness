# Installer and release review

Date: 2026-10-11. All commands executed in the repository checkout described
below; no live credentials, shell startup files, production databases, or the
operator terminal were mutated by tests (isolated tmp dirs, stubbed commands,
isolated pseudo-terminals, disposable `machina_test` database).

## Revision and environment

- Commit: `dc77017` ("profile"), branch `main`, up to date with `origin/main`
  at review start. No `AGENTS.md` exists in the checkout (the review plan
  asked to read it first; recorded as absent).
- Working tree at review time:
  - `install.sh` modified (Part 1 fixes below).
  - New untracked: `tools/install-helpers.sh`, `tests/test_installer_env.py`
    (`tools/` is tracked; the helpers were deliberately placed there because
    `lib/` is gitignored).
  - One existing test touched: `tests/test_integration_permissions.py`
    (`test_loopback_and_reuse` follows moved code; same assertions).
- Interpreter/toolchain: Python 3.14.5 (venv), Node v26.10.0, psql 16.15
  (Homebrew), GNU bash 3.2.57 (the shell that runs installer tests —
  lib code is restricted to POSIX + bash-3.2 constructs).
- Services: local PostgreSQL 16 with `machina` and `machina_test`
  databases reachable; **no Docker** on this machine.
- Official MCP Python SDK index state: `mcp` 2.3.0 latest (1.0.0–2.3.0
  listed); not yet installed at the initial installer review. The subsequent
  connector evidence below records the installed version and actual wire probes.

## Part 1 — installer defects (all three confirmed, fixed, regressed)

Changed files: `install.sh`, `tools/install-helpers.sh` (new),
`tests/test_installer_env.py` (new, 30 tests), `tests/test_integration_permissions.py`.

The five provisioning/shell functions (`try_init`, `start_docker_db`,
`start_brew_db`, `brew_vector_from_source`, `wait_for_init`) moved verbatim
into the side-effect-free lib (one intentional behavior change each, noted
below); `install.sh` sources the lib after `cd` and resolves the env target
once. Regressions drive the real lib functions with stubbed commands and an
isolated pty — never the operator terminal or database.

### 1. Custom DATABASE_URL clobbered by Homebrew fallback — FIXED

- Root cause: `start_brew_db` stripped `DATABASE_URL` and wrote generated
  credentials whenever it created the role, unlike the Docker branch which
  guarded on unset/default (`install.sh` ex-398–409).
- Fix: both branches route through `maybe_write_db_url`, which replaces
  only an unset URL or the documented installer placeholder
  (`postgresql://postgres:postgres@localhost:5432/machina`); custom
  URLs are preserved with a warning. Existing roles still never get password
  resets (unchanged `ALTER USER` gating).
- Regressions: custom+new-role unchanged; default/unset replaced;
  existing-role password untouched (asserted via ALTER call log);
  provisioning failure returns nonzero with byte-identical config.
- Red/green: `test_brew_custom_url_survives_new_role` fails against a
  copy of the lib with the guard reverted to the old unconditional write
  (via `MACHINA_INSTALL_LIB` override), passes against the fixed lib.

### 2. MACHINA_ENV_FILE ignored by install.sh — FIXED

- Root cause: the Python resolver honored `MACHINA_ENV_FILE` while `install.sh`
  hardcoded `.env` for creation, secrets, and both DB branches.
- Fix: `resolve_env_file` runs once (absolute wins; relative anchors to the
  startup directory so the later `cd` cannot redirect it; unset means the
  checkout `.env`); every read/write/chmod/replacement uses the resolved
  target with same-directory atomic replace and mode 0600.
- Regressions: unset/absolute/relative overrides; invocation outside the
  checkout; spaces/Unicode in paths and values; gap-filling preserves
  unrelated values; invalid keys, missing dirs, and directory targets fail
  closed with no stray temp files.
- Red/green: wiring assertions (`sources helpers`, `maybe_write_db_url`
  call sites, no hidden-typing fallback, no bare `.env` file ops) fail on
  HEAD `install.sh` (verified via `git stash`) and pass after the fix.

### 3. Visible secret typing fallback — FIXED

- Root cause: `stty` state-query failure fell back to an unprotected read,
  and echo-disable failure was ignored (`|| true`).
- Fix: `prompt_secret_hidden` fails (no output, nonzero) unless `/dev/tty`
  opens, `stty` state is queryable, and echo actually disables; the prompt
  prints only after hiding is on; echo restores on success, EOF, error, and
  INT/TERM/HUP/EXIT (interruption re-raises to die by signal). No visible
  fallback exists anymore; failure skips with secure instructions.
- Regressions (isolated pty unless noted): hidden round-trip with
  echo-absence proof (secret appears exactly once); no-tty failure;
  state-query failure; echo-disable failure; hangup restores echo and dies
  by signal. A direct-SIGINT variant was abandoned with cause: in this
  spawner a pty session leader swallows direct SIGINT, so the test would
  assert spawner-specific semantics; kernel hangup is deterministic.
- Note: the shipped `bash` here is 3.2 — after a trapped signal during
  `read`, bash re-blocks, which is why interruption re-raises instead of
  returning.

## Part 2 — exact-checkout baseline

- Full suite with the test database:
  `1965 passed, 5 skipped` (DB-gated skips), **0 failed**.
- One real defect found by the baseline: an existing static test
  (`test_loopback_and_reuse`) grepped `install.sh` for moved code; updated
  to scan both files with identical assertions.
- Historical snapshots (README 1844/9, DEBUGGING_REPORT, ledger 1702/0)
  describe older checkouts, not this one. The nine previously recorded
  failures were each reproduced on a clean worktree and fixed via test
  hermeticity (live-config isolation) or were environment-missing DB
  fixtures; none were open implementation defects at review time.

## Part 3 — release verification (exact commands and results)

```sh
pytest tests/ -q --tb=short --cov=ah --cov-fail-under=60
# 1965 passed, 5 skipped in ~113s. TOTAL 16282 stmts, 3036 missed: 81.35% (>= 60 gate).
ruff check machina/                                  # All checks passed (133 files).
ruff format --check machina/                         # 133 files already formatted.
python -m bandit -r machina/ -ll -x machina/cli/          # 0 medium/high severity (42 low, nosec-annotated sites).
npm --prefix ui run check                       # typecheck + 132 passed/5 skipped + eslint, all green.
git diff --check                                # clean.
```

- Real integration exercised (not mocked): UI/gateway over stdio JSON-RPC
  (137 UI tests incl. layout on a real alt-screen), PostgreSQL-backed
  suites against disposable `machina_test`, installer functions
  against stubbed OS commands, fresh non-editable install (wheel built
  with hatchling, installed into a disposable Python 3.14 venv: 42-skill
  bundle hash verified, agent + context-window modules importable).
- Approval-resume, fencing, cancellation, migration, and redaction suites
  are part of the green full run above.
- Blockers/limitations (not verified, not claimed):
  - No Docker on this machine: image build and Compose readiness BLOCKED.
  - Linux/Windows installer behavior: code restricted to portable
    constructs but executed only on macOS bash 3.2 — BLOCKED elsewhere.
  - No live-provider calls and no real MCP service integrations (opt-in
    only by policy); mock/SDK-backed connector evidence lives in
    `docs/archive/plans/MCP_CONNECTORS_PROGRESS.md`.
  - Staging alerts, backup/restore, rollback, deployment gates: open
    unless verified with authorization (unchanged from the ledger).

Do not claim zero bugs, complete security, or production readiness from
unit tests alone. The release gates in
`docs/archive/plans/PRODUCTION_COMPLETION_PROGRESS.md` remain authoritative.

## Part 4 — subsequent MCP connector library verification (2026-10-11)

Evidence and exact commands are maintained in
[MCP_CONNECTORS_PROGRESS.md](../archive/plans/MCP_CONNECTORS_PROGRESS.md). The existing
installer changes were preserved. Checkout remains `main` at `dc77017`; no
commit/push/deployment or git configuration changes were made.

- `mcp==2.3.0` is now installed and pinned as the optional `connectors` extra.
  Real stdio and loopback Streamable HTTP fixture handshakes both negotiated
  `2025-11-25`; the SDK's latest constant is not treated as negotiated proof.
- A disposable import-block probe simulated an absent optional SDK: 59 tests
  passed and the real-client module skipped cleanly (1 skipped). The installed
  virtualenv was not modified by this probe.
- The original cross-task SDK shutdown error was reproduced. The fixture process
  exited before the scope error (PIDfile, `os.kill(pid, 0)` polling, UUID-matched
  `ps` inspection), but repeated connections could hang. Persistent task
  ownership now covers context entry and teardown; six fixture cycles across
  same/cross-task disconnects leave no matching processes or scope warnings.
- A separate gated-cleanup test verifies that shutdown timeout preserves the
  tracked owner and blocks overlapping reconnect. Arbitrary hostile subprocess
  tree termination at the caller's deadline is not claimed.
- Actual fixture wire tests cover normal calls, timeout/dropped-call unknown
  outcomes without retry, bounded normalized results, known server errors,
  secret redaction, and actual elicitation rejection with no model callback.
  Config/policy and injected-client tests cover retries/circuits and service
  contracts. These are not a broker-integrated agent execution test.

```sh
.venv/bin/python -m pytest -q tests/test_connectors_client.py tests/test_connectors_config.py tests/test_connectors_policy.py tests/test_connectors_manager.py tests/test_connectors_service.py --tb=short -p no:cacheprovider
# 84 passed in 8.58s.
MACHINA_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/machina_test .venv/bin/python -m pytest -q --tb=short -p no:cacheprovider
# Final: 2049 passed, 5 skipped in 78.27s; 0 failed.
npm --prefix ui run check
# Exit 0: typecheck, 132 tests passed / 5 skipped, eslint.
# UI gateway/DB tests require an explicit opt-in environment and were skipped.
.venv/bin/python -m ruff check machina/connectors tests/test_connectors_client.py tests/test_connectors_config.py tests/test_connectors_policy.py tests/test_connectors_manager.py tests/test_connectors_service.py tests/support/mcp_fixture_server.py
# All checks passed.
.venv/bin/python -m ruff format --check machina/connectors tests/test_connectors_client.py tests/test_connectors_config.py tests/test_connectors_policy.py tests/test_connectors_manager.py tests/test_connectors_service.py tests/support/mcp_fixture_server.py
# 13 files already formatted (line-length 100).
.venv/bin/python -m bandit -r machina/connectors/ -ll
# Exit 0: 0 medium/high. Unfiltered run reports 7 low and exits 1.
git diff --check
# Exit 0.
```

No verification failures required a clean-HEAD comparison. Tests used only the
dedicated test database and disposable SDK fixtures. No third-party MCP service,
OAuth flow, live-provider call, connector management UI, or agent-loop/broker
connector execution was exercised. Those phases remain deferred; the earlier
Docker, operating-system, packaging, and production gates are not superseded.

## 2026-10-11 — Persona RSI runtime verification

The shared agent lifecycle now implements durable runtime adaptation for persona,
memory retrieval, task strategy, relevant skills and available-tool ordering.
It defaults on without new provider requests or dependencies. Startup/initialization
and CLI management use the existing additive growth migration. Session-bound
gateway and CLI operations expose status/history and restoration, including
baseline revision 0; the TUI opens an interactive action menu.

Evidence and exact commands are in
[PERSONA_RSI_PLAN.md](../archive/plans/PERSONA_RSI_PLAN.md). Final backend suite:
**2092 passed / 5 skipped**; dedicated RSI suite: **43 passed**. These include
real PostgreSQL, actual CLI subprocesses, synthetic-provider agent loops in both
modes, real subprocess exit-status evidence, task cancellation, and Node/Python
gateway restart/history/restoration checks. Standalone UI check passed typecheck,
**138 tests / 5 environment-gated skips**, and lint. Ruff check/format passed on
16 Python files. Bandit reported no findings in the RSI/growth modules. The
scoped Impeccable detector reported no findings; `git diff --check` passed.

The new method-inventory test regression was fixed. An overlapping database-test
migration conflict disappeared in sequential final checks. Both affected tests
passed against clean HEAD in a disposable detached worktree (2 passed); no
unresolved failure remains in these checks. Fresh independent review defects
were reproduced with regressions and fixed, including human-choice preservation,
typed failures and policy-update provenance across restoration/import.

Only the dedicated test database was used. These checks establish tested runtime
behavior, not a comparative quality/speed gain, production deployment, LLM weight
training, or unattended source modification. Connector agent-loop exposure,
OAuth/UI and the existing release/production gates retain their separate status.


## 2026-10-11 — Reference TUI and read-only connector inspection

Implemented the supplied screenshot’s plain messages, restrained framed tools,
transcript approval evidence/vertical dock, one footer, thin composer and calm
single-cell 600 ms activity. Static growth acknowledges evidence briefly with a
30 s cooldown. Native responsive and approval/focus/queue/overflow regressions are
in `ui/test/reference.test.ts`; motion and turn-state tests preserve ownership and
truthful outcomes. Independent review findings were reproduced red, fixed and
confirmed in a bounded re-review. Preview: `../archive/artifacts/tui-reference.png`.

Read-only `mcp.list`/`mcp.status` now feed `/mcp` from one lazy worker-owned
ConnectorService. Inspection does not start DB/SDK, launch/discover or mutate.
Authenticated RPC/input/error contracts and shared sanitized state are tested;
actual gateway stdio and real SDK connection/PID cleanup establish live inspection
and owned/borrowed lifecycle behavior. The optional SDK remains lazily imported.
Inventory is in-memory/default-empty. Server configuration/persistence, connection
management, OAuth and agent-loop brokered tool exposure remain deferred; this is
not a completed remote-tool execution feature or third-party service certification.

Standalone UI verification: 252 tests, 247 passed, 5 environment-gated skips, zero
failures; typecheck/eslint clean. The real-gateway restart wrapper runs UI checks
with zero skipped tests. Ruff check/format passed on 10 changed Python files.
Bandit connector scope: zero medium/high findings (7 low in metrics).
`git diff --check` passed. Native captures at 140×52, 80×24, 40×9 and codex/mono/light
were inspected in a bounded pass; no real PTY/operator terminal or provider was used.

Initial full-run failures were an outdated new-method inventory expectation and
an isolation bootstrap overriding a child test’s temporary config directory.
The expectation/bootstrap were corrected. Exact affected checks passed on clean
HEAD `0ea164e` in a managed worktree (5 passed), and current checkout (5 passed).
Final sequential full-backend outcome follows; no failure is waived.

Tests use only the dedicated `machina_test` DB, isolated home lookup and empty
.env; no live credentials, production DB or startup files were touched. No commit,
push, deploy or git configuration modification occurred. Existing release gates and
approval reopen-on-return remain separate from this UI/inspection evidence.

Final sequential verification after the review fixes: **2106 passed / 5 skipped /
0 failed in 115.43 s**, including the real gateway restart wrapper (all **252 UI
tests**, zero skipped UI tests). Command:
`MACHINA_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/machina_test .venv/bin/python -m pytest -q --tb=short -p no:cacheprovider`,
with a disposable home/env bootstrap supplied through PYTHONPATH as described in
the visual review. Standalone UI check: 247 passed / 5 optional skips / 0 failures;
typecheck and eslint passed. No unresolved failure remains in the recorded checks.


## 2026-10-12 — MCP execution and local release candidate

MCP is now usable through `/mcp`: add a stdio or HTTP server, review its launch,
connect, inspect tools, disconnect or remove. Configurations persist in the
installation's `~/.machina/mcp.json`; inventory does not start servers.
The default installation includes the MCP SDK. Native `mcp_list`, `mcp_describe`
and `mcp_call` tools use the runtime connector service and existing approval
broker. Untrusted external calls require approval even in full mode; explicitly
allowed trusted reads retain the configured exception. Changed configuration,
arguments or schema invalidate the approved action. Launch uses the reviewed
executable/cwd snapshot; waits are bounded and uncertain dispatched calls are
reported as `outcome_unknown` without automatic side-effect retries.

Regression coverage includes SDK stdio/HTTP fixtures, elicitation refusal,
secret redaction, repeated cross-task teardown with no fixture process leaks,
persistent management through the authenticated gateway, actual agent approval
pause/resume and timeout/truncation/error handling. Independent review's launch
race and executable/cwd mismatch were fixed and covered by regressions.

Final isolated backend verification: **2237 passed, 5 skipped, 0 failed**,
**82.26% coverage**, 127.22 seconds. Standalone UI: **290 passed, 5 gated skips**;
typecheck/lint passed. The backend's real gateway wrapper runs all **295 UI
tests with no UI skips**. Ruff check/format passed; Bandit reports **0 medium,
0 high, 67 low** findings. Lockfile freshness and diff whitespace checks passed.
Wheel/source builds pass isolated imports, bundled resource hashes and source
archive cleanliness checks. Only the dedicated `machina_test` database
was used, with disposable HOME/config and cleared live credentials.

Release cleanup preserves the 71 pinned ECC roles and 42 curated skills, adds
accurate README/changelog/third-party notices and package checks, removes tracked
backup-environment artifacts and duplicate PR workflow, and excludes installed
Node dependencies from source archives. The version remains **0.2.0**, a local
candidate. Nothing was committed, pushed, published or deployed.

Next owner: read [MCP setup](../MCP.md) and
[release evidence](RELEASE_READINESS.md) before publishing.
OAuth/HTTP credential profiles, MCP resources/prompts and server callbacks stay
deferred. Saved configuration is installation-local; live connections are
worker-local and require reconnect after restart. Run hosted CI/Docker, fresh
install/platform checks, authorized vendor/staging checks and operational
backup/rollback gates before claiming production readiness. Existing historical
counts above are superseded by this entry for the current checkout.
