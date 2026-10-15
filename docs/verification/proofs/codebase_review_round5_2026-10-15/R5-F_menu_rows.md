# R5-F · Menu physical-row mapping (finding 10) — lane evidence

Date: 2026-10-15. Lane files: `ui/src/components/Menu.ts`,
`ui/src/features/sessions.ts` (+ new `ui/test/r5-010-menu-rows.test.ts`).
No commit (coordinator commits).

## Verdict: CONFIRMED (RED reproduced) → FIXED (GREEN)

## RED (refute-or-fix gate)

New fake-terminal harness `ui/test/r5-010-menu-rows.test.ts` (4 tests):
real `Menu.render` rows establish the physical layout, then component-local
mouse events (zero-based, `y=0` = first title row, `event.width` = render
width) click exact physical rows.

Against the pre-fix code all 4 fail for the named reason
(`R5-F_red_ui.log`, tests 4 / pass 0 / fail 4). The headline proof:

- Two-line session-title entry (`"first\nsecond-line"`, session `1111…`)
  followed by `"other"` (session `2222…`) in a multi-select menu — the
  `sessions.ts` deletion-picker shape (`host.pickMany → new Menu(...,
  { multiSelect: true })`, `ui/src/app.ts:610`). Clicking physical row 3
  (the title's second line) selected and checked session `2222…`:
  `click on "second-line" must not select the next session (moved to
  ["22222222-aaaa-bbbb-cccc-222222222222"])`. That checked session is what
  the deletion picker deletes on confirm — exactly finding 10.
- Same skew reproduced for the row budget (a 2-physical-row entry plus a
  second item both fit a `visibleRows: 2` budget), for multi-line titles
  (the old `event.y - 2` origin assumed a one-row title + filter), and for
  description-embedded newlines.

Root causes in the old code: `handleMouse` indexed
`visibleWindow(filtered)[event.y - 2]` (one row per item, one-row title
assumption) while `render` expands every embedded `\n` in labels,
descriptions, section headers and the title into physical rows; and
`visibleWindow` sliced the budget by item count instead of physical rows.

## Fix (decision, disclosed)

Chose the shared-map option from the plan, not label normalization:
normalizing labels to one line would contradict the R4-023 pins (Menu must
split embedded newlines into physical rows, `r4-honesty.test.ts`) and
would leave the general hit-test skew for every other caller (model
picker, recall picker, theme picker). So `Menu.ts` now owns one
physical-row→item map used by all three seams:

- `itemRows(item, content, selected)` — raw physical rows per filtered
  item, computed with the exact sanitize/split/checkbox/marker/description
  logic `render` uses; `render` now calls it, so layout and the map cannot
  drift. Spans are stable under selection/checks (markers are fixed width).
- `handleMouse` walks the map from `event.y - titleLines - 1` (multi-line
  titles accounted), so every physical row of an entry — label or
  description continuation — selects/toggles that entry; section rows and
  rows past the window still resolve to `undefined` as before.
- `visibleWindow(visible, content)` budgets **physical rows**: centers the
  selection by row spans, extends forward, then fills leftover budget
  backward. Single-line menus behave exactly as before (span 1 ≡ 1 item);
  an entry taller than the budget still shows the selected entry.

`ui/src/features/sessions.ts`: **no change**. The deletion-picker path
needs none — it builds the same multi-select `Menu` via `host.pickMany`,
which is now correct, and session titles keep rendering truthfully across
their physical rows. Deliberately no title normalization (would be a
display behavior change with no safety gain once hit-testing is exact).

Keyboard nav (`move`/`jump`/filter/`clampSelection`) is item-based and was
already correct; untouched. `pageUp`/`pageDown` step by `rowBudget()`
items — still lands on items, disclosed as unchanged.

## GREEN + regressions

- `R5-F_green_ui.log`: new file, tests 4 / pass 4 / fail 0.
- `R5-F_regression_neighbors.log`: `dialogs.test.ts` + `r4-honesty.test.ts`
  (Menu-adjacent suites incl. the y=3 single-line click pin and the R4-023
  sanitization/split pins), 41 / 41 pass. Additionally ran
  `dialogs + r4-honesty + features + ui`: 214 / 214 pass.
- `eslint` on both touched/new files: clean.
- `tsc --noEmit`: the only 8 errors tree-wide are in
  `ui/test/r5-021_022-ui-affordances.test.ts` (R5-QR lane's in-flight file,
  `showOAuthProgress` FakeHost shape) — zero errors in `Menu.ts` or
  `r5-010-menu-rows.test.ts`. Not touched (not my lane).
- Full UI suite: 455/468 pass. The 7 failures are out-of-lane and
  Menu-independent: 6 in R5-QR's still-RED `r5-021_022` tests (that file
  does not import Menu) and `R4-094(h) /keys` allowlist drift
  (`keys.ts` does not import Menu; UI-vs-gateway allowlist mismatch from
  another lane's in-flight change).

## Files changed (this lane only)

- `ui/src/components/Menu.ts` (modified: +81/−23) — shared row map +
  hit-test + budget fix described above.
- `ui/test/r5-010-menu-rows.test.ts` (new) — 4 RED-first proofs.
- Evidence (new): this doc, `R5-F_red_ui.log`, `R5-F_green_ui.log`,
  `R5-F_regression_neighbors.log` under
  `docs/verification/proofs/codebase_review_round5_2026-10-15/`.

## Behavior/API changes called out

- Clicking any physical row of a multi-line entry now checks/selects that
  entry (was: next entry). Multi-line entries now consume their real row
  count from `visibleRows`/viewport budgets (was: one row each), so fewer
  entries may be visible when titles carry newlines — the selection and
  hint rows stay visible as before. Single-line menus: no observable
  change (verified by the unchanged neighbor pins).
