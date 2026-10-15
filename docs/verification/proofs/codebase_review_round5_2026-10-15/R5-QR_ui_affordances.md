# R5-QR — UI affordances (findings 21 + 22)

Lane R5-QR of `docs/verification/FIX_PLAN_ROUND5_2026-10-15.md` (subtasks **Q1** + **R1**).
Source review: `CODEBASE_REVIEW_CURRENT_2026-10-14.md` findings **21** (P3 optional masked
key input) and **22** (P3 memory/learning ID lookup scope).

No commit (coordinator commits). Sole-writer files:
`ui/src/features/settings.ts` (secret-field part), `ui/src/components/Form.ts`,
`ui/src/features/memory.ts`, new `ui/test/r5-021_022-ui-affordances.test.ts`.
Additive supporting edits: `ui/src/features/types.ts` (option type, already consumed by the
committed `app.ts` plumbing in `8b50144`), `ui/src/features/skills.ts` (see ownership note).

## Verdict

| Finding | Result | Evidence |
|---|---|---|
| 21 — optional secret rejects empty; Escape is the undocumented keyless path | **FIXED** | `R5-QR_red_ui.log` → `R5-QR_green_ui.log` |
| 22 — memory forget/share cannot address older IDs, even by full UUID | **FIXED** | `R5-QR_red_ui.log` → `R5-QR_green_ui.log`, `R5-QR_route_green.log` |
| 22 (learning) — approve/reject/revert latest-100 lookup | **FIXED** | `R5-QR_red_learning_ui.log` → `R5-QR_green_ui.log` |

## What changed

### Q1 — optional/keyless secret path (`Form.ts`, `settings.ts`, `types.ts`)
- `SecretFormOptions.allowEmpty?: boolean` (new, default `false`). With it set, a **blank
  Enter / blank Ctrl+S submits `""`** (the explicit keyless path) via `onSubmit`; the error
  path is unchanged when it is not set.
- **Escape still cancels** (`cancel()` → `onCancel`, references cleared) — it is unchanged and
  is *no longer* the way to proceed keyless.
- `AskTextOptions.allowEmpty?: boolean` threaded through `askText`; the committed `app.ts`
  (`8b50144`) already forwards it to `SecretForm`.
- `/provider add` passes `allowEmpty: true` on the API-key prompt, and **an `undefined`
  result (Escape) now cancels the whole add flow** instead of being coerced to `""` and
  continuing. Hint line switches to `enter save · blank for no key · esc cancel`.
- Required secret forms (e.g. `/keys set`) keep rejecting blank (`Value is required.`).

### R1 — full-UUID bypass of bounded windows (`memory.ts`, `skills.ts`)
- `isFullUuid()` accepts canonical / braced / `urn:uuid:` forms (matching the gateway
  `_uuid` parser). A **complete UUID is sent straight to the ownership-checked backend
  call**, never the windowed list.
  - `/memory forget <full-uuid>` → `memory.forget` directly (gateway ownership-checks the
    session agent → `NOT_FOUND` for foreign/missing; `INVALID_PARAMS` for non-UUID).
  - `/memory share <full-uuid>` → `memory.share` directly (identity-gate `PermissionError`
    → `NOT_FOUND`).
  - `/memory approve|reject <full-uuid>` → `memory.approve|reject` directly (no
    `memory.pending` window).
  - `/skills approve|reject|revert <full-uuid>` → `learning.<sub>` directly (route
    `agent_id` ownership-check; no `learning.list` window).
- **Abbreviated IDs keep the bounded lookup** (`memory.list` newest-500 / `pending` 500 /
  `learning.list` latest-100) and a **miss now discloses the scope**: e.g.
  `"… Abbreviated IDs are matched against the newest 500 memories — paste the full ID to
  reach older ones."` via shared `scopedMiss()`.

## Ownership note (skills.ts)
Finding 22's subtask R1 explicitly requires "same for learning approval/revert latest-100".
`ui/src/features/skills.ts` is claimed by no lane in the collision-checked lane map and was
clean (no concurrent editor) at the time. The change mirrors `memory.ts`; `isFullUuid` /
`scopedMiss` are reused from `memory.ts` to keep one implementation. If the coordinator
intended this file to stay untouched, revert `ui/src/features/skills.ts` alone — the
`memory.ts` half of R1 and all of Q1 are independent.

## RED → GREEN

New test file: `ui/test/r5-021_022-ui-affordances.test.ts` (12 tests, 313 lines).

**RED** (`docs/verification/proofs/codebase_review_round5_2026-10-15/R5-QR_red_ui.log`):
my four source files reverted to HEAD (`f53476e`), new test run against unfixed code —
**8 fail for the named reasons**, 1 guard passes:

