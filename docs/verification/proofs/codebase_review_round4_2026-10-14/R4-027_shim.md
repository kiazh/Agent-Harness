# R4-027 — Uninstall shim matching must be path-segment aware

- Round: 4 · Date: 2026-10-14 · Lane: R4-SHIM (sole writer of the
  `machina/cli/__init__.py::_shim_points_at` region and `tests/test_uninstall.py`).
- Probe base: `git rev-parse HEAD` = `cdc5232` for the "unfixed" extraction; working tree
  for GREEN runs (shared worktree — other lanes own other regions of `cli/__init__.py`).
- Finding: `_shim_points_at` (~`:1968`) used plain substring containment
  (`app_root.rstrip("/\\").lower() in shim_text.lower()`). During `uninstall`
  (`~:2225-2237`) a shim pointing at a **sibling checkout**
  (`/Users/x/machina-old/.venv/bin/machina`) matched app_root `/Users/x/machina`, so the
  wrong checkout's `~/.local/bin/machina` shim was unlinked.

## Root cause

One sentence: containment matched `app_root` inside a longer path segment, so any sibling
directory sharing the prefix (`machina-old`, `machina2`, `machinaX`, …) was indistinguishable
from the checkout being uninstalled.

## Fix

`machina/cli/__init__.py` — `_shim_points_at` only (region diff):

```diff
 def _shim_points_at(shim_text: str, app_root: str) -> bool:
-    """True when a ``~/.local/bin/machina`` shim execs an ``machina`` inside ``app_root``."""
-    return app_root.rstrip("/\\").lower() in shim_text.lower()
+    """True when a ``~/.local/bin/machina`` shim execs an ``machina`` inside ``app_root``.
+
+    Whole path segments only, case-insensitively: ``/x/machina`` never matches a
+    shim pointing at a sibling checkout such as ``/x/machina-old``.
+    """
+    root = app_root.rstrip("/\\").lower()
+    text = shim_text.lower()
+    for at in range(len(text) - len(root) + 1):
+        if not text.startswith(root, at):
+            continue
+        end = at + len(root)
+        if not any(c.isalnum() or c in "-._~" for c in text[at - 1 : at] + text[end : end + 1]):
+            return True
+    return False
```

Semantics: an occurrence counts only when **neither neighbour** can continue a filename
segment (`isalnum()` ∪ `-._~`). Before-boundary rejects `/x/pre-machina`-style fusions
(`old/x/machina` is not the absolute `/x/machina`); after-boundary rejects
`machina-old`/`machina2`/`machinaX`/`machina_old`/`machina.bak`. Case-folding and
trailing-separator stripping are unchanged; quotes, separators, spaces (the
`cd <root> && …` form) and end-of-string are accepted boundaries. The old pins are
untouched: exact/ancestor cases stay `True`, unrelated paths stay `False`.

## RED/GREEN table

| # | Case (`tests/test_uninstall.py`) | Unfixed | Fixed |
|---|---|---|---|
| a | `test_shim_points_at_rejects_sibling_checkouts` (`-old`, `2`, `X`, `_old`, `.bak`) | **FAIL** | PASS |
| a′ | `test_shim_points_at_rejects_segment_extension_before_match` (`old/x/machina`) | **FAIL** | PASS |
| b | `test_shim_points_at_case_insensitive_whole_segments` (neg: `MACHINA2`) | **FAIL** | PASS |
|c | `test_shim_points_at_accepts_exact_and_containing_paths` (bare/quote/nested/`cd &&`) | PASS | PASS |
| d | same test — path inside app_root and containing dir | PASS | PASS |
| e | existing `test_shim_points_at_matches_prefix_case_insensitively` (`/APP` vs `/app`) | PASS | PASS |
| f | `test_shim_points_at_strips_trailing_separator_from_root` (pos + sibling neg) | **FAIL** | PASS |
| g | `test_shim_points_at_skips_rejected_occurrence_and_matches_later_one` | PASS | PASS |
| h | E2E `test_uninstall_keeps_shim_of_sibling_checkout` (real `uninstall`, temp HOME) | **FAIL** | PASS |
| i | E2E `test_uninstall_full_yes_to_all_removes_everything` (own-shim deletion) | PASS | PASS |

Both `test_shim_points_at_matches_prefix_case_insensitively` (the pre-existing pin at
`tests/test_uninstall.py:27-31`) and the two pre-existing uninstall flow tests are unchanged
and green.

## RED — unfixed code

1. Scratch probe against the verbatim HEAD extraction (`R4-027_unfixed_shim.py`,
   `git show HEAD:machina/cli/__init__.py`):

```text
$ .venv/bin/python R4-027_probe_shim_matching.py unfixed R4-027_unfixed_shim.py
[FAIL] sibling machina-old: got=True expected=False
[FAIL] sibling machina2: got=True expected=False
[FAIL] sibling machinaX: got=True expected=False
[FAIL] sibling machina_old: got=True expected=False
[FAIL] sibling machina.bak: got=True expected=False
...
[FAIL] case-insensitive neg: got=True expected=False
[FAIL] trailing sep root neg: got=True expected=False
[FAIL] relative prefix before: got=True expected=False
[FAIL] windows sibling neg: got=True expected=False
10/19 passed; 9 failed
```

