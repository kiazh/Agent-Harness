# R5-E — Installer path quoting (finding 9) — FIXED

Lane: R5-E · Files owned: `install.sh` (+ new test file only) · No commit (coordinator commits).

## Verdict

**Reproduced, then fixed.** Finding 9 is confirmed as written: the generated
shim interpolated the absolute checkout path into a double-quoted shell word
with no escaping, and the Windows Path command interpolated `windir` into
single-quoted PowerShell strings with no `'`-doubling.

## RED (pre-fix) — `R5-E_red_hostile_paths.log`

Replicated the `install.sh:522-525` shim template verbatim against hostile
`VENV_MACHINA` values and executed each shim with `sh`:

| path chars | shim behavior |
|---|---|
| `$` (`/tmp/evil$a/...`) | expands to empty → `exec` wrong path (rc=126) |
| backticks | **embedded command executed at shim launch** (`touch` sentinel created → `COMMAND EXECUTION DEMONSTRATED`), then wrong path |
| `"` | shim is a syntax error (`unexpected EOF while looking for matching '"'`) |
| `\` | backslash sequences reinterpreted |

PowerShell side (no `pwsh`/`powershell.exe` on this macOS host, so
fixture-level): `'C:\Users\O'Connor\...'` terminates the single-quoted string
early — token analysis in the GREEN log shows the pre-fix command splits the
path across tokens.

The new fixture test also fails against pre-fix `install.sh` (verified via
`git stash`: 2 failed, 12 errors — helpers absent), so it is pinned to the fix.

## Fix (`install.sh`, +47/−6)

Three small quoting helpers, each escaping for exactly the layer the path
lands in (plain paths pass through unchanged):

* `sh_dquote_escape` — escapes `\ " $ \`` for double-quoted POSIX words.
  Used for the shim target and the `export PATH="...:$PATH"` rc line.
* `fish_quote` — safe paths stay bare (keeps the rc idempotency `grep`
  matching across upgrades); anything else is fish single-quoted.
* `ps_squote_escape` — `'` → `''` for PowerShell single-quoted strings.
  Used for both `'$windir_ps'` sites in `register_windows_path` (live command
  + the paste-into-PowerShell hint). No bash-layer escaping is needed there:
  `"$windir_ps"` parameter expansion inserts the value literally.

Not changed: the final `export PATH="$HOME/.local/bin:$PATH"` hint uses the
`$HOME` variable rather than an expanded path, so it needs no escaping.

## GREEN — `R5-E_green_roundtrip.log`, `R5-E_pytest.log`

* `tests/test_installer_quoting.py` (new, 14 tests — no existing test file
  edited): helpers extracted **verbatim** from `install.sh` by name; 8 hostile
  shim cases execute the exact binary with `sh` and create no sentinel;
  sh/fish rc lines re-sourced through a real `bash` put the exact hostile dir
  on `PATH`; O'Connor + `$` Windows path stays a single PS string token;
  normal-path shim/rc outputs asserted byte-identical to pre-fix.
* `14 passed`; existing `tests/test_installer_env.py`: `42 passed`.
* `bash -n` and `sh -n` clean on `install.sh`. `shellcheck` is not installed
  on this host and no `pwsh` exists, so the PS side is fixture-level
  string-token proof (per lane instructions) rather than live execution.

## Files changed

* `install.sh` (sole-writer lane file)
* `tests/test_installer_quoting.py` (new)
* Evidence: this doc + `R5-E_red_hostile_paths.log` +
  `R5-E_green_roundtrip.log` + `R5-E_pytest.log` under
  `docs/verification/proofs/codebase_review_round5_2026-10-15/`

## Limits / follow-ups

* Newline in install path: valid on POSIX but breaks the line-oriented rc
  file; out of scope (finding names `$`, backticks, quotes, backslash).
* Live PowerShell execution (`register_windows_path` on Windows) and
  `shellcheck` still unrun on this host — recommended in CI with Windows/macOS
  matrix per the lane instructions.
