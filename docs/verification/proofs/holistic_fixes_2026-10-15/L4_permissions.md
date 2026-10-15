# L4 — permissions/tools security fixes (holistic review 2026-10-15)

Lane L4 of the holistic-fix wave. Findings **H14** (P1) and **H18** (P2) from
[CODEBASE_REVIEW_HOLISTIC_2026-10-15.md](../../CODEBASE_REVIEW_HOLISTIC_2026-10-15.md).

- Base commit for the lane: `3ef6d5b` (worktree HEAD when the lane closed;
  the review snapshot was `64fc75f`).
- Files changed: `machina/permissions/policy.py`, `machina/tools/builtins.py`.
- Tests added: `tests/test_holistic_review_l4_permissions_2026_10_15.py`
  (12 tests). No commit, no push.
- Final hashes:

| File | SHA256 |
| --- | --- |
| `machina/permissions/policy.py` | `b4dcb73ade5189281157bb580f99afb6e44fecc488f696513dd1394ac842ed45` |
| `machina/tools/builtins.py` | `be96d29f1a3585875b6217e6c020e91a0567b88937c011e7e9d234ea0021f53d` |
| `tests/test_holistic_review_l4_permissions_2026_10_15.py` | `a009c2c9dd5e14fbc4a96645a9528dafdae44b512663c875f474d653ce2d229b` |

Evidence bundles (this directory):

| Path | What it is |
| --- | --- |
| `L4_h14_repro.py` | End-to-end H14 reproduction (policy decision → real `cp -RL` → `read_file`). Exit 0 = fix detected the escape. |
| `L4_h18_repro.py` | End-to-end H18 reproduction (controlled swap at the matcher-spawn boundary). Exit 0 = outside bytes stayed hidden. |
| `logs/L4_h14_before.log`, `logs/L4_h18_before.log` | Both escapes reproduce on pristine `3ef6d5b` (exit 1). |
| `logs/L4_h14_after.log`, `logs/L4_h18_after.log` | Both closed on the fixed tree (exit 0). |
| `logs/L4_regression_red.log` | New test file run on pristine `3ef6d5b`: **8 failed, 4 passed**. |
| `logs/L4_regression_green.log` | Same test file on the fixed tree: **12 passed**. |

Before/after logs were produced from identical script and test versions: the
"before" runs used a detached `git worktree` at `3ef6d5b` carrying only the
untracked test/script files; the worktree was removed afterwards.

---

## H14 (P1) — recursive dereferencing copy bypasses containment and credential checks

**Reproduced? YES.** `logs/L4_h14_before.log`:

```
policy: cp -RL source clone -> allowed (routine trusted project command)
cp exit=0 ...
clone/data is_symlink=False is_file=True
read_file('clone/data') exposes outside bytes: True
  content: 'OUTSIDE-PRIVATE-CONTENT-9f2c'
RESULT: ESCAPE — policy allowed the dereferencing copy
```

With a workspace `source/data` symlink to an outside private file, the policy
returned `allowed` ("routine trusted project command") for `cp -RL source
clone`. The real copy materialized the outside bytes as a regular workspace
file, and the public `read_file` tool returned them. No race involved.

**Why it happened.** `_sensitive_capabilities` classified only the top-level
operands. The R02 descendant scan (`_tree_contains_component`) checked just
`.git`/`.machina` names and never resolved dereferenced descendants, so under
`-L` neither workspace containment nor `sensitive_path` applied to the tree
the copy actually reads.

**Fix (policy.py).** `_tree_contains_component` is replaced by
`_recursive_copy_reach(source, follow_symlinks=…, workspace=root)`, which
walks the tree the copy will actually write and returns a `_CopyReach`:

- `protected` — reachable `.git`/`.machina` component (unchanged R02
  behavior) → `security_settings`.
- `external` — a descendant **reached through a dereferenced link** resolves
  outside the request-bound workspace root → `opaque_execution_review` (the
  established "effective read set not enforceably constrained" review bucket,
  cf. the R01/R4-060 recursive-read decisions).
