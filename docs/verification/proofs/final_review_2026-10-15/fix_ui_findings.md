# Final review — UI fix lane (final_review_2026-10-15)

Source finding: `docs/verification/proofs/final_review_2026-10-15/line_ui.md`
Lane: FIX (final review). Sole writer of the UI files listed below.

## Verdict

All confirmed UI findings fixed with the smallest safe change. Full UI gate is
green (`npm run check` exits 0): typecheck + 476 tests (470 pass, 0 fail,
6 skipped) + eslint. No behavior change beyond the fixes. No commits.

## Files changed

Source (6):
- `ui/src/components/OutputViewer.ts` — P3-1 sanitize the retained-output header.
- `ui/src/design/theme.ts` — P3-2 raise `default.dim` and `mono.dim` to AA.
- `ui/src/components.ts` — P3-3 remove dead `windowAroundCursor`.
- `ui/src/components/ToolTurn.ts` — P3-3 remove dead `setOutcome` / `getState`.
- `ui/src/components/OAuthProgress.ts` — P3-4 add missing `return` in the `"c"` branch.
- `ui/src/features/agents.ts` — P3-4 de-shadow `args`, fix indentation drift.

Tests:
- `ui/test/design.test.ts` — assert the AA bound + dim<muted hierarchy for `default`/`mono`.
- `ui/test/final-review-ui-fix.test.ts` — NEW evidence test (P3-1, P3-2, Q-3).

Evidence:
- `docs/verification/proofs/final_review_2026-10-15/fix_ui_findings.md` (this file).
- `docs/verification/proofs/final_review_2026-10-15/logs/ui_check.log` (gate output).

## Per-finding status

| ID | Status | Change |
| --- | --- | --- |
| P3-1 OutputViewer header unsanitized | FIXED | `render()` now wraps `record.name` and `record.toolId.slice(0,8)` in `sanitizeDisplay(...)`. Because the header is one physical row and `sanitizeDisplay` preserves `\n`, embedded newlines are flattened to spaces so a hostile name/ID can never inject a row break. |
| P3-2 `dim` below WCAG AA | FIXED | `default.dim` `#5f5f5f` (3.10:1) → `#7c7c7c` (4.74:1 on `#0a0a0a`); `mono.dim` `#6e6e6e` (3.73:1) → `#808080` (4.82:1 on `#101010`). Both stay below their `muted` (5.01:1 / 7.28:1) to preserve hierarchy. Theme contrast tests now assert `>= 4.5:1` and `dim < muted` for these tokens. |
| Dead code `windowAroundCursor` | FIXED | Removed. Verified zero callers across `ui/src`, `ui/test`, and the repo (only the definition matched). `composerWindow` (its only body) remains. |
| Dead code `setOutcome` / `getState` | FIXED | Removed from `ToolTurn`. Verified zero callers repo-wide (only definitions matched). `settle()` and the `outcome` field remain. |
| Q-3 OAuthProgress `"c"` missing `return` | FIXED | Added `return;` after `onCancel()` so the cancel branch cannot fall through into scroll handling. |
| Q-3 agents.ts shadowed `args` + indentation | FIXED | Block-local `const args` → `const saveArgs` (no longer shadows `run(args, host)`); cases `save`/`history`/`delete` re-indented to the file's canonical switch layout. No logic change. |

### P3-1 rationale detail

`sanitizeDisplay` alone (the literal review suggestion) strips ESC/OSC/CSI but
keeps `\n`. The review explicitly notes an embedded `\n` can reach the overlay's
terminal buffer, so the fix additionally flattens newlines for this single-row
header. A well-behaved name (`read_file` / id `abcdef01`) renders byte-identical
to before (`Output: read_file (abcdef01)`), covered by a drift test.

## Gate

Command (run from `ui/`):

```
npm run check        # tsc --noEmit -p tsconfig.json && node --test test/*.test.ts && eslint src/ test/ --ext .ts
```

Result: `EXIT=0`.

```
ℹ tests 476
ℹ pass 470
ℹ fail 0
ℹ cancelled 0
ℹ skipped 6
ℹ todo 0
```

New/updated tests observed passing:

```
✔ dim text tokens clear WCAG AA and stay below muted (default, mono)
✔ P3-1 retained-output header sanitizes a hostile tool name and id
✔ P3-1 viewer header keeps a well-behaved name verbatim (no behavior drift)
✔ P3-2 default and mono dim clear WCAG AA and stay below muted
✔ Q-3 OAuthProgress 'c' cancels exactly once
```

Full log: `logs/ui_check.log`.

---

## Follow-ups

Relaunched UI review (`line_ui.md`) left three follow-ups open after the lane
above landed. This section records their disposition.

### Files changed

