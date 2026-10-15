# R5-B — Atomic-write directory identity (finding 3)

- **Lane:** R5-B (subtask B), per `docs/verification/FIX_PLAN_ROUND5_2026-10-15.md`.
- **Finding:** CODEBASE_REVIEW_CURRENT_2026-10-14 §3, "P1, conditional — Atomic file
  writes can escape a validated parent directory" (`machina/tools/file.py:509`, `:523`,
  `:529`, `:267`, `:289`).
- **Verdict:** **REPRODUCED → FIXED.** Not refuted; the escape is real and observable.
- **Base commit:** `f53476e` (+ other lanes' uncommitted work). No commit made.
- **Sole-writer scope respected:** only `machina/tools/file.py` (atomic-write path) and
  the new `tests/test_r5_003_atomic_escape.py`. `machina/core/agent.py` was NOT touched
  (it was already dirty under the R4-025 lane before this lane started).

## Claim under test

The writer validated an opened target, closed that descriptor, then created a temp file
and replaced the destination **by pathname**. A concurrent local actor could rename the
validated workspace subdirectory away and substitute a symlink to an outside directory
before temp creation; both temp creation and the final `os.replace` would then resolve
outside the approved workspace. Leaf `O_NOFOLLOW` on the discarded probe does not retain
the parent's identity.

## Reproduction (RED, before the fix)

Two independent harnesses, both against real syscalls (no mocked writer seam):

1. **Deterministic interleave** — `R5-B_probe_deterministic.py`.
   Validates the target on a real directory, then at the temp-creation seam performs the
   **real** `os.rename` dir→symlink swap and holds it through the final replace.
   Pre-fix result (`R5-B_probe_deterministic_red.txt`):

   ```
   write returned: Successfully wrote 8 characters to sub/victim.txt
   swapped=True outside/victim.txt='ATTACKER'
   outside_files=['victim.txt']
   VERDICT: ESCAPE
   ```

   Attacker content overwrote `outside/victim.txt` while the tool reported success.

2. **Real concurrent renamer (H-FS style)** — `R5-B_probe_real_renamer.py`.
   A live thread continuously renames `ws/sub` to a symlink to `outside/` while the real
   `write_file` tool runs N=300 writes. Pre-fix, escape is **intermittent** (the vulnerable
   window is narrow): observed **2/10 runs** escape, each also leaving `.victim.txt.*.tmp`
   litter in the outside directory. Example (`R5-B_probe_real_renamer_red.txt`):

   ```
   writes_ok=60 rejected=240 escaped_at=(61, 'ATTACKER-61')
   VERDICT: ESCAPE
   ```

   The intermittent rate is expected: most writes fail closed early (validation observes
   the symlink), only writes whose validation lands just before the swap escape. The
   deterministic harness pins that exact interleaving and escapes every run.

Committed regression tests captured RED pre-fix (`R5-B_pytest_red.txt`):

```
FAILED tests/test_r5_003_atomic_escape.py::test_concurrent_renamer_zero_escapes
FAILED tests/test_r5_003_atomic_escape.py::test_deterministic_swap_at_tmp_creation_stays_inside
2 failed, 2 passed
```

## Fix (GREEN)

Anchor traversal validation, temp creation, and replacement to a **validated directory
descriptor** (`machina/tools/file.py`, atomic-write path only; +220 lines):

- `_open_parent_dir_fd()` — resolves the parent inside the base, `lstat`-rejects a
  symlink/non-directory leaf, opens it `O_DIRECTORY | O_NOFOLLOW`, and requires the fd's
  `(st_ino, st_dev)` to match the validated `lstat` identity (closes the validate→use race
  on the directory itself).
- `_write_anchored_inside()` — existence probe, leaf `O_NOFOLLOW` probe open, TOCTOU
  fstat-vs-`os.stat(..., dir_fd=..., follow_symlinks=False)` verification, hard-link
  refusal, empty-file cleanup, and atomic write all run relative to that pin.
- `_atomic_write_file_at()` — temp file created with `os.open(..., O_CREAT|O_EXCL|
  O_NOFOLLOW, dir_fd=)`, written via `os.fdopen` (preserves the R4-049b/R4-090 fd-close
  and ENOSPC fault seams), fsynced, then published with `os.rename(..., src_dir_fd=,
  dst_dir_fd=)`; `fsync(dir_fd)` after. Cleanup unlinks the temp by `dir_fd`.
- `_verify_fd_at()` — dir-fd-anchored TOCTOU guard (fail closed).
- `write_file()` selects the anchored path for `inside` targets when `_dir_fd_supported()`
  (true on this platform: open/rename/stat/unlink all accept `dir_fd`). The **outside**
  (full-mode) branch and the legacy pathname path are unchanged; `_atomic_write_file()`
  is retained for those. The `O_NOFOLLOW` leaf probe is kept.

Because temp creation and the replace are relative to the pinned descriptor, renaming the
workspace subdirectory to a symlink mid-write cannot redirect them outside the base.

## Verification (GREEN)

| Evidence | Result |
|---|---|
| `R5-B_probe_deterministic_green.txt` | `outside/victim.txt='ORIGINAL'`, no litter, **CONTAINED** |
| `R5-B_probe_real_renamer_green.txt` | **0/12 runs** escaped (12×300 writes); 0 outside temp litter |
| `R5-B_pytest_green_focused.txt` | 60 passed (`test_r5_003_atomic_escape` + handoff/R4-090/fm4 adjacent) |
| `R5-B_pytest_green_sweep.txt` | 534 passed, 1 skipped; 1 fail + 3 errors = shared-DB deadlock / cross-test contention |
| `R5-B_pytest_isolated_rerun.txt` | the 4 flaky items pass **15 passed** in isolation (retry-once-in-isolation rule) |
| `R5-B_ruff_check.txt` / `R5-B_ruff_format.txt` | `All checks passed!` / `2 files already formatted` |

RED→GREEN on the same test file: pre-fix 2 failed / 2 passed; post-fix 4 passed.

No-regression coverage: normal create/overwrite, existing-mode preservation (`0o640`),
nested parent creation, symlinked-parent resolution, ENOSPC-mid-write and fsync-failure
non-destructiveness (R4-090), and every `_os_open` race-hook in
`test_handoff_regressions.py` / `test_fm4_orchestrator.py`.

## Files changed

- `machina/tools/file.py` — atomic-write path: +220 lines (new helpers + `write_file`
  branch). Behavior/API change: inside-base writes now use dir-fd-anchored temp create +
  rename; result string and tool schema unchanged. Outside/full-mode and non-dir_fd
  platforms keep the prior pathname behavior.
- `tests/test_r5_003_atomic_escape.py` — new (4 tests).
- Proofs: `R5-B_atomic_write.md`, `R5-B_probe_deterministic.py`,
  `R5-B_probe_real_renamer.py`, and the `R5-B_*.txt` logs in this directory.

## Notes / residual risk

- `_atomic_write_file()` (legacy pathname temp+replace) remains only for outside-base and
  non-`dir_fd` platforms; those cases keep the pre-existing exposure, disclosed here.
- The deterministic harness hooks `fmod._os_open`/`tempfile.mkstemp` to *place* the real
  swap; no writer logic is mocked (the swap itself uses real syscalls).
- No commit (coordinator commits).