- `sensitive` — a descendant reached through a dereferenced link matches the
  shared `sensitive_path` definition (credential material, dotenv families,
  private dirs, credential stores) → `credential_access`.

Details that matter:

- Classification applies only where the copy materializes new content:
  `reached_via_link = follow_symlinks and (via_link or is_link)`. A tree
  already walked literally is walked again with the link-reach flag, so a
  sensitive ancestry (e.g. `etc/passwd`) is still classified when a link
  renames it (`source/data -> source/etc` would leak `clone/data/passwd`);
  the visited set is keyed on `(resolved identity, reach mode)`.
- Fail-closed behavior is preserved: inspection-cap truncation, unreadable
  directories, and resolution failures still force review (R4-062).
- `-H` operand links and `mv` are unchanged: `-H` descendants are not
  dereferenced, and the operand itself is already resolved by the existing
  operand checks; `mv` never dereferences.

**Behavior changes (explicit callouts).**

- `cp -RL` / `cp -R --dereference` (also through `uv run` and shell stages)
  now requires an explicit digest-bound approval when the dereferenced
  descendant tree reaches outside the workspace root or reads credential
  material. These copies were previously `allowed` outright.
- Required capability identities are existing ones (`opaque_execution_review`,
  `credential_access`); no new capability names, and `REVIEW_CAPABILITIES` is
  unchanged. Approval grants must cover them exactly, like every other
  sensitive capability.
- Unchanged by design, pinned by tests: no-dereference copies (`cp -R`,
  `-RP`, `-Ra`) stay routine even with an outside-target link (the link is
  copied as a link and later reads through it are rejected); in-workspace
  links stay routine; literal sensitive names keep the copy allowed because
  the copied name still gates later reads.
- Out of scope (noted, not fixed): a *hard link* to outside content is not a
  symlink, so `-L` semantics do not apply; that pre-existing path is
  orthogonal to this finding.

**Tests.** `test_recursive_dereferencing_copy_of_outside_file_requires_review`
(4 command spellings incl. nested link and `uv run`),
`test_recursive_dereferencing_copy_of_credential_target_requires_review`
(inside `.env`, outside `id_rsa`),
`test_recursive_dereferencing_copy_of_outside_directory_requires_review`,
`test_recursive_dereferencing_copy_of_sensitive_pair_through_link_requires_review`,
plus the pinned-routine cases and a direct `cp -RL` materialization
demonstration of the hazard.

---

## H18 (P2) — file search validates a path and later reopens it without descriptor protection

**Reproduced? YES.** `logs/L4_h18_before.log`:

```
controlled swap fired: True
link.txt now targets: .../outside/private.txt
search result: 'Found 1 matches:\n.../link.txt:1: OUTSIDE-PRIVATE-CONTENT-9f2c'
RESULT: ESCAPE — the matcher followed the swapped pathname
```

`search_files` validated `link.txt` (an in-workspace symlink), appended the
**original pathname** to the payload, and the isolated matcher re-opened it
with plain `open()`. A symlink swap performed after validation — controlled
in the test at the subprocess-spawn boundary — redirected the read to an
outside private file whose bytes appeared in the results.

**Fix (builtins.py).** The read boundary now carries and verifies the
validated identity:

- `_collect_files` opens every candidate through `_open_secure_read` — the
  same protected descriptor traversal ordinary file tools use (O_NOFOLLOW,
  regular-file check, dev/ino vs lstat, realpath containment) — and records
  `{path, dev, ino}` from the opened descriptor.
- The stdlib matcher opens with `O_RDONLY | O_NONBLOCK`, requires
  `S_ISREG`, and skips any entry whose `(st_dev, st_ino)` does not match the
  parent-validated identity. A swapped pathname therefore resolves to a
  different inode and is skipped, never followed. `O_NONBLOCK` + `S_ISREG`
  also keep a swapped FIFO from blocking the matcher.
- If an ancestor is swapped, the parent's `_open_secure_read` realpath
  containment check already fails closed at validation; the matcher identity
  check closes the remaining validation→read window.

