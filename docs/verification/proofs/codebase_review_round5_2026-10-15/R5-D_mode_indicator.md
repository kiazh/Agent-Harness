# R5-D · Mode indicator truth + consent disclosure (findings 8 + 2-disclosure)

Lane files: `ui/src/features/mode.ts`, `machina/gateway/features/mode.py`,
`ui/src/app.ts` (mode paths only), `README.md` (mode section only),
`SECURITY.md` (mode section only). No commit (coordinator commits).
`mcp_server` / `permissions/policy.py` untouched (R5-A owns the grant path).

## D1 · Status shows session authority, not the global default (finding 8) — FIXED

### RED (reproduced, then refuted-by-fix)

`R5-D_red_ui.log` (exit 1): both proofs failed on the unfixed tree for the
named reasons —

- **D1a:** bare `/mode` issued `mode.get` with `{}` (no session ID), so the
  gateway answered with the process-global default. After `/mode full`,
  bare `/mode` reported `ask`. (`ui/src/features/mode.ts:16`,
  `machina/gateway/features/mode.py:21-30` returns the global default when
  no session is given — by design; the bug was the client never passing one.)
- **D1b:** `App.switchTo` issued no `mode.get` at all, and `start()` queried
  `mode.get` with `{}` before any session existed (`ui/src/app.ts:293`),
  copying the global default into the footer.

### GREEN (fix)

- `ui/src/features/mode.ts` — `refreshMode` passes `{ sessionId }` of the
  active session (falls back to `{}` only when no session is active).
- `ui/src/app.ts` —
  - deleted the pre-session global `mode.get` in `start()`; after the
    initial session is created/resumed, an awaited session-scoped
    `mode.get` sets the footer (via the `request` wrapper) and drives the
    FULL HOST notice;
  - `switchTo` calls new `refreshSessionMode(session.id)` (fire-and-forget,
    failure-swallowed: a failed refresh keeps the previous indicator rather
    than blanking it) so the footer always renders the visible session's
    governing authority. Same-id re-resume needs no refresh (same session).
  - all other session changes route through `switchTo` (verified by grep;
    `sessions.ts:173` is same-session rename metadata, `adoptSessionRoute`
    is model/provider only).
- `machina/gateway/features/mode.py` — `mode_get` accepts the `session_id`
  alias alongside `sessionId` (matches `approvals_resolve`); no behavior
  change otherwise — the session branch already resolved effective
  authority.

`R5-D_green_ui.log` (exit 0): `R5-D1a`, `R5-D1b` pass.
Regression: new permanent suite `ui/test/r5-mode-indicator.test.ts`;
`app.test.ts` all pass; `r4-094-tui.test.ts` all pass except `R4-094(h)`
(`/keys` allowlist), which was proven pre-existing by re-running it with
this lane's three files stashed (still fails; cause is R5-QR's in-flight
`settings.ts`). `test_features_split.py` 9 passed; DB-backed
`test_session_mode_persistence.py` 6 passed; `tsc` reports no errors in
R5-D files (the only `tsc` errors are in R5-QR's untracked
`r5-021_022-ui-affordances.test.ts`); `eslint` clean on all touched files;
`ruff check` + `ruff format --check` clean on `mode.py`.

## D2 · Full-grant disclosure copy (finding 2, disclosure side) — FIXED per R5-A's pinned semantics

R5-A's evidence read first: `R5-A_green_a3_fastpath.py/.log` —
**disposition: fast path stands, disclosure to R5-D**. A live full-mode
session grant allows credential access, privilege escalation, and
destructive ops with verdict `allowed` / reason `full-mode session grant`.
Re-ran that script against the current tree here: exit 0, all three cases
`allowed (full-mode session grant)`, no-grant full still `pending`.

The old copy ("sensitive actions still ask" / "does not grant blanket
sensitive consent" / "require explicit review even in full mode") was
therefore false under the pinned semantics, and is replaced with exact
disclosure — a full grant is session-scoped **no-approval authority**:

- `ui/src/features/mode.ts:56` (post-activation print) and the `ui/src/app.ts`
  startup notice now state host operations proceed **without further
  approval, including credential access, elevation, and destructive system
  ops**, session-only, revocable via `/approvals revoke`. `R4-094(f)` still
  passes (points at `/approvals revoke`, no `/mode revoke`).
- `README.md` (§ Permissions and shell execution) and `SECURITY.md`
  (§ Modes and privileged operations) rewritten to the same semantics,
  including scope (that session only, never future defaults), termination
  (leave `full` / revoke), and that routing mode alone authorizes nothing.

### D2 caveat for the coordinator

Copy tracks R5-A A3's *pinned* outcome. R5-A's `policy.py` is still
uncommitted/in-flight: if R5-A ever gates sensitive caps before the
fast path instead, this copy must be re-cut to say so. As long as A3's
recorded disposition (fast path stands) holds, the copy above is exact.
