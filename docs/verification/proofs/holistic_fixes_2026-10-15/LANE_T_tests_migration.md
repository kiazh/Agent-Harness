# LANE T — Migration plan & bootstrap: `tests/` (+ benchmarks) → Machina-DevTools

**Date:** 2026-10-15 · **Status:** audit + scratch prototype + execution-day plan. **Nothing pushed. Nothing in Machina-Harness modified** (this file and `logs/LANE_T_*` are the only additions).

**Prototype scratch dir:** `/private/var/folders/sk/y8w9dwmd4lnc2yvygqqxd4xw0000gn/T/opencode/lane_t_tests_migration/`

> ⚠️ **Counts drift — re-run the enumeration at execution time.** `tests/` is being modified by 10 concurrent fix lanes. Observed churn during this audit:
> | Moment (UTC) | `.py` files | test defs (`^\s*(async )?def test_`) |
> |---|---|---|
> | 16:35 audit start | 353 | 3,781 |
> | 16:48 prototype snapshot | 357 | 3,807 |
> | 16:53 re-count | 359 | 3,822 |
>
> Everything below is a snapshot of that window; the execution-day checklist starts with a fresh count.

---

## 1. Audit

### 1.1 What "tests" means

* Snapshot used for the prototype: **357 `.py` / 366 files / 3,807 test defs / 4.5 MB clean** (26 MB live including `__pycache__`). Subdirs: `tests/workflow_trial/` (its own conftest + suite), `tests/support/` (9 helper modules: `database`, `processes`, `schema`, `scripts`, `fencing_worker`, `mcp_fixture_server`, `gateway_connectors_wire`, `mcp_oauth_fixture`, `__init__`), `tests/fixtures/` (44 KB: model catalog + session fixtures), `tests/data/rag/machina_docs_v1.json`.
* **`tests/` is an importable package** (`tests/__init__.py`, `tests/support/__init__.py`) → pytest imports `tests.test_*` and inserts the repo root on `sys.path`; subprocess workers run `python -m tests.support.fencing_worker`.
* Pytest config lives in the harness `pyproject.toml`: `asyncio_mode = "auto"`, `testpaths = ["tests"]`, markers `ui_integration`, `workflow_trial`, `db`, `integration`, `load`; coverage `source = ["machina"]`, `fail_under = 60`.
* Marker histogram (audit window): `asyncio` 283, `parametrize` 272, `skipif` 31, `db` 29 (+ auto-added at collection, see below), `usefixtures` 25, `integration` 13, `load` 10, `workflow_trial` 2, `xfail` 1, `ui_integration` 1.
* Env vars:
  * **`MACHINA_TEST_DATABASE_URL`** — 38 test files; `tests/conftest.py` routes the `db` singleton to it (never `DATABASE_URL`); DB tests skip cleanly when unset. `.env` at the repo root currently sets it to a local disposable `machina_test` database.
  * **`MACHINA_TEST_PYTHON`** — 1 file (`tests/test_personality_growth.py:174`, UI real-gateway test); CI's `ui` job writes `MACHINA_TEST_PYTHON=$(python -c ...)` into `$GITHUB_ENV`.
  * `MACHINA_GATEWAY_NO_SCHEDULER=1` — set by an autouse session fixture so gateways never start schedulers in tests; secrets env vars are blanked per-test.
* **conftest surface (2 files):**
  * `tests/conftest.py` — `pytest_collection_modifyitems` (adds `db` marker to items using `db_pool`/`live_db` fixtures); session-autouse `gateway_scheduler_disabled`; autouse `isolate_config_secrets`; session-autouse `init_test_database` (schema init once per session when DSN set); autouse `reset_singletons` (db pool, session cache, provider cache, config defaults, audit writer); `db_pool` async fixture (skips without DSN).
  * `tests/workflow_trial/conftest.py` — Selector event loop on Windows for psycopg's async saver.
* **sys.path tricks:** (a) the package layout above means pytest prepends the checkout root; (b) 3 files `sys.path.append(_REPO_ROOT / "machina" / "research")` to import research modules directly (`test_head_to_head_grader_adversarial`, `test_head_to_head_symlink_containment`, `test_final_review_fix_research_metrics`); (c) `test_connectors_dns_pinning.py:359` runs a generated script with `sys.path.insert(0, sys.argv[1])` where `argv[1]` is the repo root; (d) `tests/support/processes.py` spawns `python -m tests.support.fencing_worker` with `cwd=repo_root`; (e) `test_benchmark_tool.py:289` injects `PYTHONPATH=REPO_ROOT` into a child.