**Behavior changes (explicit callouts).**

- Files whose on-disk identity changes between validation and the matcher's
  read are skipped (fail closed) instead of read. In-workspace symlinks are
  still searched (identity matches), and the `path:line: text` report shape
  is unchanged.
- The parent performs one extra open/stat per collected candidate; the
  matcher payload now carries `{path, dev, ino}` instead of bare paths (the
  wire format is internal to the tool).
- The identity check relies on POSIX `st_dev`/`st_ino` semantics — the same
  assumption `machina/tools/file.py::_verify_fd_matches_path` already makes
  for read/write paths.

**Tests.** `test_search_files_ignores_a_swapped_symlink` (controlled swap at
the spawn boundary; asserts the swap really retargets a plain read but the
search exposes nothing) and `test_search_files_still_follows_in_workspace_symlinks`
(no false negatives).

---

## Verification runs

- **RED (pristine `3ef6d5b` worktree, final test file):** 8 failed, 4 passed —
  `logs/L4_regression_red.log`; both repro scripts exit 1 —
  `logs/L4_h14_before.log`, `logs/L4_h18_before.log`.
- **GREEN (fixed tree):** `12 passed` —
  `logs/L4_regression_green.log`; both repro scripts exit 0 —
  `logs/L4_h14_after.log`, `logs/L4_h18_after.log`.
- **Focused suites in the shared worktree** (with `MACHINA_TEST_DATABASE_URL`,
  `-p no:cacheprovider`):
  - 253 passed: `test_holistic_review_l4_permissions_2026_10_15.py`,
    `test_permission_risk_policy.py`, `test_review_security.py`,
    `test_bugfixes_tool_cache_redos.py`, `test_final_review_file.py`,
    `test_text_search.py`.
  - 257 passed: policy/permission batch (`test_fm3_policy_scope.py`,
    `test_execution_context_boundary.py`, `test_debug_permissions_hardening.py`,
    `test_final_review_permissions_views.py`,
    `test_permissions_store_regressions.py`, `test_security.py`).
  - 145 passed, 1 skipped: `test_integration_permissions.py`,
    `test_r4_090_tools.py`, `test_terminal_safety.py`,
    `test_shell_commands.py`.
- **Isolated worktree (only the L4 diff on `3ef6d5b`):** the 15-file focused
  set above plus `test_code_quality.py`, `test_architecture_layers.py`,
  `test_leaf_independence.py`, `test_architecture_pairs.py`,
  `test_property_based.py` → 493 passed, 1 skipped; a further 351 passed
  (`test_basic.py`, `test_wiring_parity.py`, `test_soulspec.py`,
  `test_ecc_agents.py`, `test_comprehensive.py`, `test_audit_regressions.py`,
  `test_audit_remediation.py`, `test_review_round2_fixes.py`,
  `test_final_review_fixes_2026_10_15.py`, `test_fm3_hardening.py`,
  `test_authority_merge.py`, `test_connectors_policy.py`). The single skip is
  the UI-dependency test (`ui/node_modules` is absent in a fresh worktree).
- **Ruff:** `ruff check` and `ruff format --check` clean on both source files
  and the new test file.

### Out-of-lane observations (not caused by L4)

Recorded for the wave's reconciliation; all were reproduced on the unfixed
tree and are outside L4's file ownership:

- `tests/test_architecture_layers.py::test_deferred_import_count_ratchet`
  fails in the shared worktree (`census 718 > cap 717`). The AST census delta
  comes from `machina/services.py` (+1, a parallel lane); L4 adds **zero**
  deferred imports, and the ratchet passes in the isolated L4-only worktree.
- `test_god_file_ceilings_only_fall[core/provider/copilot.py‑431]`,
  `test_provider_package_total_only_falls`, and the three
  `test_handoff_regressions.py` compression failures come from other lanes'
  uncommitted edits (`core/provider/copilot.py`, `services.py` /
  `gateway/features/sessions.py`).
