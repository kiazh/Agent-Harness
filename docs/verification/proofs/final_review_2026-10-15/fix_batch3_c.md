# Fix batch 3 (C) — docs-truth findings I3–I6

Date: 2026-10-15
Lane: FIX (final review batch 3, docs-only). Findings source:
`docs/verification/proofs/final_review_2026-10-15/line_infra_docs.md`.
Rules honoured: documentation/comments only, no behaviour change, no code or
tests touched, no commits.

Sole-writer files touched:
`README.md`, `docs/verification/E2E_PROTOCOL.md`, `SECURITY.md`,
`tools/install-helpers.sh` (comment only), `.github/workflows/ci.yml`
(comment only), `docker-compose.yml` (comment only), `.github/dependabot.yml`.

## Per-finding status

| ID | Finding (line_infra_docs) | Status |
| --- | --- | --- |
| I3 | Pinned-digest Dependabot overclaim (F1) | **Partially fixed in-lane; 1 of 4 claim sites out of lane** (see below) |
| I4 | Flagless `python tools/e2e_gate.py` in README + E2E_PROTOCOL (F2) | **Fixed** |
| I5 | `is_test_database` substring heuristic not documented (F3b) | **Fixed (documented)** |
| I6 | `SECURITY.md` duplicate evidence rows (Q1) | **Fixed (deduped)** |

## I4 — E2E gate invocation now includes `--db-url` and names the guard

Source of truth (unchanged): `tools/e2e_gate.py:217-230` resolves the DB from
`--db-url`, else `MACHINA_DATABASE_URL`/`DATABASE_URL` only; `main`
(`:2876-2899`) exits `2` before any journey when no URL resolves or a non-test
DB resolves without `--db-url`. The default `.env` DB is `machina`
(`.env.example:6`, no "test"), so the old documented command refused.

- `README.md:760-768` — the run block now shows the consenting invocation
  (`DB=postgresql://postgres:postgres@localhost:5432/machina` then
  `--db-url "$DB"`), states the guard, and keeps the test-named-DB flagless
  path. `README.md:775-777` now notes the exit-2 refusal in the exit-code
  sentence.
- `docs/verification/E2E_PROTOCOL.md:41-55` — added the guard paragraph and
  updated all three example commands to `--db-url …`; `:72-74` exit codes now
  include "the database guard".

## I5 — substring heuristic documented honestly

`tools/e2e_gate.py:242-244` still classifies a DB as test-like iff its name
merely contains "test" (`is_test_database`). No code change (out of lane);
documented instead:

- `docs/verification/E2E_PROTOCOL.md:78-84` — new "Database guard" note states
  the environment-only resolution, the exit-2 conditions, and that "test-like"
  is a substring heuristic (`latest`/`contest`/`attest` pass without
  `--db-url`), so `--db-url` is the real consent, not the name check. It also
  records that a blank `--db-url` value is treated as absent — matching the
  current code at `:2876` (the F3a blank-flag bypass is already fixed at HEAD).

## I6 — SECURITY.md duplicate rows deduped

`git blame` shows both duplicates originate at the boundary commit `00df258`
(the file's first commit), i.e. no distinct second test was ever recorded, so
deduping is the honest fix rather than guessing a replacement:

- `SECURITY.md:20` — `test_observability_payload_boundary.py` listed once.
- `SECURITY.md:21` — `test_terminal_safety.py` listed once.

## I3 — Dependabot overclaim corrected to "manual, deliberate" (honest minimal option)

Chosen option: **correct the comments** rather than add a `docker-compose`
Dependabot ecosystem. Adding `docker-compose` would cover only
`docker-compose.yml`; it cannot cover image refs inside workflow `services:`
(Dependabot's docker ecosystem scans only Dockerfile-named files — no workflow
YAML support, dependabot-core#5541) or the shell variable in
`tools/install-helpers.sh`. The comments would still overclaim, so the minimal
fully-honest fix is to state that these digest bumps are manual.

Comment sites corrected in-lane:

- `tools/install-helpers.sh:19-22` — says the docker ecosystem scans only
  Dockerfile-named files, so this shell variable is bumped by hand.
- `.github/workflows/ci.yml:16-23` — Dependabot's github-actions ecosystem
  bumps the action SHAs; the `services:` image digests are **not** covered and
  are bumped by hand.
- `docker-compose.yml:3-5` — the compose digest is not bumped by Dependabot;
  bump by hand.
- `.github/dependabot.yml:1-9` — header (source of truth) restated precisely:
  Dependabot covers workflow action refs + Dockerfile base images only; workflow
  image refs, `docker-compose.yml` and shell variables are bumped by hand.

Out of lane (not a sole-writer file — flagged, not touched):

- `.github/workflows/workflow-trial.yml:38` still reads "Pinned by tag@digest
  (R4-095); Dependabot's docker ecosystem bumps it." **Residual:** the same
  correction is required there; assign to the lane that owns workflow files.

The other Dependabot comment, `.github/workflows/ci.yml:197-201`, was already
accurate (Dockerfile base images) and was left unchanged.

## Verification (no heavy runs)

- `bash -n tools/install-helpers.sh` → **syntax OK** (comment-only edit).
- `git diff --stat` scoped to the seven files above; no code/test files in this
  lane's diff (pre-existing working-tree changes in `machina/**` and `tests/**`
  belong to other lanes and were not touched).
- Line references above re-checked with `grep -n` against the working tree.

## Verdict

I4, I5 and I6 fixed; I3 corrected at three of four claim sites plus the
`dependabot.yml` source-of-truth header, with `workflow-trial.yml:38` remaining
as an out-of-lane residual. Documentation and comments only; no runtime
behaviour changed.
