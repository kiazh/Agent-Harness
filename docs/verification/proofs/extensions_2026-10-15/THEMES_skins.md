# THEMES — 12 new skins (2026-10-15)

Lane: themes. **Sole writer** of `ui/src/design/theme.ts`, `ui/test/design.test.ts`,
`ui/test/themes-extended.test.ts` (new). No other source touched; `tokens.ts` and
`settings.ts` needed no change (no token missing, no hardcoded theme list).

## What changed

- `ui/src/design/theme.ts` — 12 new skins appended to `SKINS` (registry now **41**;
  `SKIN_NAMES` derives from `Object.keys(SKINS)`, so `/theme` lists them with no
  hardcoded list anywhere). Each skin replicates the full 10-slot `base` set
  (`text/muted/dim/accent/secondary/success/warning/error/info/neutral`); border,
  card fills, tool fills, selection fill and contrast-checked selection ink are
  derived by the existing helpers — no new tokens needed.
- `ui/test/design.test.ts` — extended the "added theme" registration assertion to
  name the 12 new skins (the dim-AA test already iterates `SKIN_NAMES`).
- `ui/test/themes-extended.test.ts` — **new** file: registry/palette metadata,
  readability, role-distinctness, selection contrast, plus a fake-terminal render
  pass over every new skin.

No new dependencies, no generated values, comments kept minimal, no commits.

## The 12 skins (diversity)

| skin | type | description |
| --- | --- | --- |
| `aurora` | dark · cool | green/violet over midnight blue |
| `ember` | dark · warm | orange ember on charred brown |
| `forest` | dark · nature | mossy greens and violet |
| `glacier` | light · cool | blue on ice white |
| `neon-grid` | dark · synthwave | magenta and cyan neon |
| `paper` | light · muted | ink on warm paper |
| `sepia-crt` | light · retro | amber phosphor on aged paper (sepia) |
| `blueprint` | dark · modern | cyan linework on drafting navy |
| `rose-quartz` | light · warm | warm pink and plum |
| `high-contrast-dark` | dark · a11y pair | pure white on black |
| `high-contrast-light` | light · a11y pair | black on white |
| `sunset-drive` | dark · synthwave | coral and gold over deep plum |

## Contrast numbers (WCAG ratio vs each skin's own `neutral`)

`dim` must clear 4.5:1 **and** stay below `muted`; accent roles must be distinct;
`sel` is the derived selected-row ink on the accent fill (must clear 4.5:1).

| skin | text | muted | dim | sel | accent | secondary | success | warning | error | info |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| aurora | 16.32 | 7.57 | 6.01 | 12.00 | 12.00 | 7.15 | 12.15 | 10.95 | 7.04 | 9.40 |
| ember | 15.53 | 7.66 | 5.97 | 8.13 | 8.13 | 7.48 | 10.04 | 11.21 | 5.44 | 8.03 |
| forest | 14.14 | 7.80 | 6.58 | 9.40 | 9.40 | 6.45 | 10.62 | 9.03 | 6.12 | 10.06 |
| glacier | 12.47 | 5.63 | 4.73 | 4.95 | 4.95 | 4.79 | 4.80 | 5.34 | 5.34 | 5.15 |
| neon-grid | 17.42 | 8.54 | 5.80 | 7.00 | 7.00 | 13.30 | 15.57 | 15.72 | 6.37 | 10.30 |
| paper | 12.95 | 6.02 | 4.80 | 4.83 | 4.83 | 4.98 | 4.54 | 4.55 | 5.34 | 5.33 |
| sepia-crt | 10.23 | 4.84 | 4.55 | 4.93 | 4.93 | 5.13 | 4.72 | 5.12 | 4.96 | 5.16 |
| blueprint | 13.55 | 7.38 | 6.14 | 10.05 | 10.05 | 11.63 | 10.29 | 11.08 | 6.81 | 9.44 |
| rose-quartz | 12.68 | 5.36 | 4.69 | 5.27 | 5.27 | 4.71 | 5.29 | 5.88 | 4.88 | 5.52 |
| high-contrast-dark | 21.00 | 12.55 | 8.03 | 14.67 | 14.67 | 13.11 | 12.58 | 11.08 | 6.58 | 10.56 |
| high-contrast-light | 21.00 | 8.86 | 6.69 | 10.04 | 10.04 | 9.39 | 6.65 | 6.80 | 5.62 | 6.03 |
| sunset-drive | 15.70 | 8.20 | 5.31 | 6.69 | 6.69 | 10.89 | 11.37 | 12.87 | 6.32 | 9.95 |

Every new skin: `text >= 7`, `muted >= 4.5`, `dim >= 4.5`, `dim < muted`,
all six accent roles `>= 4.5` and pairwise distinct, selection row `>= 4.5`.

## Enumerations audited

`grep` over `ui/src` + `ui/test` for skin lists / pinned counts:

- `ui/src/features/theme.ts` — uses `SKIN_NAMES` (no list). ✔
- `ui/src/app.ts` (`/theme` picker) — uses `SKIN_NAMES` (no list). ✔
- `ui/src/features/settings.ts` — no theme enumeration. ✔
- `ui/test/final-review-ui-followups.test.ts` — lower bound `>= 26`; still holds. ✔
- `ui/test/ui.test.ts` `skins switch palette and art` — presence assertions only. ✔
- `ui/test/design.test.ts` — dim-AA iterates `SKIN_NAMES`; added-theme list extended. ✔

## Gate

`cd ui && npm run check` → **exit 0** (typecheck + test + lint).

```
ℹ tests 496
ℹ suites 0
ℹ pass 490
ℹ fail 0
ℹ cancelled 0
ℹ skipped 6      (env-gated real-gateway tests: MACHINA_TEST_PYTHON / DATABASE_URL)
ℹ todo 0
```

`npm test` added 5 new tests in `themes-extended.test.ts`; the six skips are
pre-existing env-gated gateway tests. Full log: `ui-check.log` (same directory).

### Visual QA

The new file's last test follows the existing `theme-background.test.ts` pattern:
it drives each new skin through the `ThemeBackground` fake-terminal harness,
asserting the emitted 48;… background SGR equals that skin's `neutral` and is
unique across the 12, and that a nested fill reset inside a frame restores the
skin surface. No new harness was built.

```
✔ new skins register with a description, art and a full hex palette
✔ new skins keep text, muted and dim readable with dim below muted
✔ new skins keep every accent role >= 4.5:1 and distinct
✔ new skins keep selected rows >= 4.5:1
✔ new skins paint a unique background through the terminal harness
```

## Reproduce

```
cd ui && npm run check
node --input-type=module -e 'import {SKIN_NAMES} from "./src/design/theme.ts"; console.log(SKIN_NAMES.length)'   # 41
```
