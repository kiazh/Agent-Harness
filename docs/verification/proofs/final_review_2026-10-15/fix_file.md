# Final review 2026-10-15 — file tool fix lane (F01, F13, code-review #3/#4)

Baseline: `1e1edbf` + concurrent lanes' worktrees. This lane is the sole writer of
`machina/tools/file.py`, `tests/test_final_review_file.py`, and (only if a pin had
needed it) `tests/test_r4_090_tools.py` / `test_r5_003_atomic_escape.py` /
`test_r5_004_write_cancel.py`. **No pins needed updating.** No other file was
touched; no commit was made.

## Findings fixed

| # | Sev | Finding | Fix |
|---|-----|---------|-----|
| F01 | P1 | dir-fd write chain checked ancestor containment by pathname before `lstat`/`open`; an ancestor swapped to an outside symlink in that window redirected the write outside the base | `_open_parent_dir_fd` now walks from the trusted root with `openat` + `O_NOFOLLOW\|O_DIRECTORY`, creating levels with `mkdirat`; no pathname containment check runs after the root |
| F13 | P2 | `resolve_path` (and `_open_secure_read`) opened non-directory paths with blocking `O_RDONLY`; a writer-less FIFO froze the event loop | stat-first rejection of non-regular files, then nonblocking open + descriptor type re-check (handles a regular→FIFO swap); typed `ToolError` at the `read_file` boundary |
| CR#4 | P2 | `_read_page` read the stream to EOF even for a small page (full-file read on huge logs) | bounded total scan (`_READ_TOTAL_SCAN_LINES = 1000` past the page); exact total when EOF is within budget, honest `at least N` lower bound otherwise |
| CR#3 | P2 | `_atomic_write_file` duplicated `_atomic_write_file_at` | one implementation; `_atomic_write_file` is a thin delegate (opens the parent dir fd when supported, else passes the parent path for the non-dir_fd branch) |

## 1. Adversarial reproduction (RED) — pre-fix code

Deterministic, real-syscall probes (no mocks of the logic under test). Logs in
`logs/`.

### F01 — `logs/probe_f01_before.log`

A real renamer/symlink swap fires exactly inside the old window (after
`_realpath_inside_base(parent)` returns, before `lstat(parent)`/`open`):

```
write result: Successfully wrote 8 characters to a/b/file.txt
swap fired: True
outside content: ATTACKER
ESCAPE
```

The workspace write `a/b/file.txt` was redirected through the swapped ancestor
`a` and overwrote `outside/b/file.txt`. **Confirmed.**

### F13 — `logs/probe_f13_before.log`

`resolve_path("pipe")` and `read_file("pipe")` are run in a daemon thread with a
3 s join so a hang is observable:

```
resolve: HANG (>3s, blocked in open) — event loop would freeze
read: HANG (>3s, blocked in open) — event loop would freeze
```

**Confirmed.**

### CR#4 — `logs/probe_readpage_before.log`

A 100 000-line file, `offset=1, limit=10`, counting `readline` calls:

```
file lines=100000 page_limit=10 -> readline calls=100001 total_reported=100000
FULL-SCAN
```

**Confirmed** (one call per line to EOF).

## 2. GREEN — post-fix

### F01 — `logs/probe_f01_after.log`

Two attack timings against the new descriptor walk (deeper layout was attacked):
swap-to-outside right before the traversed component opens, and swap-to-outside
only after its descriptor is held (at temp creation):

```
[component] fired=True REJECTED: ToolError outside='ORIGINAL' -> no-escape
[temp]      fired=True wrote: Successfully wrote 8 characters to a/b/file.txt outside='ORIGINAL' -> no-escape
```

- `component` timing: the `O_NOFOLLOW|O_DIRECTORY` open of the swapped ancestor
  fails closed (`ToolError`), outside untouched.
- `temp` timing: the write is anchored to the already-open descriptor of the
  renamed (still inside-base) directory; outside untouched.

### F13 — `logs/probe_f13_after.log`

```
resolve: ('ValueError', 0.00064s)
read:    ('ToolError', 0.00057s)
```

Non-regular files are rejected in milliseconds with no blocking.

### CR#4 — `logs/probe_readpage_after.log`

```
file lines=100000 page_limit=10 -> readline calls=1010 total_reported=1010 total_exact=False
BOUNDED
```

`calls == offset + limit - 1 + _READ_TOTAL_SCAN_LINES` (= 1010); the marker
reports `at least 1010`.

## 3. Design notes / behavior changes

