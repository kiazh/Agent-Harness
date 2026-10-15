# R4-095 — Infra cluster proof (2026-10-14)

Task: `docs/verification/FIX_PLAN_ROUND4_TASKS.md` → R4-095 (findings ledger `Infra — I1`).
Scope: `.github/*`, `install.sh`, `tools/install-helpers.sh`, `Dockerfile`,
`Dockerfile.sandbox`, `docker-compose.yml`, `pyproject.toml`,
`tools/check_release_artifacts.py`, `tools/e2e_gate.py`, `tools/vendor_ecc*.py`,
`scripts/*`. **No commit** (working-tree changes only).

Rules followed: read-only-safe checks only (no docker/installer/network runs;
all probes drive fake commands or stdlib/YAML parsing). No new dependencies.
Workflows keep the existing action majors (pins, not upgrades).

Baseline: `git HEAD` at start of work `5fd93e1` (one throwaway worktree at
`5fd93e1` was used for the pre-fix wheel, removed afterwards).

## Changed files

```
 .github/dependabot.yml               |  14 ++--
 .github/workflows/ci.yml             |  53 ++++++++++++---
 .github/workflows/workflow-trial.yml |   7 +-
 Dockerfile                           |   8 ++-
 Dockerfile.sandbox                   |   5 +-
 docker-compose.yml                   |  13 +++-
 install.sh                           |  77 ++++++++++++++-------
 pyproject.toml                       |  21 +++---
 tools/check_release_artifacts.py     |   6 ++
 tools/e2e_gate.py                    | 126 ++++++++++++++++++++++++++++++++++-
 tools/install-helpers.sh             |  64 ++++++++++++++++--
 tools/vendor_ecc.py                  |  14 +++-
 12 files changed, 343 insertions(+), 65 deletions(-)
```

---

## (a) CI verifies the lockfile; Windows decision

**Probe (finding I1):** `uv.lock` is declared authoritative
(`docs/verification/RELEASE_READINESS.md:17,33`) but CI never ran
`uv lock --check`; the task file's fix step names the missing workflow step.
Windows-only tests: inventory of `tests/` shows exactly one test that skips
when not on Windows —
`tests/test_integration_permissions.py::test_scope_windows_case_unc_and_symlink_lexical`
(line 559: `if _os.name != "nt": pytest.skip("Windows path semantics")`). All
other `os.name` markers in tests are POSIX-only *runs* on Linux CI, not Windows
skips.

**Fix:**
- `.github/workflows/ci.yml:63-70` — new step
  `Verify lockfile matches pyproject (uv lock --check)`:
  `python -m pip install "uv==0.11.7"` then `uv lock --check`. uv is pinned to
  the version verified against this lockfile (revision 3) and the step fails
  any pyproject/Dependabot change that was not regenerated into `uv.lock`.
- **Decision (option B): expected-skip + tracked note, no Windows runner.**
  `.github/workflows/ci.yml:81-94` — a step runs the one Windows-only test with
  `-rs` and fails unless it reports exactly `1 skipped`; a comment records the
  decision and points at this proof doc (tracked: R4-104 / review I1). Rationale
  for not adding a Windows job: CI cost and flake surface against a single
  scope-semantics test; nothing else in the suite needs Windows. The Linux
  suite already fails if that test ever stops skipping there.

**Evidence:**
- `R4-095_checks.txt` — YAML parse of the workflow, `uv lock --check --offline`
  local run (`Resolved 130 packages`, `uv.lock unchanged`), Windows test run
  showing `1 skipped`.
- actionlint is not installed on this machine; YAML parse + `actionlint`
  absence are recorded in the transcript. CI itself is the hosted validator.

**Residual:** the CI step uses the online `uv lock --check` form (the offline
form was verified locally); a future uv major bump may want a lockfile
regeneration — the pin makes that an explicit, reviewed change.

## (b) docker-db reuse credential reconciliation

**Probe (finding I1):** `tools/install-helpers.sh` old `start_docker_db`
returned 0 for any existing `machina-db` (running or stopped) without checking
its `POSTGRES_PASSWORD` against `DATABASE_URL`. On a mismatch the installer ran
`wait_for_init` (30 × 2 s retries) and then fell through to `start_brew_db` —
a Homebrew install that cannot repair a custom `DATABASE_URL`.

