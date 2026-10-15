# R4-043 / R4-044 / R4-046 / R4-048 — `.env` reader/writer hardening

- **Date:** 2026-10-14
- **Task file:** `docs/verification/FIX_PLAN_ROUND4_TASKS.md` (Round 2 — security)
- **Review findings:** `CODEBASE_REVIEW_ROUND4_2026-10-14.md` §Security — `.env` escape
  round-trip, predictable temp name, lowercase key invisibility, site-packages fallback.
- **Primary proof types:** adversarial FS (H-FS: symlink + crash injection) for R4-044;
  E2E secrets flows for R4-043/046; wheel-layout subprocess simulation for R4-048.
- **No commit** (worktree-only, per instructions).

## Files changed

| File | Change |
|---|---|
| `machina/security/env_file.py` | all four fixes (reader, writer, key matching, resolver) |
| `tests/test_env_file_adversarial.py` | **new** — H-FS symlink/crash/fsync + lowercase + wheel-layout probes |
| `tests/test_fm7_cli.py` | strict xfail flipped to a passing parametrized round-trip suite |
| `tests/test_setup_secrets.py` | E2E gateway secrets round-trip (quotes/backslashes + dotenv parity) and lowercase in-place update |

Diff snapshot: `R4-043_048_env_file.diff` (243 lines). No public API names changed
(`__all__` unchanged; all new helpers private).

## Root causes (one sentence each)

- **R4-044:** `_write_env_lines` built the temp path as `<name>.tmp.<pid>` and opened it
  `O_CREAT|O_WRONLY|O_TRUNC` without `O_EXCL`/`O_NOFOLLOW`, so a pre-planted symlink at
  that predictable path was followed and its target truncated/overwritten, and the write
  never fsynced (file or directory).
- **R4-043:** `read_env_values` stripped the surrounding quotes but never undid `_quote`'s
  `\\`→`\\` and `"`→`\"` escapes, so values containing `\` or `"` read back corrupted
  (and re-writes compounded the escapes).
- **R4-046:** `_env_line_key`/`read_env_values` only matched `^[A-Z][A-Z0-9_]*$`, so a
  pre-existing `openrouter_api_key=…` line was invisible to update/remove and got
  duplicated on write.
- **R4-048:** `find_env_file` and `_allowed_roots` resolved `parents[2]` unconditionally;
  inside an installed wheel that is the interpreter's `site-packages`, so the fallback
  `.env` could be resolved/created (and allowlisted) inside the package directory.

## What changed (`machina/security/env_file.py`)

- **R4-044 — writer:** `tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp",
  dir=target.parent)` (unpredictable, `O_EXCL`-created — a planted file/symlink at any
  guessable name is never followed or reused), explicit `os.fchmod(fd, 0o600)` when
  available, write + `flush` + `os.fsync(file)`, `os.replace(tmp, target)`,
  `_fsync_directory(parent)` (best-effort: some platforms refuse directory fsync/opening
  a dir fd — macOS EINVAL, Windows). Failure path closes the fd and unlinks the temp.
  The old `<name>.tmp.<pid>` path is never touched.