```
✖ R5-021 optional SecretForm: blank Enter submits empty (explicit keyless path)
✖ R5-021 optional SecretForm: blank Ctrl+S submits empty, Escape still cancels
✔ R5-021 required SecretForm still rejects blank (no keyless path elsewhere)
✖ R5-021 /provider add: blank key proceeds keyless, Escape cancels without a request
✖ R5-022 /memory forget <full-uuid> bypasses the newest-500 window
✖ R5-022 /memory share <full-uuid> bypasses the newest-500 window
✖ R5-022 /memory approve <full-uuid> bypasses the pending window
✖ R5-022 /memory reject <full-uuid> bypasses the pending window
✖ R5-022 abbreviations stay bounded and misses disclose the full-ID path
```

**RED** learning (`R5-QR_red_learning_ui.log`): `skills.ts` reverted to HEAD — **3/3 fail**
(`/skills approve|revert <full-uuid>` never reach the backend; miss message lacks the
full-ID disclosure).

**GREEN** (`R5-QR_green_ui.log`): all **12/12 pass** (9 Q1+memory, 3 learning).

## Route / backend proof (`R5-QR_route_probe.py` → `R5-QR_route_green.log`)
Drives the **real** gateway handlers (`machina/gateway/features/memory.py`) with the `.venv`
interpreter and stub stores — the UI bypass only works because these routes accept a bare
UUID and enforce ownership:

```
forget owned full-UUID: deleted=True, no window lookup exists on this route
forget foreign-agent full-UUID: NOT_FOUND (memory not found for this agent)
forget non-UUID: INVALID_PARAMS (id must be a UUID)
share full-UUID: forwarded to identity-gated bus, no window lookup
share foreign full-UUID: NOT_FOUND (memory not found for this agent)
approve full-UUID: forwarded to approval gate, no pending-window lookup
R5-QR route proof: ALL OK
```
Learning routes (`skills.py:99-139`) were read directly: `learning_reviewer.approve/reject/
revert(_uuid(params, "id"), agent_id=session.agent_id)` raise `LookupError`/`PermissionError`
→ `NOT_FOUND`, so the direct full-UUID call is ownership-safe.

## Static + regression checks
- `npx tsc --noEmit -p tsconfig.json` → exit 0 (whole UI tree).
- `npx eslint` on `Form.ts memory.ts settings.ts skills.ts types.ts` + new test → exit 0.
- Full UI suite `npm test` → **471 tests, 464 pass, 1 fail**. The single failure
  `R4-094(h) /keys suggestions match the gateway's accepted secret allowlist` is **pre-existing
  and out of lane**: it compares `ui/src/features/keys.ts` (unmodified by me) against a
  hardcoded gateway key list, and fails identically with my files reverted to HEAD.

## Behavior / API changes (disclosure)
- Additive options `AskTextOptions.allowEmpty` / `SecretFormOptions.allowEmpty` (default off).
- `/provider add`: blank API key now proceeds as keyless; **Escape now cancels** the flow
  (previously Escape was the undocumented keyless proceed). Required secret forms unchanged.
- `/memory forget|share`, `/memory approve|reject`, `/skills approve|reject|revert`:
  full UUIDs bypass the bounded window; abbreviations unchanged; miss messages now append a
  scope + full-ID disclosure.
- No gateway/route signature changes.

## Files
- `ui/src/components/Form.ts` (+25/−2): `allowEmpty` on `SecretFormOptions`; blank-submit
  keyless path; hint line.
- `ui/src/features/types.ts` (+6/−0): `AskTextOptions.allowEmpty` (required by committed `app.ts`).
- `ui/src/features/settings.ts` (+12/−9): `/provider add` optional-key prompt with
  `allowEmpty`, Escape→cancel, trimmed key handling.
- `ui/src/features/memory.ts` (+61/−7): `isFullUuid`, exported `scopedMiss`, full-UUID bypass
  for forget/share/approve/reject, bounded-lookup disclosure.
- `ui/src/features/skills.ts` (+20/−7): full-UUID bypass + bounded-lookup disclosure for
  approve/reject/revert (ownership note above).
- `ui/test/r5-021_022-ui-affordances.test.ts` (new, 313 lines).
- Evidence: this doc, `R5-QR_red_ui.log`, `R5-QR_red_learning_ui.log`, `R5-QR_green_ui.log`,
  `R5-QR_full_ui_suite.log`, `R5-QR_route_probe.py`, `R5-QR_route_green.log`.

No commit made (per lane rules).