**Fix** (`tools/install-helpers.sh:222-297`):
- New `docker_db_container_password` helper reads
  `POSTGRES_PASSWORD` from `docker inspect` (never echoed).
- Reuse branch now reconciles:
  - installer-default/unset `DATABASE_URL` → rewritten to the container's real
    credentials via `maybe_write_db_url`;
  - exact match → reused as before;
  - custom mismatched URL or unreadable credentials → **rc=2 with two concrete
    fixes** (remove the container or align `DATABASE_URL`), no secret echoed.
- `start_docker_db` documents its rc contract (0 reuse, 1 no docker → brew may
  help, 2 conflict → stop).
- `install.sh:376-433` restructured: captures `DOCKER_DB_RC`, handles rc=2 with
  guidance and *skips* both `wait_for_init` and `start_brew_db`; all other flows
  (no docker, docker started but init failed, `--no-docker-db`) behave as
  before, including the existing "Schema ready (via Homebrew)" path.
- Executable image reference pinned: `PGVECTOR_IMAGE`
  (`tools/install-helpers.sh:23`) with tag+digest; human guidance text keeps the
  readable `pgvector/pgvector:pg16` tag.

**Evidence:** `R4-095_docker_reuse_probe.sh` / `.txt` — real helper functions
driven with a fake `docker` (no real containers):
6 scenarios (match reuse, default-URL reconcile, custom mismatch rc=2 with
untouched `.env`, unreadable credentials rc=2, stopped-container start, fresh
create using the pinned digest) all PASS, plus the static rc=2-before-brew
ordering checks. Existing suite `tests/test_installer_env.py` (55 tests incl.
`test_docker_custom_url_preserved`) green.

**Residual:** an rc=2 run leaves `INIT_STATE="failed"` and the final "next
steps" block tells the user schema is not ready — intended; the guidance above
it names both fixes.

## (c) Actions pinned by commit SHA, base images by digest

**Probe:** all workflow steps used moving tags (`@v4`, `@v5`) and images used
mutable tags (`pgvector/pgvector:pg16`, `python:3.11-slim`).

**Fix:** resolved via read-only APIs on 2026-10-14 (GitHub
`/repos/{repo}/commits/{tag}` for SHAs, Docker Hub tag JSON for manifest-list
digests) and pinned with version comments:

| reference | pinned | version |
|---|---|---|
| actions/checkout | `11d5960a326750d5838078e36cf38b85af677262` | v4.4.0 |
| actions/setup-python | `a26af69be951a213d495a4c3e4e4022e16d87065` | v5.6.0 |
| actions/setup-node | `49933ea5288caeca8642d1e84afbd3f7d6820020` | v4.4.0 |
| actions/upload-artifact | `ea165f8d65b6e75b540449e92b4886f43607fa02` | v4.6.2 |
| codecov/codecov-action | `b9fd7d16f6d7d1b5d2bec1a2887e65ceed900238` | v4.6.0 |
| python:3.11-slim | `sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce` | tag digest 2026-10-09 |
| pgvector/pgvector:pg16 | `sha256:7b822b0aac60967beb1ea5e576b8602c94c300a157d187f385ae3e0da199b90a` | tag digest 2026-10-01 |

Applied in `.github/workflows/ci.yml` (5 actions, both postgres services),
`.github/workflows/workflow-trial.yml`, `Dockerfile` (both stages),
`Dockerfile.sandbox`, `docker-compose.yml`, and the installer (`PGVECTOR_IMAGE`
above). `.github/dependabot.yml` comment now says Dependabot's
`github-actions`/`docker` PRs rewrite the pinned reference **and** the trailing
version comment, and that pip PRs must ship a regenerated `uv.lock` (CI's
`uv lock --check` gates them).

**Evidence:** `R4-095_checks.txt` YAML parse + the refs audit below
(every `uses:`/`FROM`/`image:` carries `@<sha|digest>`):

```
$ grep -rn "uses: ...@" .github/workflows/      # 10 hits, all SHA-pinned
$ grep -rn "image:\|FROM " ... # ci.yml x2, workflow-trial, compose, Dockerfile x2, Dockerfile.sandbox — all digest-pinned
```

**Residual:** digests/SHAs were resolved by API lookup (no `docker build`/CI
run locally, per the read-only rule); the pinned windows/linux images used here
are multi-arch manifest-list digests, so `ubuntu-latest` runners keep pulling
the same content. Dependabot will propose future bumps.

