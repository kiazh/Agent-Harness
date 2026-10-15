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