### 1.2 Benchmarks — **none exist as a suite**

A bounded find over the repo (excluding `.git/.venv/node_modules`) found no `benchmarks/` directory, no `pytest-benchmark` dependency, and no standalone benchmark runner. Benchmark-adjacent surfaces (they stay in the harness; see `benchmarks/README.md` in the prototype):

| Surface | Location | Disposition |
|---|---|---|
| Unified offline benchmark (`python -m machina.research.benchmark`) | `machina/research/benchmark.py` | Shipped package code; **do not move** |
| Native-vs-LangGraph workflow benchmark | `machina/research/workflow_benchmark.py` | Shipped research module |
| Harness-evals LoCoMo pilot | `tools/harness_evals_pilot.py` | Operator tool, spends budget on purpose |
| `pytest.mark.load` modules | `tests/` (10 tests) | Ordinary tests; move with the suite |
| Benchmark tests | `tests/test_benchmark_tool.py` (587 lines) | Moves with the suite |
| Frozen artifact | `docs/verification/proofs/2026-10-14-benchmark.json` | Historical proof |
| Benchmark skill | `skills/ecc/skills/benchmark-optimization-loop` | Third-party skill bundle |

→ The DevTools `benchmarks/` dir is scaffolded as a placeholder with this inventory; there is nothing to migrate into it today.

### 1.3 Execution surfaces that break when `tests/` leaves

* **`.github/workflows/ci.yml` — job `lint-and-test`:**
  * Step *"Windows-only tests must skip on Linux"* runs `tests/test_integration_permissions.py::test_scope_windows_case_unc_and_symlink_lexical` and greps for `1 skipped` → **fails immediately** (file missing).
  * Step *"Run pytest with coverage"*: `pytest tests/ -v -m "not ui_integration and not workflow_trial" --cov=machina --cov-fail-under=60` → **fails** (`no such directory`), and the coverage gate disappears with it (coverage upload to Codecov is `push`-only).
  * Stays in harness: `uv lock --check`, ruff/bandit on `machina/ tools/`, hatchling build, release-artifact check, `ui` job (npm `run check`; no pytest; uses `MACHINA_TEST_PYTHON`), `docker` job.
* **`.github/workflows/workflow-trial.yml`:** path filters reference `tests/workflow_trial/**` and `tests/test_workflow_checkpoint_count.py` (dead triggers after removal); the job runs `python -m pytest tests/ -m workflow_trial -v --strict-markers` → **fails** on dispatch/schedule. The workflow's future home is DevTools, or keep it in the harness with a cross-repo checkout (both repos are public → `actions/checkout` with `repository:` needs no token).
* **`README.md`:** dev quickstart (≈L686–691: `python -m pytest -q --tb=short -p no:cacheprovider`), env-var guidance (L698), marker guidance "run those groups locally" (L730–735).
* **Docs:** 90 markdown files mention `pytest tests/`; nearly all are **historical proof bundles** under `docs/verification/` that describe their own checkout — leave them. Only `README.md` (+ any current-state doc) needs updating. `OPERATIONS.md`, `install.sh`, `scripts/`, `tools/` contain **no** pytest invocations (verified).
* **no-mistakes:** registered repo = Machina-Harness (bare repo `~/.no-mistakes/repos/28a5341a64de.git`, origin → Machina-Harness, gate-config hash present). Global `~/.no-mistakes/config.yaml` read successfully (agent `opencode`; `test_agent_timeout 30m`, `auto_fix.test 3`, `ci.revalidate_repairs false`, evidence defaults). No repo-level `.no-mistakes.yaml` exists in the harness. Its Test step drives tests from the checkout, so after removal it has nothing to run **unless CI still exercises the suite** (see §3.3/§3.4).

### 1.4 Repo-root path-assumption surface inside `tests/`

**39 files** navigate to the repo root from `__file__`: 34 × `parents[1]` + 1 × `parents[2]` (`tests/support/processes.py`) + 4 × `parent.parent`. Full list in Appendix A. Categories:

