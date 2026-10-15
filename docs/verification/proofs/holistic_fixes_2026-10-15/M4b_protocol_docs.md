# LANE M4b — close-out: `runTimeoutSeconds` on the wire (H22 follow-up)

Date: 2026-10-15 · Repo: `/Users/shadow/machina` · HEAD `3ef6d5b` + the
uncommitted M4 working tree. Owned files only: `ui/src/protocol.ts`,
`communications/ui-gateway-protocol.md` (and their tests — none exist).
No commit, no push.

## Deliverable

1. `ui/src/protocol.ts` — `HarnessListResult` gains the wire field the
   implementation always sends:

   ```ts
   /** Default interop.run deadline in seconds; the UI passes it back as `timeout`. */
   runTimeoutSeconds: number;
   ```

   Required `number` (the current gateway always includes it; the UI feature
   keeps its own runtime guard for drift). `harnesses.ts` is untouched and
   typechecks against the field.
2. `communications/ui-gateway-protocol.md` — the `interop.list` result row now
   includes `runTimeoutSeconds`, and the paragraph documents: it is the
   seconds `interop.run` waits when the request omits `timeout` (currently
   300); `interop.run` `timeout` is seconds, 1–900, default 300, any other
   value is invalid params; the `/harnesses` UI reads the field, passes it
   back as the explicit `timeout`, sizes its client clock past it, and refuses
   to start a run when the field is absent.

Diffstat: 2 files changed, 16 insertions(+), 9 deletions(-).

## Verified against the code (report corrected)

Checked `machina/gateway/features/interop.py` directly, per instruction:

- `interop.list` (`interop_list`, lines 189–222) returns exactly
  `{harnesses: [{key, label, installed, version, running, registered, scope,
  runSupported, runHint}], stateFile, runTimeoutSeconds:
  DEFAULT_RUN_TIMEOUT_SECONDS}` — the nine row fields and both extra fields
  match the doc.
- `interop.run` (`interop_run`, lines 225–245) takes `{platform, prompt,
  timeout?}` (`platform`/`prompt` required via `_str`; `_run_timeout` lines
  134–146 rejects bool/non-number/out-of-range and defaults to 300) and
  returns exactly `{ok, exitCode, command, stdout, stderr, unsupportedReason}`
  — the doc rows were already accurate for these; the `timeout` param is now
  documented as the explicit value the UI sends.
- `interop.enable` / `interop.disable` rows re-verified unchanged and accurate
  (`ActionResult` fields, `scope: project|global`, `dry_run` bool).
- **Correction to the M4 report** (its line 115): `DEFAULT_RUN_TIMEOUT_SECONDS`
  is defined in `machina/gateway/features/interop.py:51`, not in
  `machina/interop.py`; it is what `interop_list` surfaces and `_run_timeout`
  defaults to. `machina/interop.py:1382` (`run_task`) carries its own literal
  `300.0` default. Both are 300.0, and the doc states only the wire behavior
  ("currently 300"), so no internal source is misnamed.
- Other interop-related types: `InteropStatusResult` / `InteropPlatformRow`
  (`interop.status`) expose no deadline in the implementation → no field
  added; `HarnessRunResult` already matches the run payload; protocol.ts
  defines no request-param types, so no other type needed the field.

## Verification

| check | command | result |
| --- | --- | --- |
| UI full | `cd ui && npm run check` (typecheck + 506 tests + eslint) | **exit 0** — 500 pass / 6 skipped / 0 fail (`logs/M4b_ui_npm_check_green.log`) |
| Python focused (wire payloads) | `.venv/bin/python -m pytest tests/test_harnesses_feature.py -p no:cacheprovider -q` | **17 passed** (`logs/M4b_python_feature_green.log`) |

Typecheck accepts `harnesses.ts` reading the new field (it is part of the
typecheck run); the H22 regression tests (`interop.run client clock outlasts
the backend run deadline`, the absence-refusal test) pass in the full run.

## Notes (not touched)

- `ui/src/features/harnesses.ts` (M4-owned) keeps its local
  `HarnessListBody = HarnessListResult & { runTimeoutSeconds?: unknown }` and
  the comment "read the extra field here until the protocol interface carries
  it" — now redundant since protocol.ts carries the field. Left to its owner;
  checks are green either way.
- `interop.status` is absent from the doc's method table (pre-existing gap,
  not caused by M4); left as is.
