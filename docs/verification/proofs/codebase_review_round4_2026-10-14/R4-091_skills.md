# R4-091 — Skills/soulspec cluster (hub JSON, delete tree, size guards, Windows, URL ports, classifier)

- **Date:** 2026-10-14/15
- **Base revision:** `91fb8be7b9f94456ac06cdcb3e387c4ee116480e` (working tree shared with
  other in-flight lanes; only the files below were touched — no commit made).
- **Lane files (sole writer):**
  - `machina/skills/registry.py` (+ SkillParser pre-stat cap, `SkillHubError`,
    `_listable` shape gate, `install` type-hardening; `delete_skill` routes
    through `remove_installed_skill` via a deferred import so the registry's
    top-level seam stays ports+observability)
  - `machina/skills/repo_install.py` (+ port-parse inside the typed-URL `try`,
    `_DIR_FSYNC_SUPPORTED` platform flag, `remove_installed_skill`)
  - `machina/skills/workflow.py` (+ `_AMBIGUOUS_TECHNOLOGIES` gate on
    `classify_task(...) == "ambiguous"`, applied to both `coding` and the
    domain-technology loop)
  - `machina/soulspec/schema.py` (+ `_read_package_file` pre-stat,
    `_PACKAGE_REFERENCE_MAX = 64`)
  - Tests: `tests/test_fm6_registry_hardening.py`, `tests/test_repo_install.py`,
    `tests/test_fm6_repo_install.py`, `tests/test_fm3_soulspec_import.py`,
    `tests/test_turn_intent.py`, `tests/test_architecture_layers.py`
    (ratchet delta comment only; cap unchanged at 689)
- **Proof artifacts:** this file, `R4-091_red_pytest.txt`,
  `R4-091_probe.py`, `R4-091_probe_green.txt`
- **No new dependencies; no public CLI/API change** (SkillHubError and
  RepoInstallError are both ValueError subclasses, so existing `except
  ValueError` handlers — CLI publish/install, gateway, tools — are unaffected).
- **Skill lenses applied:** python-patterns (typed-error hierarchy, smallest
  safe deltas), error-handling (fail fast/loud, never swallow, typed errors),
  security-review (pre-stat caps before reads, manifest-ownership proof before
  rmtree, traversal refusal).

## Root causes (one sentence each)

1. **(a)** `SkillHub.install` did `data["name"]` (KeyError on nameless entries)
   and `search`/`list_hub`/`install` called `.lower()`/`[]` on unvalidated hub
   JSON (AttributeError/TypeError on type-corrupt entries) — no typed hub error
   existed at all.
2. **(b)** `delete_skill` unlinked only `SKILL.md`, leaving the repo-install
   manifest and resources behind, so every reinstall refused as "diverged from
   its manifest".
3. **(c)** `SkillParser.parse` called `read_text()` before any size check and
   `soulspec._read_package_file` called `read_bytes()` before the cap, so a
   symlink to a huge file was pulled into memory first; the manifest imposed
   no bound on how many files one import could read.
4. **(d)** activation's `_fsync_dir` refused (with rollback) whenever `os.open`
   on the skills dir failed — which is every call on Windows, where opening a
   directory as an fd is unsupported by platform design.
5. **(e)** `parse_repo_url` accessed `parts.port` outside the `try/except
   ValueError`, so `:99999`/`:abc` escaped as raw stdlib ValueError instead of
   the typed `RepoInstallError`.
6. **(f)** the workflow classifier treated any `_TECHNOLOGIES` token as coding
   intent, so "go ahead" / "go on" / bare "rust" / "swift" activated the full
   coding pipeline — contradicting `classify_task`, which calls exactly those
   bare follow-ons `ambiguous` and refuses to guess.

## Fix (1:1 to the deliverables)

| Bullet | Implementation |
|---|---|
| (a) hub JSON type-hardening | New `SkillHubError(ValueError)`; `publish`/`install`/`_hub_file` raise it. `install` validates name/description/triggers/version/content types before use. New `_listable` shape gate: `list_hub` (sorted, deterministic) skips JSON that is not a renderable entry object (non-dict name, non-str description/author/version/published_at, non-list-of-str tags), exactly like malformed JSON was already skipped — so `search`, `get_hub_telemetry`, and the CLI table can never see raw corrupt entries. Undecodable/unreadable install targets raise `SkillHubError` with chaining. |
| (b) delete removes manifest-owned tree | New `repo_install.remove_installed_skill(skill_dir)`: returns False when no install manifest (ordinary skill — caller keeps its path); otherwise the manifest must decode to an object with a non-empty files mapping, and every recorded `name/...` path must `_confined`-resolve under the tree — traversal/foreign/unsafe manifests raise `RepoInstallError` without deleting anything. `delete_skill` uses it, keeping its old unlink-if-empty path for plain skills. Deferred (function-level) import keeps the registry seam pin (`test_architecture_pairs`) green; deferred-import ratchet documents the +1 (census 687→688 over base; cap stays 689). |
| (c) pre-stat size guards + reference count cap | `SkillParser.parse` stats before reading and rejects `> MAX_CONTENT_SIZE` (1 MB, same ceiling, raw file incl. frontmatter) with the "maximum size" ValueError. `_read_package_file` pre-stats before `read_bytes`, keeping the post-read recheck for TOCTOU growth. `_PACKAGE_REFERENCE_MAX = 64` enforced in `_validate_manifest` (SOUL.md + references ≤ 64 reads). |
| (d) Windows activation fsync best-effort | `_DIR_FSYNC_SUPPORTED = os.name != "nt"`; `_fsync_dir` degrades to best-effort at activation (and staging, already best-effort) only on that platform. POSIX keeps the strict rollback contract (existing `test_skills_dir_fsync_failure_rolls_back` still asserts rollback). Documented in the module comment: on Windows the rename itself is the durability boundary. |
| (e) repo URL port validation | `parts.port` is now evaluated inside the `try` (with a comment noting its lazy ValueError), so `:99999`/`:abc` map to `RepoInstallError`; `:8443` was already refused via the credentials-or-ports guard. |
| (f) workflow classifier gate | `_AMBIGUOUS_TECHNOLOGIES = {"go","rust","swift"}`; when the raw query names one AND `classify_task(routing_query) == "ambiguous"`, those tokens drop out of the `mentioned` set that drives both `coding` and the domain-technology loop. Explicit tasks ("write a Go server") are not ambiguous and keep activating; an inherited stack ("continue" after a Go-task history) never names the token in the raw query, so it is unaffected. |