Source (2 — both in this lane's allow-list):
- `ui/src/design/theme.ts` — P3-2 residual: raise `dim` to AA across the registry.
- `ui/src/app.ts` — P3-3 remove dead `inFlightSessionId`; Q-6 keep an inline
  `/provider add <name> <url> <key>` secret out of editor history.

Tests (3):
- `ui/test/design.test.ts` — AA test now iterates `SKIN_NAMES`.
- `ui/test/final-review-ui-fix.test.ts` — P3-2 assertion now iterates `SKIN_NAMES`.
- `ui/test/final-review-ui-followups.test.ts` — NEW evidence (P3-2 residual, P3-3, Q-6).

Evidence:
- `docs/verification/proofs/final_review_2026-10-15/fix_ui_findings.md` (this section).
- `docs/verification/proofs/final_review_2026-10-15/logs/ui_check_followups.log` (gate output).

### Per-finding status

| ID | Status | Change |
| --- | --- | --- |
| P3-2 residual `dim` below AA on non-default skins | FIXED | The original fix covered only `default`/`mono`, but the runtime-default skin is `codex` (`theme.ts` `currentSkin = "codex"`), whose `dim` was `#5d5d66` = 3.02:1 on `#0b0b0c`. Every registered skin's `dim` is now lifted to >= 4.5:1 on its own background while staying below `muted`. |
| P3-3 `App.inFlightSessionId` write-only dead state | FIXED | Field + its three sites removed. Verified by grep (only assign/self-compare/clear matched, no reader) and pinned by a source assertion in the new evidence test. |
| Q-6 `/provider add name url KEY` key recorded in history | FIXED (documented ignore) | `commandCarriesSecret()` now excludes the unsupported inline-key form from `editor.addToHistory`, so a raw key can never be recalled with Up-arrow; the ignore itself is documented at the call site. |

### P3-2 detail — scope and why every skin

The reviewer's fix direction was explicit: *"apply the same >= 4.5:1 derivation
to every skin's `dim` … and extend the test to iterate `SKIN_NAMES`"*. An
iterating test that asserts the invariant for every skin is only honest if every
skin actually passes, so the registry was brought up to spec, not just the three
named skins:

- `codex` / `opencode` `#5d5d66` (3.02:1) → `#797984` (4.57:1).
- `tokyonight` `#565f89` (2.76:1) → `#7982ab` (4.55:1).
- Plus the other failing skins: `catppuccin`, `dracula`, `gruvbox`, `rosepine`,
  `nord`, `everforest`, `matrix`, `cobalt2`, `ayu`, `nightowl`, `one-dark`,
  `flexoki`, `onedark`, `monokai`, `github`, `solarized`, `kanagawa`,
  `palenight`, `light`.
- Values preserve each skin's hue (HSL lightness lifted toward its own `text`),
  never a hue-shifting white/black mix.

`dim < muted` is part of the invariant, so for the eight skins whose `muted`
itself sat below the new `dim` (`tokyonight`, `catppuccin`, `nord`, `everforest`,
`one-dark`, `flexoki`, `onedark`, `kanagawa`, `palenight`) `muted` was also
lifted minimally (e.g. `palenight` `#676e95` 2.76:1 → `#969bb7` 4.98:1) to keep
the hierarchy ordered. This is an accessibility improvement on its own: those
`muted` tokens were also below AA.

`codex` keeps its `muted` (`CODEX_SEMANTIC_START.muted`, 5.83:1); only its
`dim` changed. `tokens.ts` is out of this lane, so the codex `dim` lift is
inlined in `theme.ts` with a comment rather than editing the token file.

Test changes: `ui/test/design.test.ts` and `ui/test/final-review-ui-fix.test.ts`
now loop `SKIN_NAMES` and assert `dim >= 4.5` and `dim < muted` for every skin
(and that the count equals the registry size), so `codex`/`opencode`/`tokyonight`
can never regress behind the runtime default again. `ui/test/final-review-ui-followups.test.ts`
additionally pins `getSkin() === "codex"` and its passing `dim`.

### P3-3 detail

`grep -rn inFlightSessionId ui/src ui/test` matched only the field declaration
and its three mutations (`submitTurn` assign, `clearInFlight` self-compare +
clear) — no reader made a decision, and `cancel()` iterates the whole `inFlight`
set. All four sites are deleted. The surviving guard is
`private readonly inFlight = new Set<string>()`.

### Q-6 detail

`features/settings.ts addProvider()` (out of this lane) reads only `parts[0]`
(name) and `parts[1]` (URL) and always prompts for the key through a masked
editor, so `/provider add name url KEY` drops `KEY` silently. The lane's fix
targets the actual leak: the whole command string was being stored in the
editor's in-memory history (`app.ts` `addToHistory`). `commandCarriesSecret()`
now classifies that form as secret-bearing (case-insensitive, matching the
`/keys set` handling), so the key is never recalled. The ignored-token behavior
is documented at the call site. A hard rejection of the third token would live
in `features/settings.ts`, which is outside this lane's write set.

### Gate

Command (run from `ui/`):

```
npm run check        # tsc --noEmit -p tsconfig.json && node --test test/*.test.ts && eslint src/ test/ --ext .ts
```

Result: `EXIT=0`.

```
ℹ tests 481
ℹ pass 475
ℹ fail 0
ℹ cancelled 0
ℹ skipped 6
ℹ todo 0
```

New/updated tests observed passing:

```
✔ dim text tokens clear WCAG AA and stay below muted (every skin)
✔ P3-2 every skin's dim clears WCAG AA and stays below muted
✔ P3-2 residual: the runtime-default skin is codex and its dim clears AA
✔ P3-2 residual: codex/opencode/tokyonight dims clear AA (named regressions)
✔ P3-3 App.inFlightSessionId dead state is removed
✔ Q-6 commandCarriesSecret keeps inline secrets out of history
✔ P3-2 residual: the registry is non-trivial (guard the iteration itself)
```

Full log: `logs/ui_check_followups.log`.