1. **Root-anchored source/artifact reads** (the big one): `machina/` sources (`test_code_quality`, `test_schema_conformance`, `test_architecture_layers`, `test_controller_triage`, `test_gateway_images` → `machina/db/image_context_schema.sql`, …), `skills/` (`test_fm6_scanner`), `research/` (`test_research_phase2` → `research/identity_scenarios_example.json`), `ui/` (`test_r4_093_cli_allowlist` → `ui/src/features/keys.ts`), root files `docker-compose.yml` (`test_security`, `test_audit_regressions`, `test_infra_fixes`), `install.sh` (`test_installer_quoting`, `test_installer_env`, `test_infra_fixes`), `scripts/README.md`, `pyproject.toml` (`test_final_pass_fixes` — **collides with DevTools' own pyproject**), `research/` imports via `sys.path.append`.
2. **CWD-relative reads** (assume `cwd == repo root`, true today because `pytest` is launched from the root): e.g. `test_protocol_conformance` reads `Path("ui/src/events.ts")` and `Path("machina/protocol/events.json")`; `test_wiring_parity` reads `pathlib.Path("machina/skills/workflow.py")`; ~78 quoted repo-relative path strings across the suite (several are inside `tmp_path` fixtures and harmless).
3. **Subprocess cwd/env:** `test_gateway.py:504` and `test_gateway_connectors.py:169` run children with `cwd=parents[1]`; `tests/support/processes.py:34` `cwd=parents[2]`; `test_benchmark_tool.py` child env gets `PYTHONPATH=REPO_ROOT`.
4. `monkeypatch.chdir` in 6 files and `os.getcwd()` in 5 (mostly `tmp_path`-relative, low risk).

**Breakage model in a separate checkout:** `parents[1]` becomes the DevTools root (no `machina/`, `skills/`, `ui/`, …), CWD-relative reads resolve in DevTools, and child processes get a DevTools-root `PYTHONPATH`. All 39 files + the CWD readers break **without a shim**.

**Shim strategy (smallest → durable):**
- **S1 — root conftest shim (prototype `Machina-DevTools/conftest.py`):** resolve the harness checkout via `MACHINA_REPO` env or sibling default (`../Machina-Harness`, `../machina`, …), verify `machina/__init__.py` exists, export `MACHINA_REPO`, prepend to `sys.path`. One file, loaded before `tests/conftest.py`, zero test edits.
- **S2 — `run-tests.sh`:** same resolution in shell; export `PYTHONPATH="$MACHINA_REPO"` (covers child processes and `-m machina...` invocations) and `PYTHONDONTWRITEBYTECODE=1`; run from DevTools root (CWD reads resolve there). CI can instead `pip install -e "$MACHINA_REPO[dev,connectors,workflow-trial]"`.
- **S3 — optional `bootstrap-links.sh` (zero-edit bridge):** symlink the harness-root artifacts tests read (`machina skills research docs ui tools scripts docker-compose.yml install.sh .env.example`) into the DevTools root. Covers categories 1–3 for **unmodified** tests. Known gap: `README.md` and `pyproject.toml` names belong to DevTools, so tests reading those (e.g. `test_final_pass_fixes`) still need the durable fix.
- **Durable fix (execution day):** add `tests/support/repo_paths.py` with `repo_root()` (same resolution order as the conftest) + convenience `repo_path("ui", …)`, then mechanically rewrite the 39 anchors and the CWD readers to use it. After that, delete `bootstrap-links.sh`. Estimated ~40 small, reviewable diffs in one PR.

---

## 2. Bootstrap prototype (scratch only)

### 2.1 Layout built (`$SCRATCH/Machina-DevTools/` + sibling `$SCRATCH/Machina-Harness/` snapshot)

```
lane_t_tests_migration/
├── Machina-Harness/        # rsync snapshot of the repo (34 MB, no tests/, no .env, no caches)
└── Machina-DevTools/       # proposed layout
    ├── tests/              # snapshot 2026-10-15T16:48:38Z — 357 .py / 366 files / 4.5 MB
    ├── benchmarks/README.md
    ├── conftest.py         # S1 shim (MACHINA_REPO / sibling resolution + sys.path)
    ├── run-tests.sh        # S2 runner (PYTHONPATH, bytecode-off, pytest "$@")
    ├── bootstrap-links.sh  # S3 symlink bridge (optional)
    ├── Makefile            # bootstrap / test / test-backend / test-db
    └── pyproject.toml      # pytest config mirror (markers, asyncio auto, testpaths)
```

### 2.2 Proof runs (from the DevTools layout, against the snapshot checkout)

Interpreter: harness venv `/Users/shadow/machina/.venv/bin/python` (3.14.5, pytest 9.1.1) — used read-only.

1. **Import resolution:** `python -c "import machina"` from DevTools root resolved to the **snapshot** (`.../lane_t_tests_migration/Machina-DevTools/machina/__init__.py`, i.e. via the symlink to the snapshot), never the live repo.
2. **Representative unit suite:**
   `PYTHON=.../.venv/bin/python ./run-tests.sh tests/test_skill_runtime.py -q -p no:cacheprovider`
   → **7 passed in 0.65s** (exit 0) — log `logs/LANE_T_suite1_skill_runtime.log`
3. **Repo-root / CWD-coupled suite:** `./run-tests.sh tests/test_protocol_conformance.py -q -p no:cacheprovider`
   → **9 passed, 1 skipped in 0.57s** (exit 0); it read `ui/src/events.ts` + `machina/protocol/events.json` through the shim — log `logs/LANE_T_suite2_protocol_conformance.log`
4. **DB-backed suite:** `MACHINA_TEST_DATABASE_URL=$(grep '^MACHINA_TEST_DATABASE_URL=' /Users/shadow/machina/.env | cut -d= -f2-) ./run-tests.sh tests/test_approvals_ownership.py -q -p no:cacheprovider`
   → **10 passed in 1.16s** (exit 0) against the local disposable `machina_test` Postgres (checked idle: 0 other connections; suite namespaces its rows as `lp-test%` and deletes them) — log `logs/LANE_T_suite3_db_approvals_ownership.log`

Whole-suite run deliberately **not** performed (10 lanes are exercising the same database).

### 2.3 Non-destructiveness / hygiene

* All runs executed from the scratch tree; `/Users/shadow/machina` was only read. `PYTHONDONTWRITEBYTECODE=1` + `-p no:cacheprovider` → **0** `__pycache__`/`.pytest_cache` created in scratch, and import resolution never touched the live repo.
* The DB run connected to the local disposable test database (`.env` DSN, password never printed); session schema init is idempotent; rows cleaned by the suite itself.

### 2.4 DevTools remote reachability & credentials (read-only)

From `logs/LANE_T_remote_check.txt`:

* `GIT_TERMINAL_PROMPT=0 git ls-remote https://github.com/kiazh/Machina-DevTools.git` → **exit 0, zero refs** → the remote exists and is an **empty repository** (no default branch).
* `gh repo view kiazh/Machina-DevTools` → `{"visibility":"PUBLIC","isEmpty":true,"createdAt":"2026-10-15T16:42:26Z","isFork":false}`.
* Control: HTTPS `ls-remote` of Machina-Harness returns `main → 3ef6d5b…` using the git config `osxkeychain` helper (`/opt/homebrew/etc/gitconfig`) → HTTPS credentials for GitHub are live on this machine.
* `gh auth status` → account **kiazh**, token scopes `repo`, `workflow`, gist, read:org.
* **Inference:** the repo exists, is public and empty; the available credentials (keychain + `repo`-scoped token) are consistent with being able to push to it. **No push was attempted** per lane instructions.

---

## 3. Execution-day migration plan

### 3.1 Seeding DevTools

**Option A — filtered-history move (recommended if tooling is installed).**
On a *scratch clone* (never the live repo):

```bash
git clone --no-local --bare <Machina-Harness> /tmp/mh-export.git
cd /tmp/mh-export.git
git filter-repo --path tests/ --path benchmarks/       # keeps only tests/ history, prunes empties
# then (in a devtools working clone) add the bootstrap files and push to DevTools main
```

* Pros: preserves per-file blame/log for the suite (valuable — the tests encode incident regressions with commit-message provenance); reviewable as "the same files, new home".
* Cons: requires `git-filter-repo` (**not installed on this machine** — `brew install git-filter-repo` / `pipx` on execution day); history rewrite gives new SHAs (fine — harness history is untouched); requires a secret scan of the exported history before pushing to a **public** repo; `filter-repo` refuses non-fresh clones (hence the scratch bare clone).
* Note: `--path benchmarks/` is a no-op today (no such dir) but future-proofs nothing — include or drop, it does not change the result.

**Option B — plain copy (fallback).**
`rsync tests/ → DevTools`, one commit: `Import tests/ from Machina-Harness@3ef6d5b (post-campaign snapshot)`. Pros: zero tooling, trivially reviewable, fastest. Cons: loses per-file history/blame; provenance lives only in the commit message. Given the churn of this suite by parallel lanes, B is entirely acceptable.

Either way, the first DevTools commits are: (1) seeded `tests/`, (2) the prototype scaffolding (`conftest.py`, `run-tests.sh`, `bootstrap-links.sh`, `Makefile`, `pyproject.toml`, `benchmarks/README.md`, `README.md`), (3) `.gitignore` (`.pytest_cache/`, `.hypothesis/`, `__pycache__/`).

### 3.2 Removal commit in Machina-Harness (only after DevTools CI is green)

```
git rm -r tests
# .github/workflows/ci.yml        → drop the Windows-skip step + pytest/coverage step (or retarget to cross-repo job)
# .github/workflows/workflow-trial.yml → move to DevTools, or retarget with cross-repo checkout (public repos, no token)
# README.md                       → dev section points to Machina-DevTools; keep the "don't share a test DB" warning
# pyproject.toml                  → [tool.pytest.ini_options] and [tool.coverage.*] move with the suite
git commit -m "tests: move suite to kiazh/Machina-DevTools (green at <devtools-sha>)"
```

Leave historical proof docs' `pytest tests/` references untouched (they describe their own checkouts). Order matters: removal lands **after** a green DevTools run against the exact harness SHA being left behind.

### 3.3 CI strategy options

1. **DevTools-owned CI (recommended source of truth).** DevTools workflow on push/PR + weekly + dispatch. Checkout DevTools, then `actions/checkout` the harness (`repository: kiazh/Machina-Harness`, public → no token) into the sibling path; Python 3.11; `pip install -e ../Machina-Harness[dev,connectors,workflow-trial]`; pgvector Postgres service + `MACHINA_TEST_DATABASE_URL`; run `./run-tests.sh -m "not ui_integration and not workflow_trial"`; keep the windows-skip assertion as its own step; workflow-trial suite as a separate job. Coverage: run with `--cov=../Machina-Harness/machina` (or coverage `source` override) and upload with the machina repo's Codecov token + flags so the harness dashboard keeps working (verify on execution day; worst case coverage upload moves to DevTools-only). Adopt the harness's SHA-pinning convention for actions.
2. **Harness-owned thin runner.** Harness `ci.yml` keeps a job that checks out DevTools and runs its `run-tests.sh` against the harness workspace. Every harness push/PR is still gated, no cross-repo tokens needed (public), no coverage move (runs in harness context). Downside: two config surfaces; needs a trigger/dispatch for test-only changes (or rely on option 1 for that).
3. **Local-only.** Not acceptable as the only gate; fine as an interim while 1–2 land.

Recommended: **1 as owner + 2 (or a `repository_dispatch` from harness main) as the "catch machina regressions" trigger**. Keep the harness `ui`/`docker`/lint jobs where they are.

### 3.4 no-mistakes implications

* The no-mistakes registration is the **harness** (`28a5341a64de` → Machina-Harness). After removal, its Test step has no in-repo suite to run; let the step fail closed or point it at the cross-repo runner.
* Cleanest: register **Machina-DevTools** with no-mistakes (`no-mistakes init` there) so test-bearing changes get their own pipeline; and keep the harness CI job (option 2) running the DevTools runner so no-mistakes' CI monitor on harness PRs still sees real test results.
* `~/.no-mistakes/config.yaml` (global) needs no changes; there is no repo-level config to migrate. `test_agent_timeout: 30m` is a stall bound; the ~3.8k-test backend suite fits within one agent turn today (the campaign's full runs complete well within it).

### 3.5 Risks & rollback

* **History secrets** (Option A): scan `git log -p -- tests` (or install gitleaks/trufflehog — absent today) before pushing to the **public** DevTools repo; exclude `.env*` patterns from the export.
* **Coverage attribution:** Codecov keyed to Machina-Harness while the run happens in DevTools — validate the token/flag path during seeding (worst case: coverage lives in DevTools).
* **Skew between repos:** dispatch DevTools runs on the exact harness SHA; never test DevTools `main` against harness `main` blindly.
* **Trigger drift:** harness PRs must keep a real test gate (option 2) or breakage-detection degrades.
* **Concurrent campaign:** migrate in a freeze window after the fix lanes land; re-enumerate counts first (they moved 353→359 `.py` in <20 min during this audit).
* **Rollback:** single `git revert` of the removal commit restores `tests/` + CI steps on harness; DevTools import is additive and can be archived. No harness force-push, no history rewrite on harness.

### 3.6 Execution-day checklist

1. Freeze: fix campaign landed; record green harness SHA `S`; **re-run the audit counts** (`find … -name '*.py'`, test-def grep, 39-file path grep).
2. `brew/pipx install git-filter-repo` (if Option A) + secret-scan the export.
3. Seed DevTools (A or B) + scaffolding + CI workflow; verify **green against harness `S`** (backend selection; windows-skip step; workflow-trial job).
4. Land the harness removal commit (git rm + CI/README/pyproject edits) referencing the DevTools green run; keep rollback revert staged.
5. Flip triggers: harness dispatch/thin-runner + DevTools CI on PR; register DevTools with no-mistakes; monitor one full cycle.
6. Durable path fix in DevTools: add `tests/support/repo_paths.py`, rewrite the 39 anchors + CWD readers, delete `bootstrap-links.sh`. Update harness README pointer.

---

## Appendix A — files anchoring at the repo root (39, from the 16:53 snapshot)

`tests/support/processes.py` (`parents[2]`, child cwd) · `tests/test_a5_turn_flow.py` · `tests/test_architecture_layers.py` · `tests/test_architecture_pairs.py` · `tests/test_audit_regressions.py` · `tests/test_benchmark_tool.py` · `tests/test_chatgpt_auth_adversarial.py` · `tests/test_cli_turn_e2e.py` · `tests/test_code_quality.py` · `tests/test_connectors_client.py` · `tests/test_connectors_dns_pinning.py` · `tests/test_controller_triage.py` · `tests/test_final_pass_fixes.py` · `tests/test_final_review_fix_research_metrics.py` · `tests/test_fm6_scanner.py` · `tests/test_gateway.py` · `tests/test_gateway_connectors.py` · `tests/test_gateway_connectors_sdk.py` · `tests/test_gateway_images.py` · `tests/test_head_to_head_grader_adversarial.py` · `tests/test_head_to_head_symlink_containment.py` · `tests/test_infra_fixes.py` · `tests/test_installer_env.py` · `tests/test_installer_quoting.py` · `tests/test_leaf_independence.py` · `tests/test_mcp_server.py` · `tests/test_personality_growth.py` · `tests/test_r4_045_047_049_providers.py` · `tests/test_r4_080_boot_config.py` · `tests/test_r4_083_gateway_standalone.py` · `tests/test_r4_093_cli_allowlist.py` · `tests/test_r4_105_store_fence_flock.py` · `tests/test_rag_quality.py` · `tests/test_repomap.py` · `tests/test_research_phase2.py` · `tests/test_review_round2_fixes.py` · `tests/test_schema_conformance.py` · `tests/test_security.py` · `tests/test_subscription_removals.py`

## Appendix B — artifacts

* `logs/LANE_T_remote_check.txt` — remote reachability + credentials evidence.
* `logs/LANE_T_devtools_layout.txt` — `ls -la` of the prototype layout.
* `logs/LANE_T_suite1_skill_runtime.log` — 7 passed.
* `logs/LANE_T_suite2_protocol_conformance.log` — 9 passed, 1 skipped.
* `logs/LANE_T_suite3_db_approvals_ownership.log` — 10 passed (DB-backed).
* Prototype scripts: `conftest.py`, `run-tests.sh`, `bootstrap-links.sh`, `Makefile`, `pyproject.toml`, `benchmarks/README.md`, `README.md` (in the scratch dir above; contents folded into this report).
