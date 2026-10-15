# R5-P · Benchmark symlink containment (finding 20, P2 conditional) — FIXED

Lane files: `machina/research/head_to_head.py` (copy/containment path only)
+ new test `tests/test_head_to_head_symlink_containment.py`. No commit (coordinator commits).

## Verdict: finding CONFIRMED (reproduced) → fixed, fail-closed

## RED (unfixed module via `git show HEAD:...` + PYTHONPATH shadow)

Manual copy/hash/allowlist replay — `R5-P_red_manual.log`:

- Fixture `task1/src/link.py -> /outside/victim.txt` (absolute escaping link).
- `shutil.copytree(task_dir, run_dir, symlinks=True)` preserves the link;
  the run-copy link resolves to the outside victim.
- `editable_paths()` returns `['src/calc.py', 'src/link.py']` — the link is
  followed (`is_file()`) and excluded from the integrity hash.
- Simulated harness write through `run/src/link.py` overwrites the
  **original** `victim.txt` (`ORIGINAL` → `HARNESS-PWNED`) while
  `hash_tree` before == after → integrity check misses the escape.

Test-level RED — `R5-P_red_unfixed.log` (new tests vs unfixed module):
2× `Failed: DID NOT RAISE RuntimeError` (file-link and dir-link attacks
proceed with no rejection) + 2× `AttributeError: escaping_symlinks`
(abs-link and control tests need the new gate, absent pre-fix).

## Fix (`machina/research/head_to_head.py`, reject don't materialize)

- New `escaping_symlinks(root)`: `os.walk(..., followlinks=False)` surfaces
  file *and* directory links without descending; `os.path.realpath`
  resolves absolute links, `..`, chains, and dangling links; anything not
  rooted at `root` is reported (fixture-relative, sorted).
- New `assert_no_escaping_symlinks(root, *, label)` → typed `RuntimeError`.
- `run_one`: fixture gate **before** any copy (no harness ever runs), plus
  run-copy gate **after** `copytree` that discards the copy and re-raises.
  The second gate matters: an *absolute link into the pristine fixture*
  passes the fixture gate but aliases the original after copying — verified
  by test 3. Rejection (not materialization) chosen: smallest change,
  fail-closed, no silent content rewriting. Internal *relative* links
  resolve inside the tree and are unaffected.
- Behavior change (called out): fixtures containing escaping/absolute
  symlinks now raise `RuntimeError` instead of running. Ordinary fixtures
  (no symlinks, or internal relative links) are byte-for-byte unaffected —
  R4-020 suite still 7/7 green.

## GREEN — `R5-P_green.log` (11 passed)

- New file, 4 tests: escaping file link rejected + original untouched + no
  run copy; escaping dir link rejected + outside dir clean; absolute
  in-fixture link rejected at run-copy gate + copy discarded; control —
  internal relative link, genuine fix through the alias scores
  `success=true` (1 passed/0 failed) while the pristine original stays buggy.
- Existing `tests/test_head_to_head_grader_adversarial.py`: 7 passed.
- `py_compile` clean on both files.

## Logs

- `R5-P_red_manual.log` — RED replay vs unfixed module (escape + hash miss).
- `R5-P_red_unfixed.log` — new tests vs unfixed module (no rejection).
- `R5-P_green.log` — new tests + R4-020 suite vs fixed module (11 passed).