- **R4-043 — reader:** double-quoted values now pass through `_unescape` (single left-to-right
  pass decoding `\\`→`\` and `\"`→`"`); single-quoted values stay literal — the same
  semantics python-dotenv applies, so the live `load_dotenv` loader and the app reader
  agree on every file the writer produces.
- **R4-046 — keys:** `_KEY_RE_CI = ^[A-Za-z][A-Za-z0-9_]{1,63}$` + `_normalize_key_token`
  upper-case the token on read/update/remove; `_validate_key`/`_KEY_RE` still gate
  **writes**, so new lines and rewritten lines use the canonical uppercase spelling.
- **R4-048 — resolver:** new `_checkout_root()` returns the source root only when
  `parents[2]/pyproject.toml` or `parents[2]/.git` exists. `_allowed_roots` appends the
  checkout root only when detected; `find_env_file` falls back to `checkout/.env` in a
  checkout, else to the walk-up origin (`<cwd>/.env`) — never `site-packages`. The
  `MACHINA_ENV_FILE` override and the existing walk-up (first existing `.env` upward)
  are unchanged, and every caller keeps working in installed layouts.
- Side effect: `import tempfile` hoisted from `_allowed_roots` to module level; the
  architecture deferred-import census drops by one (691 ≤ cap 692 — caps only guard
  growth, so no ratchet update is needed).

## RED → GREEN (definitive evidence)

RED command (HEAD's unfixed module swapped in for the run, then restored —
byte-identical restore verified with `cmp`):

```
.venv/bin/python -m pytest tests/test_env_file_adversarial.py \
  tests/test_fm7_cli.py tests/test_setup_secrets.py -q -p no:cacheprovider
```

**RED: 14 failed, 36 passed** (`R4-043_048_red_final.txt`); GREEN: **50 passed**
(`R4-043_048_green_focused.txt`).

| Task | Test (final) | RED reason (unfixed code) | GREEN |
|---|---|---|---|
| 044 | `test_planted_symlink_at_old_tmp_path_is_never_followed` | victim clobbered: `'ORIGINAL=1\n\nNEW_KEY=v\n' == 'VICTIM-CONTENT\n'` | pass |
| 044 | `test_crash_mid_write_never_leaves_an_empty_env` | `.env` emptied by `O_TRUNC` through the planted link: `'' == 'SECRET_VALUE=keep-me\n'` | pass |
| 044 | `test_write_fsyncs_file_and_directory` | `assert 0 >= 2` — the writer never called `fsync` | pass |
| 044 | `test_crash_before_replace_keeps_old_env_and_cleans_temp` (supplement) | passes pre-fix (old code also renames last); guards the fixed cleanup path | pass |
| 046 | `test_lowercase_existing_line_is_updated_in_place` | lowercase line kept and a duplicate uppercase line appended | pass |
| 046 | `test_lowercase_existing_line_is_removed_cleanly` | `removed == []` (line invisible) | pass |
| 046 | `test_read_normalizes_mixed_case_keys` | `{}` returned for a lowercase-only file | pass |
| 046 | `test_provider_removal_deletes_lowercase_env_line` (E2E) | `report["envLineRemoved"] is False` via the real provider store | pass |
| 046 | `test_secrets_set_updates_lowercase_line_in_place` (E2E gateway) | file kept `openrouter_api_key=stale` plus a second uppercase line | pass |
| 043 | `test_env_roundtrip_escaped_values[...]` (3 fail / 4 pass) | quote/backslash combinations read back with literal escapes (`'he said \\"hi\\"…' != 'he said "hi"…'`); backslash-only, whitespace and unicode cases round-tripped pre-fix | pass (all 7) |
| 043 | `test_env_roundtrip_stable_across_rewrites` | escapes compounded on rewrite (`\\\\\\"` drift) | pass |
| 043 | `test_secrets_set_roundtrips_quotes_and_backslashes` (E2E gateway) | `read_env_values` mismatched while `os.environ`/dotenv held the original | pass |
| 048 | `test_installed_wheel_layout_never_targets_site_packages` (E2E subprocess) | `_allowed_roots()` contained the fake `site-packages`, `find_env_file()` resolved `site-packages/.env`, and the write created `site-packages/.env` | pass |

### H-FS harness notes (R4-044)

- **Symlink adversary:** plants a symlink at the *old* predictable path
  (`<name>.tmp.<pid>` → victim file), runs a real `set_env_values`, and asserts the victim
  is byte-identical, the symlink is still a symlink, and the `.env` got the new content.
  Pre-fix the victim received the env payload and `.env` was replaced by the symlink.
- **Crash injection:** plants a symlink at the old path pointing *at* `.env` and replaces
  `os.fdopen` with an exploding file object (fails after `O_TRUNC` would have fired),
  then asserts the `.env` retains its exact prior bytes and no temp file is left. Pre-fix
  the truncation happened before the injected failure → empty `.env`.
- **Replace-crash + fsync spy:** `os.replace` injected failure keeps the old file and
  cleans the temp (supplement); an `os.fsync` spy proves file *and* directory fsync run.

### E2E secrets flows (R4-043/046)

- Gateway `secrets.set(persist=True)` with `he said "hi" and C:\tmp` → `os.environ` value,
  `read_env_values`, and `dotenv_values` (the live loader) all agree.
- Lowercase gateway update: pre-seeded `openrouter_api_key=stale` → single canonical
  `OPENROUTER_API_KEY=sk-fresh` line.
- Provider removal E2E: real `security.providers` store + real `.env`; lowercase
  `machina_provider_acme_api_key=…` line is found and deleted (`envLineRemoved: true`).

### Wheel-layout simulation (R4-048)

A real subprocess (`sys.executable -c`) imports a verbatim copy of the module inside a
fake `venv/lib/python3.14/site-packages/machina/security/` tree (stub
`machina.observability.diagnostics`), with `MACHINA_ENV_FILE` unset and cwd in a clean
scratch dir. The probe asserts the loaded `__file__` really is the fake copy, then prints
`_allowed_roots()`, `find_env_file()`, and the path `set_env_values` wrote. Asserts: no
allowed root / resolved path / written path under fake site-packages, no `.env` or `*.tmp`
created there, and the write landed at `<run-dir>/.env`.

## Commands run (all with `.venv/bin/python -m`)

| What | Command | Result | Log |
|---|---|---|---|
| RED (final tests, HEAD module) | `pytest tests/test_env_file_adversarial.py tests/test_fm7_cli.py tests/test_setup_secrets.py -q -p no:cacheprovider` | 14 failed, 36 passed | `R4-043_048_red_final.txt` |
| GREEN (focused) | same command, fixed module | 50 passed | `R4-043_048_green_focused.txt` |
| Required checks | `pytest tests/test_fm7_cli.py tests/test_setup_secrets.py tests/test_installer_env.py -q -p no:cacheprovider` | 83 passed | `R4-043_048_green_required.txt` |
| Architecture guard | `pytest tests/test_architecture_layers.py -q -p no:cacheprovider` | 27 passed | `R4-043_048_green_architecture.txt` |
| Related env/store suites | adversarial + wave3 hotfix + provider store + uninstall + precedence + gateway(+providers) + integration-permissions + personality-growth + cli-turn-e2e + fm3-approval-store + config import/reentrancy | 202 passed, 1 skipped | `R4-043_048_green_related.txt` |
| Code quality / pairs | `pytest tests/test_code_quality.py tests/test_architecture_pairs.py -q -p no:cacheprovider` | 38 passed | — |
| Lint/format | `ruff format` + `ruff check` on the four touched files | clean | `R4-043_048_ruff.txt` |

Earlier partial RED logs (`R4-043_048_red_adversarial.txt`,
`R4-043_046_red_roundtrip.txt`) are superseded by `R4-043_048_red_final.txt`.

## Residual risks / notes

- **Directory fsync is best-effort** by design: macOS returns `EINVAL` for `fsync` on a
  directory fd and Windows refuses to open one; the rename has already happened, so only
  the durability hint is lost. File fsync failures still propagate (fail-loud).
- **Concurrent writers remain last-writer-wins** (unchanged): the writer still takes no
  lock — any lock would be released before the replace. `mkstemp` makes the temp side
  attack-proof, not the race itself (out of scope per the original review note).
- **Reader parity is intentionally limited to the writer's escape set** (`\\`, `\"`);
  hand-written dotenv files using `\n`/`\x..` escapes are decoded by python-dotenv in the
  live loader but not by `read_env_values` — unchanged pre-existing divergence, and the
  writer rejects newline values so it can never emit those sequences.
- **Installed-layout fallback** is `<cwd>/.env` (the walk-up origin). Callers always got
  *some* path before; now it is writable-by-default and outside packages. A read-only cwd
  surfaces the normal `OSError` from the write (fail-loud, no silent redirect).
- **Windows:** `fchmod` is guarded by `hasattr`; the symlink tests follow the existing
  repo convention (other suites create symlinks unconditionally).
- Deferred-import census 692 → 691 (only decreases are allowed); no cap edit needed.
