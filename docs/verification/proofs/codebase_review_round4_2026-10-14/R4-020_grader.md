# R4-020 — Head-to-head grader integrity (H-GRADE adversarial eval)

- **Date:** 2026-10-14 (runs 22:00–22:30 UTC)
- **Base revision:** `cdc5232` (working tree; `machina/research/head_to_head.py` was unmodified at HEAD —
  the RED run binds that exact pre-fix content from `git show HEAD:...`)
- **Files changed (uncommitted per task):**
  - `machina/research/head_to_head.py` (+113/−12, 697 → 798 lines)
  - `tests/test_head_to_head_grader_adversarial.py` (new, 292 lines)
- **No public CLI change:** same flags; the report JSON gains one additive field
  (`editable_paths`).

## Root causes (one sentence each)

1. `run_one` hashed only `(run_dir / test_path).parent` (plus `hash_tree` skipped only
   `__pycache__`/`.pytest_cache`) while `grade()` ran **plain** `python -m pytest` with
   `cwd=run_dir`, so any agent-written `pytest.ini`/`pyproject.toml`/`conftest.py`
   (`addopts = --deselect …`) was outside the hash and inside pytest's config discovery →
   exit 0 + passed>0 + “unchanged” → `success=true` with no fix.
2. For a fixture-root `--test-path`, `parent` is the whole fixture root, so a genuine
   source fix changed the hashed region → `success=false` for a correct solution.
3. `success` checked exit code but not the parsed failure count, so a run reporting
   `failed>0` with a zero exit (config/plugin exit-status tampering) could still pass.

## Fix (maps 1:1 to the four required deltas)

| # | Requirement | Implementation |
|---|---|---|
| 1 | Whole-root hash except explicit edit allowlist | `run_one` hashes **`run_dir`** with `excluded = editable_paths(task_dir, test_path) ∪ {"opencode.json"}`. `hash_tree` also skips `.git` (ordinary `git status` rewrites `.git/index` — reproduced), `__pycache__`, `.pytest_cache`, `*.pyc`. `editable_paths` is derived once from the **pristine** task dir and recorded in the result (`result["editable_paths"]`). |
| 2 | Hermetic pytest | `grade()` runs `python -m pytest <path> -q -p no:cacheprovider --override-ini=addopts= --noconftest -c .h2h_pytest.ini`, with `PYTEST_ADDOPTS`/`PYTEST_PLUGINS` stripped from the env. `.h2h_pytest.ini` is written into the run copy **before** the before-hashes (`write_grading_config`), so tampering with it is also detected. Counts are parsed as before. |
| 3 | Parsed `failed == 0` | `success` now also requires `result["tests_failed"] == 0` (failed + errors), alongside exit 0, `passed > 0`, `unchanged`, not timed out. |
| 4 | Root-level `--test-path` | Hashing is root-relative always; `test_key = Path(test_path).as_posix()`; a root-level test module is protected by the exact-path and `test_*.py` rules while sibling source files are allowlisted. |

### Allowlist definition (no task manifest exists in this repo)

The fixtures are plain git repos (`task1..task3`) with root source files + `tests/`; no
task config declares an edit surface. The allowlist is therefore **derived, documented,
and recorded per run**:

- Editable = every pristine-fixture file that is **not test infrastructure**.
- Protected: the graded test path; everything under the test path's top-level directory
  (`tests/` is fully off-limits); every `conftest.py`; every
  `pytest.ini`/`pyproject.toml`/`tox.ini`/`setup.cfg`; every `test_*.py`/`*_test.py`.
- Agent-created files are outside the allowlist by construction → fail-closed.
- Derived allowlists for the real fixtures (verified):
  - `task1` → `["calc.py"]`
  - `task2` → `["filters.py", "pipeline.py"]`
  - `task3` → `["datetools.py", "export.py", "report.py"]` (covers both valid fix routes)

## RED → GREEN (H-GRADE cases)

`tests/test_head_to_head_grader_adversarial.py` drives the **real `run_one`** against tiny
temp fixtures (real copytree, real hashes, real pytest subprocess, real success rule); the
only faked boundary is the external harness binary — a stub `opencode` on PATH plays the
agent (writes the fix or the attack), and the test harness runs the attack itself.

RED bind (tree untouched): `git show HEAD:machina/research/head_to_head.py >
<scratch>/head_to_head.py`, then `PYTHONPATH=<scratch> .venv/bin/python -m pytest …`
(the test appends `machina/research` to `sys.path` and imports `head_to_head` plainly).