- **F01**: The old `_open_parent_dir_fd` is replaced by a root-anchored walk.
  The explicit fallbacks are retained: **outside/full-mode paths** and
  **platforms without `os.supports_dir_fd`** keep the legacy pathname sequence
  (`_reject_symlink_components` → `_secure_mkdir_parents` → pathname probe →
  `_atomic_write_file`). `_dir_fd_supported()` now also requires `os.mkdir`
  dir_fd support (the walk uses `mkdirat`); where that is absent the safe legacy
  path is used.
- **F13**: non-regular files (FIFO/device/socket) are now rejected by
  `resolve_path`/`_open_secure_read` with `ValueError` → `ToolError`. This is a
  deliberate behavior change: previously a character device could pass
  `resolve_path`'s open-and-match check. No in-repo caller targets special files
  through these helpers (grep-verified); `list_files` skips a FIFO instead of
  hanging.
- **CR#4**: for files larger than the scan budget the truncation marker now says
  `of at least N` rather than an exact total. Small files keep exact totals —
  `tests/test_r4_090_tools.py` and `test_final_review_file.py` pins are
  unchanged/passing.
- **CR#3**: `_atomic_write_file` is now a 15-line delegate. On POSIX an outside
  write is anchored to a parent dir fd (same observable result, stronger
  anchoring); the `dir_fd=None` pathname branch preserves the non-dir_fd
  platform fallback byte-for-byte (temp via `mkstemp`, `os.replace`).

## 4. Verification

- `ruff check machina/tools/file.py tests/test_final_review_file.py` — pass
  (`logs/ruff_after.log`).
- `ruff format --check` — both files formatted.
- `bandit -ll machina/tools/file.py` — 0 issues (`logs/bandit_file.log`).
- Focused suites — **183 passed** (`logs/focused_suites_after.log`):
  `test_r4_090_tools.py`, `test_r5_003_atomic_escape.py`,
  `test_r5_004_write_cancel.py`, `test_handoff_regressions.py`,
  `test_final_review_file.py`, `test_fm4_orchestrator.py`, `test_security.py`,
  `test_code_quality.py`, `test_tool_timeouts.py`, `test_basic.py`,
  `test_execution_context_boundary.py`, `test_audit_regressions.py`,
  `test_bug_fixes.py`, `test_v1_repomap_tool.py`,
  `test_bugfixes_tool_cache_redos.py`.
- New evidence tests — **14 passed** (`tests/test_final_review_file.py`),
  covering: F01 at the component and temp seams; F13 for `resolve_path`,
  `read_file`, `_open_secure_read`, and `list_files`; CR#4 bounded/exact totals;
  CR#3 delegation + the `dir_fd=None` pathname branch; and the outside/full-mode
  fallback.

## 5. Concurrent-lane noise (not this lane)

`tests/test_architecture_layers.py` fails on **`core/agent.py`**,
**`core/provider/audit.py`**, **`core/provider/sse.py`** and the
`core/provider` package total (`logs/architecture_layers_after.log`). Those
files are being changed by other review lanes in the same worktree
(`git status`); none is in this lane's write set. `tests/test_progress_monitor.py`,
`test_delegation_contracts.py` and `test_wiring_parity.py` also fail to
collect/run because another lane removed `core.progress.should_write_first`
(code-review #7) — unrelated to `tools/file.py`.

A late run of `tests/test_handoff_regressions.py` also failed
`test_keyset_pagination_counts_and_concurrent_insert` with
`TypeError: ... _page() got an unexpected keyword argument 'before_seq'`
(`machina/core/context.py:727`) — another lane is mid-edit on
`core/context.py` (`git status` shows it modified; the same test passed in the
earlier 183-passed run). Not this lane.

## 6. "Refuted" / deliberately unchanged

- The **`temp`-timing F01 swap** (ancestor replaced after its descriptor is
  held) is *not* an escape and is left as availability-preserving: the write
  lands in the renamed, still-inside-base directory. It is pinned as a GREEN
  test rather than "fixed" to reject.
- The **outside/full-mode** and **non-`dir_fd`** fallbacks remain, explicitly,
  per the brief — covered by `test_write_file_outside_full_mode_still_writes`
  and `test_atomic_write_file_at_pathname_mode_preserved`.
- The claim "R5-B holds" from the adversarial review is *partially refuted*:
  the post-open race was closed but the earlier pathname containment window
  (F01) was real and is now closed.

## Files changed

- `machina/tools/file.py` (fixes F01, F13, CR#3, CR#4)
- `tests/test_final_review_file.py` (new evidence tests)
- `docs/verification/proofs/final_review_2026-10-15/fix_file.md` + `logs/*` (this evidence)

No commits.
