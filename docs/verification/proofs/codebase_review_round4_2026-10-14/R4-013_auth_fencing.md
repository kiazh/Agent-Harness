# R4-013 / R4-014 / R4-015 — subscription auth-store fencing

Date: 2026-10-14 · lane: R4-AUTH · status: implemented, verified, **not committed**
(per instruction). Evidence logs under `R4-013_logs/` next to this file.

## Scope

- **R4-013** — `machina/security/claude_auth.py`: per-store lock across
  read→POST→publish, peer adoption under the lock, generation bump on logout,
  publish gated on unchanged generation + store fingerprint, post-error
  adoption of the winner's pair, import fencing; CLI `claude_logout` routes
  through the fenced helper.
- **R4-014** — `machina/security/gemini_auth.py`: same fencing;
  `update_project` is a read-merge-write under the same lock.
- **R4-015** — `machina/security/copilot_auth.py`: fenced logout; a device
  flow completing after logout raises `copilot_auth_cancelled` and writes
  nothing; the documented `gh auth token` re-import behavior is preserved.

Simplified from the plan's literal step "(6) bump generation under lock →
unlink": taking the store lock in logout would deadlock the acceptance gate
(see Design decision below). The bump is still durable/atomic and ordered
before the unlink; the refresh's publish gate + post-save undo keep the
fence sound.

## Root causes (probed at HEAD `cdc5232`)

1. `claude_auth.refresh` / `gemini_auth.refresh` read the store, POSTed with
   no serialization, and saved unconditionally afterwards. Two concurrent
   callers both replayed the same single-use rotating Claude refresh token
   (one got `invalid_grant` instead of adopting the winner) and a response
   landing after `claude_logout` recreated the revoked store;
   `gemini_auth.update_project` wrote the snapshot it read before an
   interleaved refresh (resurrecting the expired access token).
2. `claude_logout` / `gemini_logout` / `copilot_logout` in
   `machina/cli/auth.py` were plain `path.unlink()`: no fence of any kind.
3. The copilot device flow persisted its token with an unconditional
   `save_tokens` after the CLI logout window, so an authorization completing
   after logout recreated the store.

## Design

New single authoritative helper `machina/security/store_fence.py` (190 lines,
stdlib only) extracted from the `chatgpt_auth` idiom:

| primitive | role |
|---|---|
| `store_lock(target)` | exclusive per-store `flock` (msvcrt on Windows) on `<resolved store>.lock`; held across the whole read → provider call → publish section |
| `credential_generation(path)` | durable counter (`<store>.generation`, atomic mkstemp+fsync+replace) + in-process fallback |
| `invalidate_credentials(path)` | bump, used by logout paths |
| `store_fingerprint(target)` | `(present, sha256)` of the raw store for out-of-band change detection |
| `fenced_logout(path)` | bump **then** unlink; returns whether a store was removed |

### Design decision: fenced logout does NOT take `store_lock`

- Refreshes hold the lock across the provider round trip (that is what
  guarantees one POST per rotation). The acceptance test
  `test_claude_refresh_finishing_after_logout_cannot_recreate_the_store`
  requires `claude_logout()` to complete — bump + unlink — **while** the
  refresh is blocked inside the token endpoint; a lock-acquiring logout would
  deadlock until the handler's 10 s wait expires and the test fails.
- Verified `flock(LOCK_EX)` on a second fd in the same process blocks on this
  platform (experiment: second thread never acquired while the first held the
  lock), so "under the same lock" is not achievable without the deadlock.
- No death-wedge by design either: a lock-hung logout would be a DoS behind a
  slow/hung token endpoint, so lock-free logout is also the better product
  behavior.
- Residual interleave (refresh passes its gate, logout bumps+unlinks, refresh
  saves) is closed by a post-save re-check: refresh re-reads the generation
  after `save_tokens` and, when it changed, removes the store **only if it
  still holds exactly the value it just wrote** (content-guarded undo). Same
  pattern in `copilot_auth.publish_device_token`.

### Claude (`claude_auth.py`)

- `refresh`: `store_lock` wraps generation capture, store re-read, peer
  adoption (different, usable refresh token ⇒ adopt, no POST), POST, and the
  publish gate. Post-response: generation changed → return the fresh
  in-memory pair, never save; fingerprint changed without a generation bump →
  adopt a usable peer pair or leave the store as found; else save + post-save
  undo. Non-200 responses first try post-error adoption (store now holds a
  different, usable pair ⇒ return it) before surfacing `invalid_grant`.
- `import_from_claude_code`: early fast-path kept; generation captured before
  reading the Claude Code file; publication under the lock re-checks the
  generation and the existing-tokens rule.
- New `logout(*, path=None)` → `store_fence.fenced_logout(target)`.

### Gemini (`gemini_auth.py`)

- `update_project`: read-merge-write under `store_lock` (the merge re-reads
  immediately before saving); a concurrent refresh can no longer be clobbered.
