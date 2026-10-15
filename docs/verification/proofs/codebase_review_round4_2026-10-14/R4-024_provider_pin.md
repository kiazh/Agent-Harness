# R4-024 — `/provider remove` stale session pin

- Round: 4 · Date: 2026-10-14 · Proof type: **E2E UI (H-UI scripted)**.
- Finding: `CODEBASE_REVIEW_ROUND4_2026-10-14.md` §13 — "`/provider remove` can leave the session
  pinned to the deleted provider".
- Files changed: `ui/src/app.ts` (+24), `ui/test/state.test.ts` (+187, harness + 2 tests).
  No public-protocol change; `ui/src/protocol.ts` untouched; no edits under `machina/`.

## Root cause (one sentence)

The remove guard (`ui/src/features/settings.ts:223-230`) compared `host.session()` — the App's
`this.current` snapshot — against the removed name, but the snapshot is never refreshed after
`/provider`/`/models` re-pin the session via `session.setModel` (`app.ts` `onConfig` stored only
`footer.model`), so the guard saw the pre-switch provider, skipped the re-pin, and the next turn
executed with the deleted pin (`Unknown provider: <name>` from `machina/core/provider/factory.py:145`).

## Fix (chosen: mirror at the `session.setModel` boundary)

`App.request` now adopts the gateway's authoritative route whenever a `session.setModel` RPC
completes for the current session (new private `adoptSessionRoute`): it merges the response's
non-empty `model`/`provider` into `this.current` via the existing `updateSession` path. The
gateway already returns exactly this truth (`machina/gateway/features/sessions.py:86-91`:
`{"sessionId", "model", "provider"}` — read-only verification; the gateway was not edited).

Why this over re-fetching inside `removeProvider`:

- There is no lightweight session get: `session.resume` re-opens the session and returns up to
  200 history chunks (`machina/gateway/server.py:591-603`), and `session.list` is paginated —
  neither is a "read just the pin" call. Mirroring adds no RPC.
- One change covers all four route-change call sites (`settings.ts:225,385,442`;
  `models.ts:65`) and additionally keeps `host.session()` honest for `/models` grouping and
  other readers, instead of fixing only the remove path.
- The UI stays a faithful mirror: it does not re-derive the fallback (no duplicated gateway
  defaults); it only records what the gateway says the session now is, then the existing guard
  compares live truth.

## RED — unfixed code (log: `R4-024_red.txt`, exit 1)

```text
✖ R4-024 /provider switch mirrors the authoritative route into the session snapshot
  AssertionError: the app snapshot follows the gateway's pinned provider
  + actual - expected
  + 'openrouter'
  - 'acme'

✖ R4-024 provider remove re-pins the session and the next turn uses the fallback
  AssertionError: next turn routes to the fallback; notices: success:Provider "acme" added …
  | success:provider = acme | success:Provider "acme" removed — … | warning:Active provider fell
  back to openrouter (model openrouter/free). … | error:Unknown provider: acme. Choose one of: openrouter
  + actual - expected
  + undefined
  - 'openrouter'
```

Test 1 pins the stale-snapshot mechanism; test 2 runs the full scripted flow
(`/provider add acme …` → `/provider acme` → `/provider remove acme` → Enter on a prompt) and
fails with the user-visible `Unknown provider: acme` turn rejection — the named bug.

## GREEN — fixed code (log: `R4-024_green.txt`, exit 0)

```text
✔ R4-024 /provider switch mirrors the authoritative route into the session snapshot (10.4ms)
✔ R4-024 provider remove re-pins the session and the next turn uses the fallback (10.0ms)
ℹ tests 2 · pass 2 · fail 0
```

The full-flow test asserts, after the removal: `session.setModel` was issued with
`{sessionId, provider: "openrouter", model: "openrouter/free"}` (the re-pin), the next prompt
reaches `prompt.submit` on the fallback route, and no notice matches `/Unknown provider/`.
Determinism: 5 consecutive runs — `pass 2 / fail 0` each.

## Harness / mock boundary (per proof policy)

- `ui/test/state.test.ts` `PinClient extends FakeClient` models the gateway's **durable session
  row**: `session.setModel` updates the row and answers with the authoritative route (mirrors
  `sessions.py:66-91`); `providers.remove` reports the gateway's active-provider fallback
  (mirrors `providers_feature.py:276-314`); `prompt.submit` executes with the row's pinned
  provider and rejects an unknown pin (mirrors `server.py:923-964` → `factory.py:145`).
- Real code under test: `App` (start/submit/request/updateSession), `settings.ts`
  `/provider add|switch|remove` + the re-pin guard, the editor Enter path, transcript notices.
- Only the two dialog prompts are scripted (`app.pick` answers the confirmation, `app.askText`
  returns an empty optional key); the RPC seam and command/session flow stay real. No network
  boundary is exercised (none exists in this flow).

## Checks (log: `R4-024_check_green.txt`)

```text
$ cd ui && npm run check        # typecheck + node tests + eslint
ℹ tests 425 · pass 419 · fail 0 · skipped 6      (exit 0; lint clean)
```

The 6 skips are the pre-existing env-gated `gateway.e2e.test.ts` cases
(`MACHINA_TEST_PYTHON`/`MACHINA_TEST_DATABASE_URL` unset in this environment).

## Residual risks (not fixed here, no claims beyond evidence)

1. A route change made by **another client** while the session is visible is still not noticed
   until the next `switchTo`/resume — pre-existing; the fix makes the App's own route changes
   truthful, which is the reported defect.
2. The guard still re-pins only when the gateway reports `switchedTo` (removed provider was the
   gateway's *active* one). If the session pin and the gateway default have been made to diverge
   (e.g. `/config provider <other>` does not re-pin sessions, then the session's pinned provider
   is removed), the response carries `switchedTo: null` and no re-pin happens. Closing that would
   need gateway knowledge of session pins or duplicated fallback defaults in the UI — explicitly
   out of scope ("do not duplicate the gateway fallback logic").
3. Cosmetic only: the transcript header renders the session captured at switch time, so it does
   not re-render the new route until the next switch/banner (unchanged behavior, unrelated to
   the turn routing).
