# Round-4 baseline triage — 2026-10-14

## Backend suite (`baseline_backend.log`)
- Command: `MACHINA_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/machina_test .venv/bin/python -m pytest tests/ -q --tb=line -p no:cacheprovider`
- Result: **4179 passed, 1 skipped, 1 xfailed, 0 failed** in 197.85 s; `EXIT=0`.
  (The single xfail is the strict env-file round-trip pin; nothing to triage.)

## UI check (`baseline_ui.log`)
- Result: **1 failed / 6 skipped / rest passed**, `EXIT=1`.
- Failure: `ui/test/clipboard.test.ts` — "clipboard transfer sends the complete value on
  stdin without shell evaluation" (duration 3035 ms).
- Classification: **load-induced flake, not a product defect.**
  - Isolated run passes in ~0.5 s; failing duration equals `copyText`'s own 3 s timeout.
  - `npm run check` runs test files with node's default concurrency while the backend
    baseline was running on the same machine.
  - The product behaved correctly: the hung child was killed at `copyText`'s bound and the
    recoverable `false` was returned (verified behavior, not a leak).
- Fix (test-only, smallest safe correction): the fake `pbcopy`/`wl-copy`/`xclip` fixtures
  are now a shared `writeClipboardFixture` helper writing `#!/bin/sh` +
  `exec /bin/cat > "<output>"` (boots in milliseconds instead of a Node process ~0.5 s).
  The test still proves stdin-only transfer and non-shell-evaluation: `cat` reads stdin and
  ignores argv, and `$( )` in the value is never evaluated.
- Verification: `node --test test/clipboard.test.ts` → 3/3 pass (~0.8 s);
  `eslint test/clipboard.test.ts` clean; `npm run typecheck` clean.
- Rerun after backend completion (`baseline_ui_rerun.log`): **417 passed / 6 skipped /
  0 failed**, typecheck + eslint clean, `EXIT=0` — flake confirmed fixed.

## Second instance — final Stage-1 UI gate (orchestration, not a defect)
- `stage1_final_ui.log`: same clipboard test failed at 3069 ms (`EXIT=1`) — because the
  backend full suite was launched **in parallel** with the UI gate, recreating the load
  contention the fixture fix was meant to survive.
- Isolation: fixture intact (`13+/6−` in `ui/test/clipboard.test.ts`), **3/3 isolated runs
  pass in ~0.6 s**; product `copyText` 3 s bound untouched.
- Correction: process only — the plan's gate rule now requires **sequential** backend/UI
  gates; no product change, no retries added (retries would hide flakes).
- Action: rerun the UI gate alone after the backend gate completes; record the clean run.