(full log: `R4-027_red_probe.log`)

2. Full test file against the **HEAD implementation injected** (`R4-027_red_shim_plugin.py`
   replaces `machina.cli._shim_points_at` with the extracted HEAD body before collection):

```text
$ PYTHONPATH=<scratch> pytest tests/test_uninstall.py -q -p no:cacheprovider -p red_shim_plugin
FAILED tests/test_uninstall.py::test_shim_points_at_rejects_sibling_checkouts
FAILED tests/test_uninstall.py::test_shim_points_at_rejects_segment_extension_before_match
FAILED tests/test_uninstall.py::test_shim_points_at_case_insensitive_whole_segments
FAILED tests/test_uninstall.py::test_shim_points_at_strips_trailing_separator_from_root
FAILED tests/test_uninstall.py::test_uninstall_keeps_shim_of_sibling_checkout
5 failed, 15 passed in 0.40s
```

(full log: `R4-027_red_pytest.txt` — exactly the new adversarial negatives fail; every other
test, including the old pins, passes)

3. E2E through the real `machina uninstall` command with the old function injected
   (`R4-027_e2e_sibling_shim.py`, temp HOME + temp checkout + sibling `-old`):

```text
$ .venv/bin/python R4-027_e2e_sibling_shim.py old sibling R4-027_unfixed_shim.py
mode=old scenario=sibling exit=0 shim_survives=False expected=False PASS   <- reproduced bug: sibling shim deleted
$ .venv/bin/python R4-027_e2e_sibling_shim.py old own R4-027_unfixed_shim.py
mode=old scenario=own exit=0 shim_survives=False expected=False PASS
```

## GREEN — fixed code

1. Probe against the live module:

```text
$ .venv/bin/python R4-027_probe_shim_matching.py live
19/19 passed; 0 failed
```

(full log: `R4-027_green_probe.log`)

2. E2E through the real command with the fixed function:

```text
$ .venv/bin/python R4-027_e2e_sibling_shim.py live sibling
mode=live scenario=sibling exit=0 shim_survives=True expected=True PASS    <- sibling shim survives
$ .venv/bin/python R4-027_e2e_sibling_shim.py live own
mode=live scenario=own exit=0 shim_survives=False expected=False PASS      <- own shim still deleted
```

(full log: `R4-027_e2e_probe.txt` — command output says “Shim … points elsewhere, leaving it.”)

3. Test file:

```text
$ pytest tests/test_uninstall.py -q -p no:cacheprovider
20 passed in 0.40s
```

(full log: `R4-027_green_pytest.txt`)

## Proof type

Adversarial with temp-HOME fixtures (matrix row R4-027): sibling/case/suffix/prefix/trailing
variants must not match; exact + ancestor + containing-dir forms must match. The E2E case
drives the real Typer `uninstall` command (real questions, real plan, real shim unlink/keep
branch) with only `Path.home`, `_uninstall_app_root`, `find_env_file` and the docker/brew
probes substituted — the seam under test (`_shim_points_at`) is executed, never mocked.
The RED run additionally injects the verbatim HEAD function (plugin/scratch script) so the
failure is attributable to exactly the old matching mechanism.

## Checks

```text
$ pytest tests/test_uninstall.py -q -p no:cacheprovider
20 passed in 0.40s
$ pytest tests/test_architecture_layers.py -q -p no:cacheprovider
27 passed in 0.96s
$ pytest tests/test_fm7_cli.py -q -p no:cacheprovider
20 passed, 1 xfailed in 0.77s
$ ruff format --check machina/cli/__init__.py tests/test_uninstall.py
2 files already formatted
$ ruff check machina/cli/__init__.py tests/test_uninstall.py
All checks passed!
$ wc -l machina/cli/__init__.py
    2329 machina/cli/__init__.py
```

(full log: `R4-027_checks.txt`; the `test_fm7_cli.py` xfail is the pre-existing W6
env-file-escape gap at `tests/test_fm7_cli.py:325`, targeted by R4-043 — the file is
unmodified in this lane.)

Line-count note: `tests/test_architecture_layers.py` has **no ceiling entry for
`cli/__init__.py`** today (`GOD_FILE_CEILINGS` and the provider-total ratchet cover
`core/provider` only; verified by grep). This fix adds 8 net lines (function 3 → 11) to
`machina/cli/__init__.py` inside the sole-owned region; the file's overall diff vs HEAD also
carries other lanes' changes, so any cli ratchet added later in the round must be baselined
after all lanes land.

## Residual risks

- The neighbour class is `isalnum() ∪ {-._~}`. Sibling names that differ with characters
  outside that class remain treated as boundaries (e.g. a hypothetical `machina+old` would
  match). Deliberate: the after-boundary must stay permissive for the required
  `cd <root> && …` (space) and quote/end forms; the class covers every named adversarial
  variant plus Unicode letters (`isalnum`).
- Path-string forms that differ only by separator style between `app_root` and the shim
  (e.g. `C:\x\machina` vs `C:/x/machina`) were already unsupported by the substring
  implementation and are unchanged; `install.sh` bakes the same POSIX spelling it resolves.
- `app_root` equal to a bare separator (`/`) degenerates to a near-empty pattern; not
  reachable from `_uninstall_app_root()` (a real checkout path) and not worth extra branch
  complexity.