## RED → GREEN (mandated + adjacent suites)

RED binds unfixed content: the 14 new/changed tests were written first and run
against the pre-fix sources — 15 failed, 5 passed (the 5 passing are the
"keeps working" guards: nonstring-name-typed-error, `:99999` matrix already
covered, explicit-tech-tasks ×3, inherited-stack). Excerpt of the RED failure
modes (`R4-091_red_pytest.txt`): `KeyError: 'name'`, `AttributeError: 'int'
object has no attribute 'lower'`, `reads == []` violated (huge target read),
stale tree surviving delete, `DID NOT RAISE` on corrupt-manifest delete and
oversized symlinked package file, raw `ValueError: Port out of range`,
`PermissionError` escaping activation, `plan.kind == 'coding'` for all five
bare-tech queries.

| Run | Command | Result |
|---|---|---|
| RED | 14 new/changed tests vs pre-fix sources | **15 failed, 5 passed** — `R4-091_red_pytest.txt` |
| GREEN (mandated set + registry hardening + turn intent) | `uv run pytest tests/test_repo_install.py tests/test_skill_registry_cache.py tests/test_skill_runtime.py tests/test_soulspec.py tests/test_soulspec_bugfixes.py tests/test_fm3_soulspec_import.py tests/test_fm6_repo_install.py tests/test_architecture_layers.py tests/test_fm6_registry_hardening.py tests/test_turn_intent.py -q -p no:cacheprovider` | **170 passed** |
| GREEN (adjacent: workflows, triage, ECC, learning, CLI, comprehensive) | `... tests/test_skill_workflows.py tests/test_controller_triage.py tests/test_fm6_workflow_stages.py tests/test_fm6_triggers.py tests/test_ecc_skills.py tests/test_learning_review.py tests/test_fm7_cli.py tests/test_code_review_fixes.py tests/test_comprehensive.py -q -p no:cacheprovider` | **383 passed** |
| GREEN (architecture seam + deferred ratchet) | `tests/test_architecture_layers.py tests/test_architecture_pairs.py` | **43 passed** (the pairs seam test initially caught the new top-level edge; fixed by deferring the import per the codebase's own idiom) |
| ruff | `ruff check` + `ruff format --check` on all 4 sources, 6 test files, probe | all checks passed / already formatted |

Every RED failure fails for the named reason only; the GREEN runs include the
pre-existing guards that pin the unchanged behavior (URL matrix, POSIX fsync
rollback, explicit-tech activation, inherited stack, 64-reference boundary).

## Adversarial probe (supplement to the pinned tests)

`R4-091_probe.py` exercises hostile/boundary cases through the real modules
(exit 0 = all hold; `R4-091_probe_green.txt`):

- **(a)** falsy-tags (`tags: 0`) and non-string `published_at` entries skipped
  from `list_hub`/`get_hub_telemetry`; traversal name `../escape` refused.
- **(b)** owned tree fully removed incl. planted extra file; `beta/../escape`
  and foreign-prefix manifests refuse with the tree intact; plain dir returns
  False; end-to-end `registry.delete_skill` on a manifest-owned tree and on a
  plain learned skill.
- **(c1)** exact-1 MB file parses; cap+1 rejected; 8 MB symlinked target never
  read (read spy empty). **(c2)** 64 references import; 65 rejected;
  256 KB+1 symlinked package file never read.
- **(d)** Windows branch (`_DIR_FSYNC_SUPPORTED=False` + failing `os.open`)
  returns silently; POSIX branch still raises.
- **(e)** `:99999`/`:abc`/`:8443` → `RepoInstallError`; plain link parses.
- **(f)** "go ahead", "go on", "go", "rust", "swift", "and then go ahead",
  "rust vs go" → not coding; three explicit tech tasks → coding; "continue"
  after Go history → coding.

## Residuals / notes for the integrator

1. `tests/test_soulspec_runtime.py` CLI round-trip tests are
   contention-sensitive under parallel DB load in this tree (one failure, then
   a `DeadlockDetectedError`, each on a *different* test per run; every named
   test passes in isolation). Unrelated to this lane (no shared files), noted
   only because the suite was run as an adjacent check.
2. `tests/test_architecture_layers.py` carries this lane's ratchet delta
   comment (+1 deferred import, census 687→688 over the committed base, cap
   unchanged at 689). If another lane moves the deferred census, the procedure
   comment requires them to itemize their own delta — this entry must not be
   silently absorbed.
3. Deferred-import census on this worktree measures 689, i.e. other in-flight
   lanes already added +2 over the base 687 (`core/__init__`, `mcp_server`,
   `tools/file`, minus `core/config`/`gateway/server` hoists); this lane's
   contribution is exactly the one documented +1.
4. `_PACKAGE_REFERENCE_MAX = 64` is a new policy constant; the largest real
   package shape in-repo should be sanity-checked before any future lowering.
5. No commit made, per task rules.