## (d) install.sh env-file parent guard; compose `./machina` mount

**Probe (finding I1):** `MACHINA_ENV_FILE=/missing/dir/.env` reached
`[ -f "$ENV_FILE" ] || : > "$ENV_FILE"` (and the `cp .env.example` branch) and
died with a bare bash redirection error.

**Fix:** `install.sh:204-216` — guard between the two markers
`--- env-file parent guard ---` / `--- end env-file parent guard ---`:
missing parent → `die "… directory … does not exist (create it, or point
MACHINA_ENV_FILE at an existing directory)"`; non-writable parent → `die "…
is not writable"`. Runs before any `.env` read/write.

**Evidence:** `R4-095_install_env_guard_probe.sh` / `.txt` — the guard block is
extracted **verbatim** from `install.sh` and executed against three scenarios
(missing parent, chmod-555 parent, valid parent). RED: the pre-fix snippet
emits `bash: …: No such file or directory` (rc 1); GREEN: both bad cases exit
via `die` with guidance, valid case proceeds. Static check confirms the guard
precedes the redirection line.

**Compose mount — decision: document as inert (no PYTHONPATH wiring).**
`docker-compose.yml:60-69` now states the mount is INERT for the service:
`machina init`/`serve` run through the console entry point, which imports the
pip-installed copy in site-packages (`/app` is not on `sys.path` for a console
script), so host edits do not reach the container — rebuild the image. The note
also records the real caveat: ad-hoc `python -m machina…`/`python -c` from
`/app` DOES pick the mounted tree up (cwd on `sys.path`), which is the
stale-code trap. Wiring `PYTHONPATH=/app` was rejected: it would silently run
host code with possibly stale installed deps in a `read_only` container.

**Evidence:** `R4-095_mount_inert_probe.sh` / `.txt` — a cwd-local shadow
package is ignored by a console-script invocation (`sys.path[0]` = script bin
dir; installed copy wins) and IS imported by `python -c` from the same cwd
(`sys.path[0] == ''`). Both documented claims verified.

## (e) `e2e_gate.py` refuses non-test databases without `--db-url`

**Probe (finding I1):** the gate resolved its database from ambient config with
no flag and no guard; journeys write real sessions/chunks/memories.

**Fix (`tools/e2e_gate.py`):**
- `--db-url` added (`:2846-2853`); when passed it is exported to every child as
  `MACHINA_DATABASE_URL` and `DATABASE_URL` (explicit config env beats `.env`).
- Guard in `main()` (`:2871-2905`): resolution order flag →
  `MACHINA_DATABASE_URL` → `DATABASE_URL`; no URL → exit 2; unparsable/
  non-postgres URL → exit 2; non-test database name (`"test"` absent) without
  the flag → exit 2 with guidance; the flag is the explicit consent and is
  logged as such.
- Helpers `resolve_db_url`, `database_name_from_url`, `is_test_database`,
  `summarize_db_url`, `redacted_command_line` (`:211-268`); the proof's
  `command_line` field masks `--db-url` values so credentials never land in the
  proofs JSON/Markdown, and the new `database` block records source + summary
  (host/db only).

**Evidence:** `R4-095_e2e_db_guard_probe.py` / `.txt` — helper semantics
(precedence, parsing, `--db-url=` redaction) plus four real CLI refusal runs
(no env, non-test env, missing db name, non-postgres URL) all exit 2 before
touching anything; sample output shows the URL summary strips credentials.

**Residual:** documented usages in `docs/verification/E2E_PROTOCOL.md` (owned by
R4-120) still show the flagless invocation; runs against the dev database
`machina` now require
`--db-url postgresql://…:…@host:5432/machina` (or a test-like DB in
`MACHINA_DATABASE_URL`). A test-like ambient database needs no flag.

## (f) `tools/vendor_ecc.py` IndexError → validation error

**Probe:** `original.read_text().split("---", 2)[1]` raised `IndexError` for a
`SKILL.md` without frontmatter (verified RED with the pre-fix module imported
from `git show HEAD:tools/vendor_ecc.py`).

**Fix (`tools/vendor_ecc.py:171-192`):** split guarded
(`ValueError: … has no YAML frontmatter: <relative>`), then `yaml.safe_load`
result type-checked (`ValueError: … must define string name/description`).
Mirrors the existing validation style in `tools/vendor_ecc_agents.py`.