- `refresh`: same lock + generation + fingerprint gate + post-save undo as
  Claude (no peer/refresh-token adoption — Google desktop clients do not
  rotate, per the module's documented behavior).
- `import_from_gemini_cli`: fenced like Claude's import.
- New `logout(*, path=None)`.

### Copilot (`copilot_auth.py`)

- New `credential_generation(path=None)` (mirrors `chatgpt_auth`).
- New `logout(*, path=None)`; CLI routes through it. The gh credential store
  is untouched; `resolve_tokens` still re-imports it on the next call.
- New `publish_device_token(token, *, account, generation, path=None)`: under
  the lock, a changed generation raises `copilot_auth_cancelled` and writes
  nothing (content-guarded undo for the gate race). `copilot_login` captures
  the generation **before** `device_flow_start` and publishes through it.

### CLI (`machina/cli/auth.py`)

`claude_logout`, `gemini_logout`, `copilot_logout` now call the store's
fenced `logout()` instead of `path.unlink()`; `copilot_login` publishes
through `publish_device_token`. No public CLI surface changed (same messages,
same exit codes).

## Files changed / created

| file | state | delta |
|---|---|---|
| `machina/security/store_fence.py` | **new** | 190 lines |
| `machina/security/claude_auth.py` | modified | +154/−87 |
| `machina/security/gemini_auth.py` | modified | +145/−88 |
| `machina/security/copilot_auth.py` | modified | +62 |
| `machina/cli/auth.py` | modified | +26/−21 |
| `tests/test_auth_store_adversarial.py` | xfails flipped to hard tests | docstring + 3 marker removals |
| `tests/test_oauth_cli_auth.py` | +28 | new R4-015 CLI regression test |
| `docs/.../R4-013_logs/*` | evidence | logs, probes |

`machina/security/chatgpt_auth.py` was **not touched** (its migration to the
helper is a tracked Stage-4 item). No new dependencies; store JSON formats,
file modes (0600/0700), atomic replace and error codes unchanged.

## RED → GREEN

| evidence | command | result |
|---|---|---|
| RED baseline (`R4-013_logs/red_baseline.txt`) | `pytest tests/test_auth_store_adversarial.py -q -p no:cacheprovider` on HEAD | `4 passed, 3 xfailed` |
| GREEN ×3 (`R4-013_logs/green_acceptance_x3.txt`, `final_battery.txt`) | same command, after the fix | `7 passed` ×3 (6.0–8.1 s) |
| affected suites (`R4-013_logs/final_battery.txt`) | chatgpt/claude/gemini/copilot auth + oauth CLI + turn ownership | `110 passed` |
| architecture + cycles + leaves | `test_architecture_layers.py test_config_import_cycle.py test_leaf_independence.py` | `78 passed` |
| ruff (`final_battery.txt`) | `ruff format --check` + `ruff check` on all 7 touched files | clean |

### xfail → pass transitions (markers removed, assertions untouched)

| test | documented bug | now |
|---|---|---|
| `test_claude_concurrent_refresh_submits_one_post_per_rotation` | 2 POSTs per rotation, no peer adoption | hard pass (one POST, both callers adopt the winner, 5 repeats in-test) |
| `test_claude_refresh_finishing_after_logout_cannot_recreate_the_store` | late refresh resurrected the store after CLI logout | hard pass (store stays removed, status signed-out) |
| `test_gemini_update_project_interleave_keeps_the_fresh_access_token` | update_project clobbered the interleaved fresh token | hard pass (merge serializes behind the refresh) |

### Supplementary probes (`R4-013_logs/probe_extra.txt`)

Paths the 7-test gate does not cover, run as real store files + mocked HTTP at
the transport boundary:

- `probe_gemini_refresh_after_logout` — PASS (logout during in-flight gemini
  refresh: store stays removed; caller keeps fresh in-memory tokens).
- `probe_claude_post_error_adoption` — PASS (rejected consumed token adopts
  the peer's rotated pair instead of raising `invalid_grant`).
- `probe_generation_bump_is_durable[claude|gemini]` — PASS (bump exactly +1,
  persisted next to the store; second logout returns `False` but still bumps).

### Mock boundary

HTTP is faked only via `httpx.MockTransport` through the modules' existing
`client=` injection point. Real tmp-dir store files, real `flock`, real
barrier synchronization, real CLI invocation (`CliRunner`). No auth-module
function is patched in the gate file.

### Ratchet / god-file

- `test_architecture_layers.py` fully green (78 passed). New module is a
  small stdlib-only leaf; `security/*` is not in `GOD_FILE_CEILINGS`.
- Tree-wide deferred-import census: 692/692; every file touched by this lane
  contributes **0** (`R4-013_logs/ratchet_census.txt`).
- Transient note: for a few minutes mid-lane an intermediate state of a
  concurrent lane (`machina/cli/__init__.py`, +2 deferred imports, R4-017)
  failed the tree-wide census (694 > 692); it self-resolved when that lane
  landed its itemized state. No R4-AUTH file ever affected the count.

## Residual risks / non-goals

- **Lock-free logout interleave**: the window where a refresh saves *after*
  the logout's unlink is swept by the post-save undo; if the undo's unlink
  itself fails (permissions), the error propagates loudly on the
  refresh/next-logout path rather than being swallowed. The gate race is
  closed on the tested paths; no externally injectable barrier exists between
  the gate check and save, so it is reasoned + content-guarded rather than
  barrier-tested.
- **`exchange_code` (claude/gemini) is not generation-fenced** — the plan's
  RED list only required refresh + import fencing; a login exchange racing a
  logout mid-flight is still unfenced (chatgpt has the pattern to port when
  scoped). Not attempted here to keep the diff minimal and APIs unchanged.
- **Copilot `gh auth token` import is not fenced** by design: the deliverable
  requires the documented re-import behavior to keep working, and gh's token
  is long-lived (no rotation), so the file-import fence used for
  claude/gemini would only suppress a user-requested import race. Flagged for
  the reviewer.
- **Cross-process contention tests** are R4-105's scope (the flock is
  cross-process capable; the gate exercises thread-level races with real
  locks).
- Lock acquisition has no timeout yet (R4-049, P2); blocking behavior matches
  the existing chatgpt implementation.
- `R4-040/041/042` (chatgpt pre-logout window, exchange false-cancel,
  redaction registration) remain open; `chatgpt_auth.py` untouched.

## No commit

Per instruction: no `git add`/`git commit` performed. The worktree contains
this lane's changes plus other concurrent lanes' in-flight edits.
