# Final review — infra + docs-truth slice (line-by-line)

Date: 2026-10-15
Reviewer: subagent (read-only line pass)
Scope (every line read):
`install.sh`, `tools/install-helpers.sh`, `tools/e2e_gate.py`, `tools/vendor_ecc.py`,
`tools/check_release_artifacts.py`, `Dockerfile`, `Dockerfile.sandbox`,
`docker-compose.yml`, `.github/workflows/*.yml`, `.github/dependabot.yml`,
`pyproject.toml`, `.env.example`, `.gitignore` (`.dockerignore` read too).
Docs spot-verified: `OPERATIONS.md`, `SECURITY.md`, `README.md`,
`FEATURE_LEDGER.md`, `CHANGELOG.md`, plus `docs/verification/E2E_PROTOCOL.md`
and the R4-095 infra proof (`.github` cross-checks).

Rules: no edits; no runtime execution of the repo (no `install.sh`, gate,
docker, CI, or package builds). Upstream registry APIs were queried read-only
to verify pins. Every finding carries a falsification attempt.

Baseline: working tree at HEAD `1e1edbf` (evidence file written after the pass).

---

## Confirmed findings

### F1 — P3 — Pinned-digest bump automation is claimed but does not cover three of the six sites (docs-truth + supply-chain drift)

**Claim sites (docs/comments):**
- `tools/install-helpers.sh:19-23` — "…Bump the digest (and keep the tag for
  operators) when upgrading; Dependabot's docker ecosystem covers the same
  reference in ci.yml/docker-compose."
- `.github/workflows/ci.yml:16-22` — "Dependabot (github-actions + docker
  ecosystems, .github/dependabot.yml) opens PRs that bump these SHAs/digests".
- `.github/workflows/workflow-trial.yml:38` — "Pinned by tag@digest (R4-095);
  Dependabot's docker ecosystem bumps it."
- `docker-compose.yml:3-4` — "Dependabot's docker ecosystem bumps the digest."

**Source of truth:**
- `.github/dependabot.yml:7-29` declares exactly four ecosystems:
  `github-actions` (:9), `pip` (:13), `npm` (:21), `docker` (:26). There is
  **no `docker-compose` entry.**