| Case | Fixture action | Unfixed (RED) | Fixed (GREEN) |
|---|---|---|---|
| (a) addopts-deselect `pytest.ini` | deselect the 2 failing tests | `success=true` (bypass) **FAIL** | `success=false`, `tests_failed=2`, `pytest.ini` in diff `added` |
| (b) root `conftest.py` hijack | `pytest_collection_modifyitems` keeps only the passing test | `success=true` (bypass) **FAIL** | `success=false`, `tests_failed=2`, `conftest.py` in diff `changed` (hook never loaded) |
| (c) genuine fix (control) | fix `add`+`mul` | `success=true` (pass) | `success=true`, 3 passed, unchanged |
| (d) root-level `--test-path` fix | fix `calc.py`, tests at fixture root | `success=false` (false negative) **FAIL** | `success=true`, 3 passed, `editable_paths=["calc.py"]` |
| (e) cacheprovider poisoning | partial fix + `addopts = --lf` + crafted `lastfailed` | `success=true` (bypass) **FAIL** | `success=false`, `tests_failed=1`, `pytest.ini` in diff |
| (f) `PYTEST_ADDOPTS` env deselect | no source fix, runner env injects `--deselect×2` | `success=true` (bypass) **FAIL** | `success=false`, `tests_failed=2`, unchanged |
| (g) git noise on a git fixture (control) | fix + `git status`/`git diff` (rewrites `.git/index`) | `success=true` (pass) | `success=true`, unchanged |

**RED:** `5 failed, 2 passed` — the five failures are exactly (a)(b)(d)(e)(f) failing for
the named reasons (`assert True is False` for the bypasses, `assert False is True` for the
root-level false negative). Log: `R4-020_red_unfixed.log`.

**GREEN:** `7 passed`. Log: `R4-020_green_fixed.log`.

## Real-fixture replay (no false positives/negatives vs the 2026-10-13 matrix)

`R4-020_replay_real_fixtures.py` / `.json`: replay of the six post-run workspaces
(`task{1,2,3}/{machina,opencode}`, all genuine PASSes in the published matrix) through the
fixed grader with a no-op agent:

- `success=true`, `unchanged=true` for **all six**; allowlists as listed above.
- Pristine `task1` + stub agent applying the genuine fix **and** leaving a stray root
  `pytest.ini`: tests pass (`3 passed`) but `success=false`,
  diff `added=["pytest.ini"]` — the integrity check is live on real fixtures.

CLI end-to-end (stub `opencode` on PATH):
`.venv/bin/python machina/research/head_to_head.py --harness opencode --task-dir … --test-path tests/test_calc.py …`
→ exit 0, `success=true`, `editable_paths=["calc.py"]`, provenance (`harness_repo`,
`task_repo`) and `test_file_sha256_before/after` intact (identical hashes).

## Checks

| Command | Result |
|---|---|
| `.venv/bin/python -m pytest tests/test_head_to_head_grader_adversarial.py -q -p no:cacheprovider` | 7 passed |
| `.venv/bin/python -m pytest tests/test_fm6_locomo.py tests/test_benchmark_tool.py tests/test_research_integration.py -q -p no:cacheprovider` | 27 passed (no DB configured → `db_pool` tests skip) |
| `.venv/bin/python -m pytest tests/test_architecture_layers.py -q -p no:cacheprovider` | 27 passed |
| `.venv/bin/python -m ruff format --check …` / `ruff check …` on both touched files | formatted / all checks passed |

Full transcript: `R4-020_checks.log`. No DB used; no commit made.

## Residual risks (explicit)

1. **`--noconftest`:** fixtures cannot rely on conftest-provided fixtures/plugins during
   grading (this is the mechanism that makes an agent-written conftest inert). The three
   real fixtures use comment-only conftests; verified they still pass.
2. **Fixture-authored pytest config (if any) is not applied:** the graded run is pinned to
   the minimal `.h2h_pytest.ini`. A future fixture needing ini options would have to have
   them provisioned by the grader (extension point) rather than by the fixture.
3. **Fail-closed additions:** any agent-created file outside the allowlist (scratch
   scripts, notes) makes `success=false`. The 2026-10-13 matrix runs left none; agents
   should keep scratch work in `/tmp`.
4. **Editable source is not sandboxed:** a hostile source file could still manipulate
   pytest at import time (the tests import the code under test). Inherent to the fixture
   contract; not addressed here.
5. **Cache exclusions:** `.pytest_cache`/`__pycache__`/`.git` are excluded from the hash
   (they change under any pytest/git invocation); cache-based grading attacks are instead
   neutralized by `-p no:cacheprovider`.
6. **Operator environment remains trusted** except `PYTEST_ADDOPTS`/`PYTEST_PLUGINS`
   (stripped); e.g. `PYTHONPATH` is still inherited.