**Evidence:** `R4-095_vendor_ecc_probe.py` / `.txt` — RED pre-fix `IndexError`;
GREEN clear `ValueError` naming the file; missing-description case rejected;
valid fixture still vendors (manifest written). `tests/test_vendor_ecc.py`
green.

## (g) Wheel telemetry exclusion

**Probe:** `hatchling`'s `force-include` bypasses **every** `exclude` —
verified three ways: `[tool.hatch.build] exclude`, target-level
`[tool.hatch.build.targets.wheel] exclude`, and `packages = ["machina"]` +
force-include all shipped `skills/telemetry.json[.lock]` from a used checkout
(counters only, but machine-local state).

**Fix (trivial once measured):** `pyproject.toml:59-75` — the wheel target now
selects `skills` through `packages = ["machina", "skills"]` (normal file
selection honors `exclude`), force-include for the wheel removed, and
`exclude = ["skills/telemetry.json", "skills/telemetry.json.lock"]`. Member
paths are unchanged. `tools/check_release_artifacts.py:87-89` now fails CI when
any `skills/telemetry.json*` member appears in a wheel.
Sdist: `include = ["skills"]` replaces the VCS selection set and dropped most
of the tree (measured), so the sdist keeps force-include; the counters-only
file may ship in sdists and that limitation is documented in the pyproject
comment + `.dockerignore` already prunes it from image contexts.

**Evidence:** `R4-095_wheel_telemetry_probe.sh` / `.txt` — pre-fix wheel built
from a throwaway `git worktree` (HEAD, telemetry copied in) carries both
telemetry members; post-fix wheel has 0 telemetry members, the skills member
set is identical minus those two, and the artifact checker rejects the pre-fix
wheel / accepts the post-fix one. `R4-095_artifacts_check.txt` — full
`tools/check_release_artifacts.py` on wheel+sdist green (missing-resource
checks, ECC hashes, isolated import).

**Residual:** sdist may carry `skills/telemetry.json` (documented; not a wheel
artifact consumers install). `tests/test_final_pass_fixes.py::test_docker_context_and_docs_handle_telemetry_state`
still passes (`.dockerignore` entries + "force-include bypasses" wording kept).

---

## Verification summary

| check | command | result |
|---|---|---|
| shell syntax | `bash -n install.sh tools/install-helpers.sh` | OK (`R4-095_checks.txt`) |
| YAML/TOML parse | workflows, `dependabot.yml`, `docker-compose.yml`, `pyproject.toml` | OK |
| lockfile | `uv lock --check --offline` (local; CI uses online form) | `Resolved 130 packages`, lock unchanged |
| focused suites | `pytest tests/test_installer_env.py tests/test_infra_fixes.py tests/test_vendor_ecc.py -q -p no:cacheprovider` (`tests/test_release_artifacts*` absent) | 55 passed (`R4-095_focused_pytest.txt`) |
| related suites | telemetry/compose/ECC-PIN config tests | 4 passed |
| Windows expectation | Windows-only test with `-rs` | `1 skipped` (matches the CI step's grep) |
| lint | `ruff check tools/`, `ruff format --check tools/` | clean |
| probes | docker reuse, env guard, e2e DB guard, vendor frontmatter, wheel telemetry, mount inertness | all PASS (see files above) |

`actionlint` is not installed locally; the YAML parse is the substitute and
hosted CI performs the authoritative workflow lint.

## Residuals / notes

1. Online `uv lock --check` and docker builds are exercised by CI only (the
   task forbade docker/network runs); local equivalents were checked
   (`--offline` lock check, digest resolution via registry API, YAML parse).
2. `docs/verification/E2E_PROTOCOL.md` and `OPERATIONS.md` still show flagless
   gate invocations — docs updates belong to R4-120; this proof records the new
   `--db-url` requirement.
3. Pre-existing, unrelated: `tools/install-helpers.sh` keeps the stale
   `# shellcheck disable=SC2086` above a fully quoted `docker run`; left as-is
   (no drive-by changes) and `shellcheck` is not part of the gate.
4. `pyproject.toml` now uses `packages` selection for the wheel's `skills`
   tree; CI's artifact check (member parity, resources, hashes) guards against
   silent regressions if hatchling changes behavior.
5. No commit made; all changes remain in the working tree alongside concurrent
   sessions' edits.