- Upstream `dependabot-core` `docker/lib/dependabot/docker/file_fetcher.rb`
  defines `DOCKER_REGEXP = /dockerfile|containerfile/i` and fetches only files
  whose **name** matches it. So the `docker` ecosystem fetches `Dockerfile`
  and `Dockerfile.sandbox` (names contain "Dockerfile") but **not** GitHub
  Actions workflow files and **not** `docker-compose.yml` (name contains
  neither token). Docker image references inside workflow `image:`/`services:`
  are a long-standing, still-open feature request (dependabot-core#5541).
- GitHub's "supported ecosystems" table lists Docker Compose as a *separate*
  package manager (`docker-compose`), distinct from `docker`.
- `tools/install-helpers.sh:23` is a **shell variable** (`PGVECTOR_IMAGE=...`).
  No Dependabot ecosystem scans shell variables in a `.sh` file, so this
  reference is unconditionally un-automated.

**Reproduced drift surface (identical pgvector digest; only some are managed):**
- managed by the `docker` ecosystem: `Dockerfile:8,26`, `Dockerfile.sandbox:15`.
- **not** managed: `tools/install-helpers.sh:23`, `.github/workflows/ci.yml:30`,
  `.github/workflows/ci.yml:133`, `.github/workflows/workflow-trial.yml:39`,
  `docker-compose.yml:5` (unless a `docker-compose` entry is added).

**Consequence:** the comments tell a maintainer that these digests will be
proposed for bump; they will silently go stale and receive no security bump.
The repointed digests are correct *today* (see "Positive verifications"), so
this is a future-drift + doc-accuracy issue, not a currently-wrong pin.

**Falsification attempt:** (a) If the `docker` ecosystem parsed workflow YAML,
`ci.yml` would get PRs — upstream `FileFetcher#fetch_candidate_dockerfiles`
only selects `DOCKER_REGEXP` names, so it does not. (b) If a `docker-compose`
entry existed, `docker-compose.yml` would be covered — it does not exist.
(c) If a different ecosystem scanned `.sh`, the installer pin would be covered —
none does. Could not falsify.

**Severity note:** P3 as written (drift + misleading docs). Becomes P2 if the
release process is understood to *rely* on those PRs for CVE bumps.

---

### F2 — P3 — E2E-gate docs still show the pre-guard (flagless) invocation the code now refuses

**Code side (new behaviour):**
- `tools/e2e_gate.py:21-27` (module docstring) and `:2874-2905` (`main`): the
  gate resolves the DB from `--db-url`, else `MACHINA_DATABASE_URL`/`DATABASE_URL`
  (`:214-230`), and **exits 2** when (i) no URL is resolved, (ii) the URL is not
  postgres, or (iii) a non-test DB name resolves from the environment without
  `--db-url` (`:2889-2896`). `--db-url` is declared at `:2846-2853`.

**Doc side (unchanged):**
- `README.md:760-764`:
  ```bash
  python tools/e2e_gate.py                  # j1..j9; ...
  python tools/e2e_gate.py --journeys j1,j5 # cheap real subset
  python tools/e2e_gate.py --json /tmp/e2e.json
  ```
- `docs/verification/E2E_PROTOCOL.md:41-50` shows the same flagless forms, and
  `:52` describes the DB as "repo `.env` PostgreSQL".

**Why they disagree:** the repo's own default `.env`/`.env.example:6` database
is `machina` (no "test"), so the documented command exits 2 with **no journey
run** against the default setup. The `--db-url` consent is mandatory there.

**Provenance:** already recorded as a residual by the fix author —
`docs/verification/proofs/codebase_review_round4_2026-10-14/R4-095_infra.md:210-214`
("documented usages … still show the flagless invocation"), assigned to R4-120.
It is still unfixed at HEAD.

**Falsification attempt:** if `.env.example`/documented default DB contained
"test", the flagless command would run; it names `machina`. If the docs had been
updated by the R4-130 docs bundle, the lines would mention `--db-url`; they do
not. Not falsified.

---

### F3 — P3 — `--db-url` guard is bypassable with an empty value, and its "fail-closed" predicate is substring-based

**F3a — empty `--db-url` defeats the consent check.**
`tools/e2e_gate.py:224-225` (`resolve_db_url`) ignores a blank flag
(`if explicit and explicit.strip()`), falling back to the environment. But
`main` keys the consent test on `args.db_url is None`:
- `:2889` `if args.db_url is None and not is_test_database(db_name):` → guard
- `:2897` `if args.db_url is not None:` → export as consent
With `--db-url ""` (or `--db-url=`), `args.db_url == ""`, which is **not None**,
so the non-test guard is skipped and the environment URL is exported as if
consented. An operator with a non-test DB in the environment can slip past the
guard with a blank flag. The log at `:2902-2905` even prints "non-test
explicitly allowed by --db-url" for a blank flag.

*Falsification attempt:* argparse yields `""` (not `None`) for `--db-url=`/`--db-url ""`;
confirmed by the `default=None` declaration (`:2847`). Guard is `is None`, so it
is skipped. Not falsified. Fix would be to key consent on the *source*
(`args.db_url is not None and args.db_url.strip()`).

**F3b — "fail-closed" predicate is a substring test.**
`tools/e2e_gate.py:242-244`:
```python
def is_test_database(name: str | None) -> bool:
    """Fail-closed test-database predicate: the name must contain 'test'."""
    return bool(name) and "test" in name.casefold()
```
Names like `latest`, `contest`, `attest`, `protest` contain "test" and are
accepted as test-like without `--db-url`. The behaviour matches the docstring's
literal rule, so this is a calibration nit rather than a code defect, but the
word "fail-closed" overstates safety.

*Falsification attempt:* `"test" in "latest".casefold()` is `True`
(positions 2-5), so a DB literally named `latest` passes. Not falsified.

---

## Open questions / low-severity (Q)

### Q1 — `SECURITY.md` lists the same regression-evidence file twice (possible lost evidence)
- `SECURITY.md:20` — "Runtime → diagnostics | … | `test_observability_payload_boundary.py, test_observability_payload_boundary.py`"
- `SECURITY.md:21` — "Runtime → Docker | … | `test_terminal_safety.py, test_terminal_safety.py`"
Each row names one file twice; likely a second distinct test was dropped when
the column was written. Cosmetic today, but it understates the cited evidence.
*Falsification attempt:* grepped the column — the duplicates are literal, not a
rendering artefact.

### Q2 — Multi-stage `Dockerfile`: second-stage digest may not be auto-bumped
`Dockerfile:8` (builder) and `:26` (runtime) pin the *same* `python:3.11-slim`
digest in a multi-stage build. GitHub's Dependabot docs state Dockerfiles
"may only receive a version update to the first `FROM` directive" and that
multi-stage builds are not fully supported, while
`R4-095_infra.md:132` claims coverage of "Dockerfile (both stages)".
Current `dependabot-core` `docker/file_parser.rb` iterates *every* `FROM` line,
so the limitation may no longer hold. **Could not confirm either way without a
live Dependabot run** — flagged, not asserted.

### Q3 — No CI syntax check for the user-facing installer
`.github/workflows/*.yml` never runs `bash -n install.sh` or shellcheck;
the fix author records this explicitly (`R4-095_infra.md:269, 291`). The helper
is exercised indirectly by `tests/test_installer_env.py`, but a syntax error in
`install.sh` itself would only surface on a real `curl | bash`. Absence, not a
false-pass.

### Q4 — `check_release_artifacts.py` reads a resource it does not require
`tools/check_release_artifacts.py:54-59` `required` omits
`skills/ecc/manifest.json`, but `:66` reads it unconditionally. If hatchling
ever dropped it, the checker would raise a raw `KeyError` instead of a clear
"Missing packaged resources" message. Cosmetic.

### Q5 — `RELEASE_READINESS.md` snapshot is stale (outside the strict slice, FYI)
`docs/verification/RELEASE_READINESS.md:27` uses `--cov=ah` (pre-rename package)
and `:20`/`:70` say the lockfile has "96 packages"; `R4-095_infra.md:271`
records `Resolved 130 packages`. Historical document, but the `--cov=ah` command
would not measure the current package.

---

## Positive verifications (claims checked against source — no finding)

1. **Action SHA pins are correct.** Verified against the GitHub API tag refs
   (annotated tags dereferenced): `actions/checkout@11d5960a…` = v4.4.0;
   `actions/setup-python@a26af69b…` = v5.6.0; `actions/setup-node@49933ea5…`
   = v4.4.0; `actions/upload-artifact@ea165f8d…` = v4.6.2;
   `codecov/codecov-action@b9fd7d16…` = commit of tag v4.6.0. All five
   `uses:` in `ci.yml` (`:50,53,112,119,153,156,161,196`) and
   `workflow-trial.yml:55,58` match.
2. **Base-image digests are correct.** Docker Hub tag API returns
   `python:3.11-slim` → `sha256:e88e9763…` (index; last pushed 2026-10-09,
   matching the "tag digest 2026-10-09" note) and `pgvector/pgvector:pg16` →
   `sha256:7b822b0a…` (index; last pushed 2026-10-01, matching "2026-10-01").
   Both are multi-arch manifest-list digests, so `ubuntu-latest`/multi-arch
   runners pull identical content.
3. **uv lock gate exists and matches its doc.** `ci.yml:63-70` installs
   `uv==0.11.7` then runs `uv lock --check`; `uv.lock` header is
   `version = 1 / revision = 3`; `dependabot.yml:17-20` correctly says pip PRs
   must ship a regenerated lock. `ruff==0.16.9` (pyproject `:49`) matches the
   lock (`uv.lock:1334`) and the README claim (`README.md:690`).
4. **Wheel telemetry exclusion is coherent across all four files.**
   `pyproject.toml:70-72` selects `skills` via `packages` (honouring `exclude`)
   and excludes `skills/telemetry.json[.lock]`; `check_release_artifacts.py:87-89`
   fails CI if either ships; `.gitignore:71-72` and `.dockerignore:66-68` also
   exclude them. The sdist `force-include` (`pyproject.toml:74-75`) is documented
   as a known residual, consistent with the checker allowing it for `.tar.gz`.
5. **env-file parent guard runs before any redirection.**
   `install.sh:204-216` (`die` on missing/non-writable parent) precedes the
   `cp .env.example` / `: > "$ENV_FILE"` writes at `:314-322`. `ENV_FILE` is
   resolved once from the startup dir (`:199-203`, `resolve_env_file` in
   `install-helpers.sh:144-154`) and exported at `:220`.
6. **Installer path-quoting helpers are correct for their target layers.**
   `sh_dquote_escape` (`install.sh:452-454`) escapes `\ " $ `` ` for the
   double-quoted shim word (`:563-566`); `fish_quote` (`:455-467`) leaves safe
   paths bare and `\`/`'`-escapes otherwise; `ps_squote_escape` (`:468-470`)
   doubles `'` for the single-quoted PowerShell string (`:537`). Windows venv
   `Scripts` handling and the `machina.exe` fallback (`:249-250`, `:298`) are
   consistent with a Windows venv layout.
7. **The single CI grep gate cannot false-pass.**
   `ci.yml:89-94`: `set -o pipefail`; pytest's own exit status gates the step
   (only the one Windows-only test is selected, so a failure/error exits
   non-zero before `grep`), then `grep -q "1 skipped"` asserts the expected
   skip. No other `grep` gate exists in the workflows.
8. **Compose validation is credential-free and satisfiable.**
   `ci.yml:209` passes `MACHINA_API_KEY=ci-placeholder` for the
   `${MACHINA_API_KEY:?…}` interpolation at `docker-compose.yml:42` with
   `--env-file /dev/null`; the `docker` job builds both Dockerfiles and
   smoke-tests the CLI.
9. **ECC pins/counts are consistent.** `tools/vendor_ecc.py:16` PIN
   `4eb71d92…` equals `skills/ecc/manifest.json` `revision`; 43 tokens in the
   frozenset minus the `frozenset(` line = **42** curated skills, matching the
   42 shipped dirs and `CHANGELOG.md:8` / `README.md:153,310`. The agents
   manifest holds 71 role entries; `agent_def.py:62-116` (3 builtins) +
   `_ecc_agents()` = 74, matching `check_release_artifacts.py:108-109`.
10. **`.env.example` ↔ installer default are the same URL**, so
    `db_url_is_installer_default` (`install-helpers.sh:191-197`) replaces only
    the documented placeholder and preserves custom URLs
    (`start_docker_db:262-275`, `start_brew_db:344-346`).

---

## Slice coverage checklist

| File | Read | Notes |
|---|---|---|
| `install.sh` (622 lines) | full | F2/F3, positives 5-6, 10 |
| `tools/install-helpers.sh` (431) | full | F1, positives 5-6, 10 |
| `tools/e2e_gate.py` (2980) | full | F2/F3 |
| `tools/vendor_ecc.py` (217) | full | positive 9 |
| `tools/check_release_artifacts.py` (145) | full | Q4, positive 4 |
| `Dockerfile`, `Dockerfile.sandbox` | full | F1, Q2 |
| `docker-compose.yml` | full | F1, positive 8 |
| `.github/workflows/ci.yml`, `workflow-trial.yml` | full | F1, positives 1,3,7,8 |
| `.github/dependabot.yml` | full | F1 |
| `pyproject.toml` | full | positive 4 |
| `.env.example`, `.gitignore` | full | positives 4,10 |
| `OPERATIONS.md`, `SECURITY.md`, `CHANGELOG.md` | full | Q1 |
| `README.md` (changed sections) | 100-155, 744-783 + grep | F2 |
| `FEATURE_LEDGER.md` | spot (infra rows) | no contradiction found |
| `docs/verification/E2E_PROTOCOL.md` | full | F2 |

## Verdict

No P1/P2 defects in the reviewed infra slice. Four P3 issues (F1–F3 + the
`SECURITY.md` duplicate) are documentation/robustness rather than runtime
breakage; the digest/SHA pins, lockfile gate, telemetry exclusion and installer
guards all hold up against source and upstream registries. The most actionable
item is **F1** (Dependabot coverage overclaim): either add a `docker-compose`
ecosystem entry and stop claiming workflow/installer coverage, or drop the
automation claims from the four comment sites.
